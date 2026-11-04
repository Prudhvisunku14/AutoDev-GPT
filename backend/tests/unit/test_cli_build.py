"""`devpilot build`: the prompt, the stack, the typed spending approval, the run through the real engine, and the ZIP. Offline.

The engine tests drive the real app (routes, orchestrator, gates, approvals, events) with GPT scripted (FakeLLM), the coding engine
simulated and the tests replayed, so they prove the CLI's own flow, never real generation. Packaging is tested on a real-shaped
report with real files, because a simulated run is correctly refused a ZIP.
"""

from __future__ import annotations

import io
import json
import re
import zipfile
from collections.abc import Callable, Iterator
from contextlib import AbstractContextManager
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from fastapi.testclient import TestClient

from app.cli import build, credentials, runtime
from app.cli.engine import EngineRefused, Session
from app.cli.paths import Paths, paths as make_paths
from app.cli.userconfig import UserConfig
from app.coding import opencode_container
from app.coding.simulated import PROVIDER_NAME, ScriptedAttempt, SimulatedLauncher
from app.core.redaction import Redactor
from app.demo import mvp
from app.demo.fixtures import SEED
from app.main import create_app
from app.orchestrator.fake_llm import FakeLLM, call
from app.sandbox.replay import ReplaySandbox
from app.selection.selector import ProviderOption, ProviderSelector
from app.tools.adapters.workspace_fs import WorkspaceFS
from tests.helpers import make_settings

CONFIG = UserConfig(model="gpt-test", price_input_per_mtok=2.0, price_output_per_mtok=10.0)
FAKE_KEY = "sk-test" + "K" * 40
IMAGE = "sha256:" + "a" * 64
PROMPT = "Create a python tool that converts csv files to json with unit tests"


class ScriptedIO:
    def __init__(self, *answers: str) -> None:
        self.answers = list(answers)
        self.lines: list[str] = []
        self.asked: list[str] = []

    def ask(self, prompt: str) -> str:
        self.asked.append(prompt)
        if not self.answers:
            raise AssertionError(f"an unexpected question: {prompt}")
        return self.answers.pop(0)

    def say(self, text: str = "") -> None:
        self.lines.append(text)

    @property
    def text(self) -> str:
        return "\n".join(self.lines)


@pytest.fixture(autouse=True)
def _fast_and_clean(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    monkeypatch.setattr(build, "POLL_SECONDS", 0.01)
    yield
    opencode_container._LOCALLY_APPROVED.clear()


@pytest.fixture
def p(tmp_path: Path) -> Paths:
    where = make_paths(tmp_path / "home")
    where.ensure()
    return where


# --- The prompt and the stack -----------------------------------------------------------------------


def test_the_prompt_must_be_a_real_description_of_something_that_can_be_built() -> None:
    assert build.validate_prompt("   ") == "Describe the application you want to build."
    assert "at least 15" in (build.validate_prompt("todo app") or "")
    assert "limit" in (build.validate_prompt("x" * 19_001) or "")
    assert build.validate_prompt(PROMPT) is None
    assert build.validate_prompt("Create a weather forecast application") is None
    for other in ("Create a React dashboard for my sales data", "Build a FastAPI service for bookings", "I need a website for my shop", "Make an Android app for habits"):
        assert build.validate_prompt(other) is None, other  # length only: what is asked for is decided from the request and readiness
        assert "cannot be built" in (build.unsupported_request(other) or ""), other  # none of these stacks is ready (or exists), so each is declined


def test_a_project_is_named_from_the_first_sentence() -> None:
    assert build.project_name("Create a habit tracker. It should have streaks.") == "Create a habit tracker"
    assert len(build.project_name("word " * 40)) == 60 and build.project_name("word " * 40).endswith("…")


def test_only_python_cli_is_offered_as_buildable_and_the_others_say_why_not() -> None:
    stacks = {s[0]: s for s in build.available_stacks()}
    assert stacks["python-cli"][2] is True
    assert not stacks["python-fastapi"][2] and not stacks["react-vite"][2] and "not built or approved" in stacks["python-fastapi"][3]


def test_with_one_verified_stack_it_is_used_and_named_not_asked_about_and_the_rest_are_listed_as_unavailable(monkeypatch: pytest.MonkeyPatch) -> None:
    io = ScriptedIO()  # any question would fail the test
    assert build.choose_stack(None, io, interactive=True) == "python-cli"
    assert "not available yet" in io.text and "Stack: python-cli, the only one verified end to end so far." in io.text
    assert build.choose_stack("python-cli", ScriptedIO(), interactive=False) == "python-cli"


def test_with_several_buildable_stacks_the_person_chooses_and_a_wrong_answer_is_asked_again(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(build, "available_stacks", lambda: [("python-cli", "Python CLI", True, ""), ("other", "Other", True, ""), ("nope", "Nope", False, "not built")])
    assert build.choose_stack(None, ScriptedIO(""), interactive=True) == "python-cli"
    io = ScriptedIO("nope", "other")
    assert build.choose_stack(None, io, interactive=True) == "other" and "Choose one of: python-cli, other." in io.text


def test_the_stack_question_refuses_what_cannot_be_built() -> None:
    refused = ScriptedIO()
    assert build.choose_stack("react-vite", refused, interactive=False) is None and "cannot be built yet" in refused.text
    assert build.choose_stack("cobol", ScriptedIO(), interactive=False) is None
    assert build.choose_stack(None, ScriptedIO(), interactive=False) == "python-cli"


# --- The spending approval --------------------------------------------------------------------------


def test_the_approval_states_the_actual_cap_and_that_the_cost_cannot_be_known_beforehand() -> None:
    text = build.approval_text(CONFIG, 0.5)
    assert "$0.50" in text and "cannot be known beforehand" in text and "not sent" in text and "gpt-test" in text


@pytest.mark.parametrize(("answer", "approved"), [("approve 0.50", True), ("  APPROVE 0.50 ", True), ("yes", False), ("approve 0.51", False), ("", False), ("approve", False), ("y", False)])
def test_only_the_typed_phrase_for_the_exact_cap_approves(answer: str, approved: bool) -> None:
    assert build.confirm_spend(CONFIG, 0.5, ScriptedIO(answer), None) is approved


def test_the_command_line_approval_must_equal_the_cap_exactly() -> None:
    assert build.confirm_spend(CONFIG, 0.5, ScriptedIO(), 0.5) is True
    io = ScriptedIO()
    assert build.confirm_spend(CONFIG, 0.5, io, 1.0) is False and "not this build's limit" in io.text


# --- Progress lines ---------------------------------------------------------------------------------


def test_events_become_readable_lines_and_bookkeeping_is_skipped() -> None:
    def d(kind: str, **payload: Any) -> str | None:
        return build.describe_event({"type": kind, "payload": payload})

    assert d("run.state_changed", to="IMPLEMENTING") == "Writing the code" and d("run.state_changed", to="COMPLETED") is None
    assert d("plan.accepted", tasks=2, stack="python-cli") == "Plan accepted: 2 task(s) on python-cli"
    assert d("coding.finished", task_order=1, status="COMPLETED", kept=True, changes=[{"path": "a.py"}]) == "Coding task 1: COMPLETED, 1 file(s) kept"
    assert "undone" in (d("coding.finished", task_order=1, status="FAILED", kept=False, detail="refused") or "")
    assert d("verification.finished", verdict="PASSED", passed=6, ran=6) == "Tests PASSED: 6 of 6 passed"
    assert "2 failed" in (d("verification.finished", verdict="FAILED", passed=4, ran=6, reason="2 failed") or "")
    assert d("recovery.decided", category="TEST_FAILURE", action="REPAIR") == "Recovery: TEST_FAILURE → REPAIR"
    assert d("delivery.finished", files=["a.py", "b.py"]) == "Delivered 2 file(s)"
    assert d("llm.request") is None and d("tool.called") is None and d("something.new") is None
    assert "stopped" in (d("run.failed", reason="budget", detail="limit") or "")


# --- Packaging --------------------------------------------------------------------------------------


def real_shaped_report(project_id: str, *, acceptance_ok: bool | None = None, tests: str = "PASSED") -> dict[str, Any]:
    acceptance: dict[str, Any] = {"configured": False, "note": "none"} if acceptance_ok is None else {"configured": True, "verdict": "PASSED" if acceptance_ok else "FAILED", "passed": 1, "total": 1}
    return {
        "project": {"id": project_id, "name": "CSV to JSON", "requirement": "r", "stack": "python-cli"},
        "run": {"id": "0123456789abcdef", "state": "COMPLETED", "outcome": "completed", "detail": None, "turns": 3, "tokens": 100, "cost_usd": 0.1234, "repairs": 0},
        "tasks": [], "criteria": [], "acceptance_runs": [], "recovery": [], "acceptance": acceptance,
        "verifications": [{"verdict": tests, "passed": 4, "ran": 4, "exit_code": 0, "container": "c", "reason": "ok", "failed_names": []}],
        "delivered_files": [{"path": "convert.py", "change": "added", "bytes": 10}],
        "provenance": {"gpt": "OpenAI Responses API", "coding_providers": ["opencode_server"], "sandbox": "DockerSandbox", "simulated": [], "fully_real": True},
    }  # fmt: skip


class StubClient:
    """A client that answers from a table of (method, path regex) -> handler(params, body) -> (status, json)."""

    def __init__(self, routes: list[tuple[str, str, Callable[[dict[str, Any] | None, Any], tuple[int, Any]]]]) -> None:
        self.routes, self.posts = routes, []

    def _answer(self, method: str, url: str, params: dict[str, Any] | None, body: Any) -> Any:
        if method == "POST":
            self.posts.append((url, body))
        for verb, pattern, handler in self.routes:
            if verb == method and re.search(pattern, url):
                status, payload = handler(params, body)
                return SimpleNamespace(status_code=status, json=lambda payload=payload: payload, text=json.dumps(payload))
        raise AssertionError(f"no route for {method} {url}")

    def get(self, url: str, headers: Any = None, params: dict[str, Any] | None = None) -> Any:
        return self._answer("GET", url, params, None)

    def post(self, url: str, headers: Any = None, json: Any = None) -> Any:
        return self._answer("POST", url, None, json)


def packaging_session(tmp_path: Path, report: dict[str, Any], secret: str = "configured-secret-value") -> tuple[Session, str]:
    fs = WorkspaceFS(tmp_path / "ws", max_file_bytes=1_000_000, max_files=100)
    project_id = "ab" * 16  # project ids are 32 hex characters
    fs.create_project(project_id)
    report["project"]["id"] = project_id
    fs.write_text(project_id, "convert.py", "def convert(rows):\n    return rows\n")
    fs.write_text(project_id, "test_convert.py", "import unittest\n")
    fs.write_text(project_id, "README.md", "# CSV to JSON\n")
    fs.write_text(project_id, ".env", f"OPENAI_API_KEY={secret}\n")  # excluded by name: never project source
    fs.write_text(project_id, "__pycache__/convert.cpython-312.pyc", "compiled")
    client = StubClient([("GET", r"/report$", lambda params, body: (200, report))])
    app = SimpleNamespace(state=SimpleNamespace(workspace_fs=fs, redactor=Redactor([secret])))
    return Session(client, {}, app, SimpleNamespace()), project_id  # type: ignore[arg-type]


def test_a_verified_delivery_becomes_a_folder_and_a_zip_with_everything_but_the_secrets(tmp_path: Path) -> None:
    session, _ = packaging_session(tmp_path, real_shaped_report("x"))
    out = tmp_path / "out"
    result = build.package(session, {"id": "r", "state": "COMPLETED"}, out, ScriptedIO())

    assert result.ok and result.verification == "generated-tests" and result.cost_usd == 0.1234
    assert result.zip_path == out / "csv-to-json-01234567.zip" and result.folder == out / "csv-to-json-01234567"
    names = zipfile.ZipFile(result.zip_path).namelist()  # type: ignore[arg-type]
    root = "csv-to-json-01234567/"
    for expected in ("convert.py", "test_convert.py", "README.md", build.HOW_TO_RUN, "devpilot-verification/report.md", "devpilot-verification/report.json", "devpilot-verification/MANIFEST.txt"):
        assert root + expected in names, expected
    assert not any(n.endswith((".env", ".pyc")) for n in names)  # secrets and caches stay out
    archive = zipfile.ZipFile(result.zip_path)  # type: ignore[arg-type]
    assert not any(b"configured-secret-value" in archive.read(n) for n in names)
    assert "Verified by the generated tests only" in archive.read(root + "devpilot-verification/MANIFEST.txt").decode()
    assert "python -m unittest discover" in archive.read(root + build.HOW_TO_RUN).decode()
    on_disk = sorted(f.relative_to(result.folder).as_posix() for f in result.folder.rglob("*") if f.is_file())  # type: ignore[union-attr]
    assert on_disk == ["README.md", "convert.py", "test_convert.py"]  # the folder holds exactly what the ZIP's project holds


def test_nothing_is_packaged_when_the_tests_failed_or_the_independent_suite_did(tmp_path: Path) -> None:
    for report, expected in ((real_shaped_report("x", tests="FAILED"), "did not pass"), (real_shaped_report("x", acceptance_ok=False), "independent acceptance did not pass")):
        session, _ = packaging_session(tmp_path / expected[:5], report)
        out = tmp_path / f"out-{expected[:5]}"
        result = build.package(session, {"id": "r", "state": "COMPLETED"}, out, ScriptedIO())
        assert not result.ok and expected in (result.reason or "") and not out.exists()


@pytest.mark.parametrize("leaked", ["tests/test_auth.py", "app.js", "config.json", "index.html", "notes.txt"])
def test_the_cli_refuses_a_zip_when_a_safeguard_drops_a_source_file_exactly_like_the_download(tmp_path: Path, leaked: str) -> None:
    """A source or configuration file holding a credential cannot be packaged, so the project is incomplete: the CLI
    refuses with the same reason as the dashboard's download, and writes nothing."""
    from app.api.artifacts import describe

    report = real_shaped_report("x")
    session, project_id = packaging_session(tmp_path, report)
    fs = session.app.state.workspace_fs
    fs.write_text(project_id, leaked, "TOKEN = 'configured-secret-value'\n")
    out = tmp_path / "out"
    result = build.package(session, {"id": "r", "state": "COMPLETED"}, out, ScriptedIO())
    assert not result.ok and "could not be included safely" in (result.reason or "") and leaked in (result.reason or "") and not out.exists()
    http = describe(report, fs.list_files(project_id), lambda p: fs.read_bytes(project_id, p), session.app.state.redactor)
    assert not http.available and http.reason == result.reason


def test_a_simulated_run_is_never_packaged(tmp_path: Path) -> None:
    report = real_shaped_report("x")
    report["provenance"]["simulated"] = ["GPT orchestration"]
    session, _ = packaging_session(tmp_path, report)
    assert "simulated components" in (build.package(session, {"id": "r", "state": "COMPLETED"}, tmp_path / "o", ScriptedIO()).reason or "")


# --- The loop: questions, pauses, refusals ----------------------------------------------------------


def stub_run(states: list[dict[str, Any]], events: list[list[dict[str, Any]]], approvals: list[dict[str, Any]] | None = None) -> StubClient:
    runs, batches = iter(states), iter(events)
    last = {"run": states[-1], "events": []}

    def next_run(params: Any, body: Any) -> tuple[int, Any]:
        last["run"] = next(runs, last["run"])
        return 200, last["run"]

    return StubClient([
        ("GET", r"/runs/r1$", next_run),
        ("GET", r"/runs/r1/events$", lambda params, body: (200, next(batches, []))),
        ("GET", r"/approvals$", lambda params, body: (200, approvals or [])),
        ("POST", r"/approvals/a1/decision$", lambda params, body: (200, {})),
        ("POST", r"/runs/r1/cancel$", lambda params, body: (200, {})),
    ])  # fmt: skip


def run(state: str, **extra: Any) -> dict[str, Any]:
    return {"id": "r1", "state": state, "ended_at": None, "pause_reason": None, **extra}


def loop_session(client: StubClient) -> Session:
    return Session(client, {}, SimpleNamespace(), SimpleNamespace(run_wall_clock_seconds=1200.0))  # type: ignore[arg-type]


def test_progress_is_printed_from_the_events_as_they_arrive() -> None:
    client = stub_run([run("ANALYZING"), run("COMPLETED", ended_at="t")], [[{"seq": 1, "type": "run.state_changed", "payload": {"to": "ANALYZING"}}], [{"seq": 2, "type": "plan.accepted", "payload": {"tasks": 1, "stack": "python-cli"}}]])
    io = ScriptedIO()
    final = build._wait(loop_session(client), "r1", io, sleep=lambda s: None, clock=lambda: 0.0)
    assert final["state"] == "COMPLETED" and "Analysing your request" in io.text and "Plan accepted: 1 task(s) on python-cli" in io.text


def test_a_question_from_gpt_is_answered_by_the_person_and_pressing_enter_stops_the_build() -> None:
    question = {"id": "a1", "kind": "HUMAN_INPUT", "payload": {"question": "Which delimiter?", "reason": "the request is open"}, "tool": "request_human_input", "risk": "LOW"}
    client = stub_run([run("AWAITING_APPROVAL"), run("COMPLETED", ended_at="t")], [], [question])
    io = ScriptedIO("a comma")
    build._wait(loop_session(client), "r1", io, sleep=lambda s: None, clock=lambda: 0.0)
    assert "GPT asks: Which delimiter?" in io.text
    assert client.posts == [("/api/v1/approvals/a1/decision", {"decision": "approve", "response": "a comma"})]

    stopping = stub_run([run("AWAITING_APPROVAL"), run("CANCELLED", ended_at="t")], [], [question])
    out = ScriptedIO("")
    build._wait(loop_session(stopping), "r1", out, sleep=lambda s: None, clock=lambda: 0.0)
    assert "Stopping the build at your request" in out.text and any("/cancel" in url for url, _ in stopping.posts)


def test_a_tool_approval_is_denied_unless_the_person_types_yes() -> None:
    risky = {"id": "a1", "kind": "TOOL", "payload": {}, "details": {"repo": "x"}, "tool": "import_repository", "risk": "HIGH"}
    for answer, decision in (("yes", "approve"), ("sure", "deny"), ("", "deny")):
        client = stub_run([run("AWAITING_APPROVAL"), run("COMPLETED", ended_at="t")], [], [risky])
        build._wait(loop_session(client), "r1", ScriptedIO(answer), sleep=lambda s: None, clock=lambda: 0.0)
        assert client.posts[0][1] == {"decision": decision}


def test_a_paused_build_is_closed_so_nothing_goes_on_spending_unattended() -> None:
    client = stub_run([run("IMPLEMENTING", pause_reason="budget")], [])
    io = ScriptedIO()
    build._wait(loop_session(client), "r1", io, sleep=lambda s: None, clock=lambda: 0.0)
    assert "paused (budget)" in io.text and any("/cancel" in url for url, _ in client.posts)


def test_a_build_that_outlives_its_time_is_closed() -> None:
    ticks = iter(range(0, 100_000, 700))
    client = stub_run([run("IMPLEMENTING")] * 5, [])
    io = ScriptedIO()
    build._wait(loop_session(client), "r1", io, sleep=lambda s: None, clock=lambda: float(next(ticks)))
    assert "did not finish in time" in io.text and any("/cancel" in url for url, _ in client.posts)


def test_the_start_always_carries_the_approved_cap_and_a_refusal_by_the_engine_is_reported() -> None:
    client = StubClient([
        ("POST", r"/projects$", lambda params, body: (201, {"id": "p1"})),
        ("POST", r"/projects/p1/runs$", lambda params, body: (201, {"id": "r1"})),
        ("POST", r"/runs/r1/start$", lambda params, body: (409, {"detail": "Not started: the session's approved total could be passed"})),
    ])  # fmt: skip
    with pytest.raises(build.BuildError, match="starting the build was refused: Not started"):
        build.run_build(loop_session(client), PROMPT, "python-cli", 0.5, Path("unused"), ScriptedIO())
    created = client.posts[0][1]
    assert created["stack"] == "python-cli" and "Use this stack exactly" in created["requirement_text"] and "acceptance" not in created
    assert client.posts[2] == ("/api/v1/runs/r1/start", {"approved_max_usd": 0.5})


# --- The whole command ------------------------------------------------------------------------------


class SimulatedEngine(AbstractContextManager[Session]):
    """The real app, with GPT scripted, the coding engine simulated and tests replayed (never real generation)."""

    instances: list[SimulatedEngine] = []

    def __init__(self, tmp_path: Path, cap: float) -> None:
        settings = make_settings(tmp_path / "sim", require_spend_approval=True, run_budget_usd=cap, openai_price_input_per_mtok=2.0, openai_price_output_per_mtok=10.0)
        llm = FakeLLM(*mvp._calculator_opening(), call("generate_code", {"task_order": 1, "instructions": "m"}), call("generate_code", {"task_order": 2, "instructions": "t"}),
                      call("submit_implementation", {"summary": "done"}), call("finish", {"summary": "built"}))  # fmt: skip
        launcher = SimulatedLauncher([ScriptedAttempt({"calculator.py": mvp.CALCULATOR, **SEED}), ScriptedAttempt({"test_calculator.py": mvp.CALCULATOR_TESTS})])
        selector = ProviderSelector([ProviderOption(PROVIDER_NAME, launcher, frozenset({"code_generation", "test_generation", "documentation"}), simulated=True)])
        self.app = create_app(settings, llm=llm, sandbox=ReplaySandbox.load(), coding=selector)
        self.client = TestClient(self.app)
        self.settings = settings
        self.events: list[str] = []
        SimulatedEngine.instances.append(self)

    def __enter__(self) -> Session:
        self.client.__enter__()
        return Session(self.client, {"Authorization": f"Bearer {self.settings.devpilot_api_token.get_secret_value()}"}, self.app, self.settings)

    def __exit__(self, *exc: object) -> None:
        headers = {"Authorization": f"Bearer {self.settings.devpilot_api_token.get_secret_value()}"}
        runs = self.client.get("/api/v1/runs", headers=headers).json()
        for run_row in runs:
            self.events += [e["type"] for e in self.client.get(f"/api/v1/runs/{run_row['id']}/events", params={"limit": 500}, headers=headers).json()]
        self.client.__exit__(*exc)  # type: ignore[arg-type]


@pytest.fixture
def ready(monkeypatch: pytest.MonkeyPatch, p: Paths, tmp_path: Path) -> list[str]:
    """Doctor passes, a (fake) key and an approved image exist; the engine is the simulated one. Returns the list of engine caps used."""
    SimulatedEngine.instances.clear()
    caps: list[str] = []
    monkeypatch.setattr(build, "run_checks", lambda paths, config: [])
    monkeypatch.setattr(credentials, "load_key", lambda path: FAKE_KEY)
    monkeypatch.setattr(runtime, "load_approval", lambda paths, **kw: (IMAGE, None))

    def factory(paths: Paths, config: UserConfig, key: str, image_id: str, manifest: Path, cap: float, io: Any) -> SimulatedEngine:
        assert key == FAKE_KEY and image_id == IMAGE
        caps.append(f"{cap:.2f}")
        return SimulatedEngine(tmp_path, cap)

    monkeypatch.setattr(build, "_default_engine", factory)
    return caps


def command(p: Paths, io: ScriptedIO, **overrides: Any) -> int:
    args: dict[str, Any] = {"prompt": None, "stack": None, "max_usd": None, "output": None, "approve_spend": None, "io": io, "interactive": True}
    return build.build_command(p, CONFIG, **(args | overrides), engine_factory=build._default_engine)


def test_the_whole_flow_asks_for_everything_approves_exactly_once_and_runs_the_real_engine(ready: list[str], p: Paths, tmp_path: Path) -> None:
    io = ScriptedIO(PROMPT, "approve 0.50")
    code = command(p, io, output=tmp_path / "out")
    assert [q.strip().split(":")[0].split("?")[0] for q in io.asked] == ["What application should DevPilot build", 'Type "approve 0.50" to start the build, or anything else to cancel']
    assert "Stack: python-cli" in io.text
    assert ready == ["0.50"]
    engine = SimulatedEngine.instances[0]
    assert "run.spend_approved" in engine.events and "run.start_requested" in engine.events and engine.events.index("run.spend_approved") < engine.events.index("run.start_requested")
    assert "Build started" in io.text and "Plan accepted" in io.text and "Tests PASSED" in io.text and "Final state: COMPLETED" in io.text
    assert code == 1 and "No ZIP was made: the run used simulated components" in io.text  # simulated: correctly refused a ZIP
    assert not (tmp_path / "out").exists()


def test_declining_the_approval_starts_nothing_and_spends_nothing(ready: list[str], p: Paths) -> None:
    for answer in ("no", "", "approve 0.49", "approve"):
        io = ScriptedIO(answer)
        assert command(p, io, prompt=PROMPT, stack="python-cli") == 0
        assert "Not approved: nothing was started, contacted or spent" in io.text
    assert ready == [] and SimulatedEngine.instances == []  # the engine was never even created


def test_a_failed_check_stops_the_build_before_anything_is_asked_or_spent(monkeypatch: pytest.MonkeyPatch, p: Paths) -> None:
    from app.cli import doctor

    monkeypatch.setattr(build, "run_checks", lambda paths, config: [doctor.Check("Docker running", "fail", "not answering", "start Docker Desktop")])
    created: list[int] = []
    monkeypatch.setattr(build, "_default_engine", lambda *a: created.append(1))
    io = ScriptedIO()
    assert command(p, io, prompt=PROMPT) == 1
    assert "FAIL" in io.text and "start Docker Desktop" in io.text and "Nothing was started or spent" in io.text and io.asked == [] and created == []


def test_the_cap_is_bounded_and_a_mismatched_command_line_approval_is_refused(ready: list[str], p: Paths) -> None:
    io = ScriptedIO()
    assert command(p, io, prompt=PROMPT, max_usd=5.0) == 2 and "at most $2.00" in io.text
    refused = ScriptedIO()
    assert command(p, refused, prompt=PROMPT, approve_spend=1.0) == 0 and "not this build's limit" in refused.text and ready == []
    approved = ScriptedIO()
    command(p, approved, prompt=PROMPT, approve_spend=0.5, interactive=False)
    assert "Approved on the command line" in approved.text and ready == ["0.50"] and approved.asked == []


def test_a_custom_cap_is_what_is_approved_typed_and_enforced(ready: list[str], p: Paths) -> None:
    io = ScriptedIO("approve 0.25")
    command(p, io, prompt=PROMPT, max_usd=0.25)
    assert ready == ["0.25"] and "at most $0.25" in io.text
    engine = SimulatedEngine.instances[0]
    assert engine.settings.run_budget_usd == 0.25  # the engine's own enforced limit is the approved figure


def test_a_description_that_cannot_be_built_is_declined_not_quietly_built_as_something_else(ready: list[str], p: Paths) -> None:
    io = ScriptedIO()
    assert command(p, io, prompt="Create a React dashboard for my sales data") == 2
    assert "cannot be built yet" in io.text and ready == [] and io.asked == []
    retry = ScriptedIO("Build a FastAPI backend for my shop", PROMPT, "no")
    assert command(p, retry) == 0 and "cannot be built yet" in retry.text and "Not approved" in retry.text


def test_no_prompt_without_a_terminal_is_refused_rather_than_guessed(ready: list[str], p: Paths) -> None:
    io = ScriptedIO()
    assert command(p, io, interactive=False) == 2 and "no application description" in io.text


def test_an_engine_that_will_not_start_is_reported_as_nothing_spent(monkeypatch: pytest.MonkeyPatch, p: Paths, ready: list[str]) -> None:
    class Refusing(AbstractContextManager[Session]):
        def __enter__(self) -> Session:
            raise EngineRefused("OpenAI did not accept the API key. Nothing was billed.")

        def __exit__(self, *exc: object) -> None:
            return None

    monkeypatch.setattr(build, "_default_engine", lambda *a: Refusing())
    io = ScriptedIO("approve 0.50")
    assert command(p, io, prompt=PROMPT) == 1
    assert "Nothing was spent. OpenAI did not accept the API key" in io.text
