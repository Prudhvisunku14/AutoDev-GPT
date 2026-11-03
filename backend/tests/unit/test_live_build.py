"""The first real build's script (scripts/live_build.py). Nothing here starts a container, reads the real .env or contacts anything external.

The evaluator's acceptance suite is shown to pass a correct tracker and to fail broken ones, by running it with this interpreter over
trusted code written in this file, which no model wrote."""

from __future__ import annotations

import dataclasses
import importlib.util
import json
import re
import shutil
import subprocess
import sys
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest

from app.config import Settings
from app.orchestrator.fake_llm import FakeLLM, fail
from app.orchestrator.llm_client import LLMErrorKind

SCRIPTS = Path(__file__).resolve().parents[2] / "scripts"

REFERENCE = '''
from datetime import date


class AttendanceTracker:
    def __init__(self):
        self._names = {}
        self._marks = {}

    def add_student(self, student_id, name):
        if not isinstance(student_id, str) or not student_id.strip() or student_id in self._names:
            raise ValueError("a student ID is not blank and not already added")
        self._names[student_id] = name
        self._marks[student_id] = {}

    def students(self):
        return list(self._names)

    def record(self, student_id, day, present):
        if student_id not in self._names:
            raise KeyError(student_id)
        parsed = date.fromisoformat(day)
        self._marks[student_id][parsed] = bool(present)

    def percentage(self, student_id):
        marks = self._marks[student_id]
        if not marks:
            return None
        return round(100 * sum(marks.values()) / len(marks), 2)
'''
# Each break of the reference and the criterion it must make fail.
BREAKS = {
    "AT-004": ("round(100 * sum(marks.values()) / len(marks), 2)", "100 * sum(marks.values()) / len(marks)"),
    "AT-002": (" or student_id in self._names", ""),
    "AT-005": ("return None", "return 0.0"),
    "AT-006": ("self._marks[student_id][parsed] = bool(present)", "self._marks[student_id][len(self._marks[student_id])] = bool(present)"),
    "AT-008": ("parsed = date.fromisoformat(day)", "parsed = day"),
    "AT-007": ("if student_id not in self._names:\n            raise KeyError(student_id)\n        ", "self._names.setdefault(student_id, '')\n        self._marks.setdefault(student_id, {})\n        "),
    "AT-003": ("not student_id.strip() or ", ""),
    "AT-001": ("return list(self._names)", "return sorted(self._names, reverse=True)"),
}


@pytest.fixture(scope="module")
def build() -> ModuleType:
    spec = importlib.util.spec_from_file_location("devpilot_live_build", SCRIPTS / "live_build.py")
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules["devpilot_live_build"] = module
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def demo(build: ModuleType, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> ModuleType:
    """live_demo, which live_build runs through. No test may read the repository's .env or write to the real demo folder."""
    module = sys.modules["live_demo"]
    monkeypatch.setattr(module, "REPO_ROOT", tmp_path)
    monkeypatch.setattr(module, "DEMO_ROOT", tmp_path / "runs")
    (tmp_path / "runs").mkdir()
    return module


def acceptance_run(tmp_path: Path, tracker: str) -> dict[str, str]:
    """Each acceptance test's outcome, from running the suite over this tracker."""
    from app.verification.acceptance import evaluation_files

    folder = tmp_path / "evaluation"
    folder.mkdir(parents=True)
    suite = sys.modules["devpilot_live_build"].SUITE
    for name, data in evaluation_files({"attendance.py": tracker.encode()}, suite).items():
        (folder / name).write_bytes(data)
    done = subprocess.run([sys.executable, "-I", "-B", "-m", "unittest", "discover", "-v", "-s", ".", "-p", "test*.py"], cwd=folder, capture_output=True, text=True, timeout=60, check=False)
    outcomes = {code.upper().replace("_", "-"): "ok" for code in re.findall(r"(test_at_\d{3})\S* \(.*\) \.\.\. ok", done.stderr)}
    for result, code in re.findall(r"^(FAIL|ERROR): (test_at_\d{3})", done.stderr, re.MULTILINE):
        outcomes[code.upper().replace("_", "-").replace("TEST-", "")] = result
    return {k.replace("TEST-", ""): v for k, v in outcomes.items()}


def test_the_evaluator_s_suite_passes_a_correct_tracker_and_catches_each_break(build: ModuleType, tmp_path: Path) -> None:
    assert [c.code for c in build.SUITE.criteria] == [f"AT-{n:03d}" for n in range(1, 9)]
    assert acceptance_run(tmp_path / "good", REFERENCE) == {f"AT-{n:03d}": "ok" for n in range(1, 9)}
    for code, (old, new) in BREAKS.items():
        assert REFERENCE.count(old) == 1, code
        outcome = acceptance_run(tmp_path / code, REFERENCE.replace(old, new))
        assert outcome.get(code) in ("FAIL", "ERROR"), f"{code} was not caught: {outcome}"


def test_the_request_names_everything_the_hidden_tests_use_and_none_of_them(build: ModuleType) -> None:
    for needed in ("attendance.py", "AttendanceTracker", "add_student(student_id", "students()", "record(student_id", "percentage(student_id", "ValueError", "KeyError", "YYYY-MM-DD",
                   "rounded to 2 decimal places", "None when no mark", "replaces the earlier mark", "test_attendance.py", "unittest", "unit tests"):  # fmt: skip
        assert needed in build.REQUEST, needed
    assert "test_at_" not in build.REQUEST and "S1" not in build.REQUEST and "66.67" not in build.REQUEST and "assertEqual" not in build.REQUEST


def test_the_limits_are_valid_strict_and_win_over_the_environment_file(build: ModuleType, demo: ModuleType, tmp_path: Path) -> None:
    (tmp_path / ".env").write_text("RUN_BUDGET_USD=99\nOPENAI_MAX_RETRIES=5\nGATEWAY_MAX_USD_PER_TASK=50\nOPENAI_MODEL=gpt-other\nMAX_TURNS=500\n", encoding="utf-8")
    s = demo.live_settings(tmp_path, approved=False, limits=build.LIMITS)
    assert (s.run_budget_usd, s.openai_max_retries, s.gateway_max_usd_per_task, s.openai_model, s.max_turns, s.coding_live_approved) == (0.50, 0, 0.30, "gpt-6.1-sol", 12, False)
    assert s.cost_known and s.opencode_enabled and (s.openai_price_input_per_mtok, s.openai_price_output_per_mtok) == (2.0, 10.0)
    assert s.gateway_max_usd_per_task < s.run_budget_usd and demo.live_settings(tmp_path, approved=True, limits=build.LIMITS).coding_live_approved is True
    Settings(_env_file=None, devpilot_api_token="t" * 32, **build.LIMITS)  # type: ignore[call-arg]
    assert demo.LIMITS["run_budget_usd"] == 1.50 and demo.SPEND_LIMIT_USD == 1.50  # the calculator demonstration is unchanged


def test_the_first_request_of_each_kind_fits_inside_the_cap(build: ModuleType) -> None:
    """The worst case of a GPT request and of an OpenCode request, at the measured sizes, leaves room for a whole run."""
    gpt = (5_000 * 2 + build.LIMITS["openai_max_output_tokens"] * 10) / 1e6
    opencode = (17_202 * 2 + build.LIMITS["gateway_max_output_tokens"] * 10) / 1e6
    assert gpt < 0.06 and opencode < 0.08 and 8 * gpt + 2 * opencode < build.SPEND_LIMIT_USD


def test_machine_checks_use_the_readiness_thresholds(build: ModuleType) -> None:
    good = {"c_free_gb": 11.6, "ram_available_gb": 4.4, "commit_percent": 60.0}
    assert all(passed for _, passed, _ in build.machine_checks(lambda: good))
    for key, value in (("c_free_gb", 9.9), ("ram_available_gb", 3.9), ("commit_percent", 70.5), ("c_free_gb", None), ("commit_percent", None)):
        results = build.machine_checks(lambda k=key, v=value: {**good, k: v})
        assert [passed for _, passed, _ in results].count(False) == 1, (key, value)
    assert all(passed for _, passed, _ in build.machine_checks(lambda: {"c_free_gb": 10.0, "ram_available_gb": 4.0, "commit_percent": 70.0}))
    assert set(build.machine_readings()) == {"c_free_gb", "ram_available_gb", "commit_percent"}


@pytest.mark.parametrize(("typed", "accepted"), [("yes", True), (" YES ", True), ("y", False), ("", False), ("no", False), ("yes please", False)])
def test_only_a_typed_yes_confirms(build: ModuleType, typed: str, accepted: bool) -> None:
    said: list[str] = []
    assert build.confirm(lambda _q: typed, said.append) is accepted
    text = "\n".join(said)
    assert "$0.50" in text and "PAID" in text and "not sent" in text


class _Guarded:
    """A preflight with the given result, and alarms on everything that would cost money or start something."""

    def __init__(self, demo: ModuleType, monkeypatch: pytest.MonkeyPatch, *, passed: bool = True) -> None:
        self.started: list[str] = []
        monkeypatch.setattr(demo, "preflight", lambda *_: [("a check", passed, "")])
        monkeypatch.setattr(demo, "keep_awake", lambda: self.started.append("awake"))
        monkeypatch.setattr(demo, "start_guard", lambda *_, **__: self.started.append("guard"))
        monkeypatch.setattr(demo, "model_access", lambda: self.started.append("model_access") or True)
        monkeypatch.setattr(demo, "dry_launch", lambda *_: self.started.append("dry_launch"))
        monkeypatch.setattr(demo.OpenCodeLauncher, "unavailable_reason", lambda _: None)
        self.ran: list[dict[str, Any]] = []

        def run(*_a: Any, **kw: Any) -> dict[str, Any]:
            self.started.append("run_demo")
            self.ran.append(kw)
            return {"state": "COMPLETED", "left_behind": {"containers": [], "networks": []}, "attempts": 1, "cost_usd": 0.2, "report": {}}

        monkeypatch.setattr(demo, "run_demo", run)


def experiment(build: ModuleType, *, confirmed: bool = True, machine: bool = True, problems: list[str] | None = None) -> Any:
    return dataclasses.replace(build.EXPERIMENT, extra_checks=lambda: [("the machine", machine, "")], confirm=lambda: confirmed, judge=lambda _s: problems or [])


@pytest.mark.parametrize("amount", ["1.50", "0.51", "0.49", "5", "0"])
def test_only_the_approved_figure_is_accepted(build: ModuleType, demo: ModuleType, monkeypatch: pytest.MonkeyPatch, amount: str, capsys: pytest.CaptureFixture[str]) -> None:
    monkeypatch.setattr(demo, "preflight", lambda *_: pytest.fail("nothing may run after a refused figure"))
    assert build.main(["--approve-spend", amount]) == 2
    assert "$0.50" in capsys.readouterr().out


def test_with_no_approval_nothing_external_or_paid_happens_and_no_question_is_asked(build: ModuleType, demo: ModuleType, monkeypatch: pytest.MonkeyPatch) -> None:
    guarded = _Guarded(demo, monkeypatch)
    asked: list[str] = []
    assert demo.main([], dataclasses.replace(experiment(build), confirm=lambda: asked.append("asked") or True)) == 0
    assert guarded.started == [] and asked == []


def test_the_paid_path_asks_a_person_after_the_checks_and_before_anything_starts(build: ModuleType, demo: ModuleType, monkeypatch: pytest.MonkeyPatch) -> None:
    guarded = _Guarded(demo, monkeypatch)
    order: list[str] = []
    exp = dataclasses.replace(experiment(build), confirm=lambda: order.append(f"confirm after {guarded.started}") or True)
    assert demo.main(["--approve-spend", "0.50"], exp) == 0
    assert order == ["confirm after []"] and guarded.started == ["awake", "guard", "model_access", "dry_launch", "run_demo"]
    ran = guarded.ran[0]
    assert ran["request"] == build.REQUEST and ran["name"] == build.NAME and ran["acceptance"] == build.ACCEPTANCE


def test_declining_or_a_failed_check_stops_the_paid_run_before_anything_starts(build: ModuleType, demo: ModuleType, monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
                                                                               capsys: pytest.CaptureFixture[str]) -> None:  # fmt: skip
    for number, (kwargs, passed) in enumerate((({"confirmed": False}, True), ({"machine": False}, True), ({}, False))):
        monkeypatch.setattr(demo, "DEMO_ROOT", tmp_path / f"runs-{number}")  # the run folder is named by the second
        (tmp_path / f"runs-{number}").mkdir()
        guarded = _Guarded(demo, monkeypatch, passed=passed)
        assert demo.main(["--approve-spend", "0.50"], experiment(build, **kwargs)) == (0 if kwargs == {"confirmed": False} else 1)
        assert guarded.started == [], kwargs
    assert "Not confirmed" in capsys.readouterr().out


def test_a_run_is_a_success_only_if_the_judge_finds_no_problem(build: ModuleType, demo: ModuleType, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    _Guarded(demo, monkeypatch)
    assert demo.main(["--approve-spend", "0.50"], experiment(build, problems=["the suite failed"])) == 1
    assert "Not a success: the suite failed" in capsys.readouterr().out


def spend_experiment(build: ModuleType) -> Any:
    """The real spending ledger and nothing else changed: the machine's readings are replaced, since they depend on the laptop."""
    return dataclasses.replace(build.EXPERIMENT, extra_checks=build.SPEND.check, confirm=lambda: True, judge=lambda _s: [])


def ledger_lines(demo: ModuleType) -> list[dict[str, Any]]:
    path = demo.DEMO_ROOT / "build-ledger.jsonl"
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()] if path.is_file() else []


def clear_run_folders(demo: ModuleType) -> None:
    """The run folder is named by the second, so a second run in a test needs the first one's folder gone."""
    for folder in [p for p in demo.DEMO_ROOT.iterdir() if p.is_dir()]:
        shutil.rmtree(folder)


def test_the_budget_is_a_total_across_runs_and_counts_a_run_at_its_cap_from_the_start(build: ModuleType, demo: ModuleType, monkeypatch: pytest.MonkeyPatch) -> None:
    guarded = _Guarded(demo, monkeypatch)
    seen: list[list[str]] = []
    monkeypatch.setattr(demo, "keep_awake", lambda: seen.append([e["event"] for e in ledger_lines(demo)]))
    assert demo.main(["--approve-spend", "0.50"], spend_experiment(build)) == 0
    assert seen == [["reserved"]]  # already counted, at its whole cap, when the first container could start
    reserved, settled = ledger_lines(demo)
    assert reserved["usd"] == 0.50 and settled["usd"] == 0.2 and build.SPEND.check()[0][1] is False
    clear_run_folders(demo)
    guarded.started.clear()
    assert demo.main(["--approve-spend", "0.50"], spend_experiment(build)) == 1  # $0.20 is counted, and another full run could pass $0.50
    assert guarded.started == [] and len(ledger_lines(demo)) == 2


def test_a_run_that_stopped_before_any_model_call_costs_nothing_and_may_be_repeated(build: ModuleType, demo: ModuleType, monkeypatch: pytest.MonkeyPatch) -> None:
    guarded = _Guarded(demo, monkeypatch)
    monkeypatch.setattr(demo, "model_access", lambda: False)
    assert demo.main(["--approve-spend", "0.50"], spend_experiment(build)) == 1
    assert [e["usd"] for e in ledger_lines(demo)] == [0.50, 0.0] and build.SPEND.check()[0][1] is True
    clear_run_folders(demo)
    monkeypatch.setattr(demo, "dry_launch", lambda *_: "the port is taken")
    monkeypatch.setattr(demo, "model_access", lambda: True)
    assert demo.main(["--approve-spend", "0.50"], spend_experiment(build)) == 1
    assert [e["usd"] for e in ledger_lines(demo)] == [0.50, 0.0, 0.50, 0.0] and "run_demo" not in guarded.started


def test_a_run_that_crashes_or_has_an_unknown_cost_stays_counted_at_its_cap(build: ModuleType, demo: ModuleType, monkeypatch: pytest.MonkeyPatch) -> None:
    _Guarded(demo, monkeypatch)

    def crash(*_a: Any, **_k: Any) -> dict[str, Any]:
        raise RuntimeError("the terminal was closed")

    monkeypatch.setattr(demo, "run_demo", crash)
    with pytest.raises(RuntimeError):
        demo.main(["--approve-spend", "0.50"], spend_experiment(build))
    assert [e["event"] for e in ledger_lines(demo)] == ["reserved"] and build.SPEND.check()[0][1] is False  # never settled, so still $0.50
    (demo.DEMO_ROOT / "build-ledger.jsonl").unlink()
    clear_run_folders(demo)
    monkeypatch.setattr(demo, "run_demo", lambda *_a, **_k: {"state": "COMPLETED", "left_behind": {"containers": [], "networks": []}, "cost_usd": None})
    assert demo.main(["--approve-spend", "0.50"], spend_experiment(build)) == 0
    assert ledger_lines(demo)[-1]["usd"] == 0.50  # an unknown cost counts at the cap


def test_an_unreadable_ledger_refuses_and_a_preflight_never_writes_one(build: ModuleType, demo: ModuleType, monkeypatch: pytest.MonkeyPatch) -> None:
    guarded = _Guarded(demo, monkeypatch)
    assert demo.main([], spend_experiment(build)) == 0 and ledger_lines(demo) == []  # no approval: nothing is recorded
    clear_run_folders(demo)
    (demo.DEMO_ROOT / "build-ledger.jsonl").write_text("{broken\n", encoding="utf-8")
    assert demo.main(["--approve-spend", "0.50"], spend_experiment(build)) == 1 and guarded.started == []
    assert "cannot be read" in build.SPEND.check()[0][2]


def good_summary() -> dict[str, Any]:
    return {"attempts": 2, "cost_usd": 0.31, "report": {"provenance": {"simulated": [], "fully_real": True}, "acceptance": {"verdict": "PASSED", "reason": "all"},
                                                       "delivered_files": [{"path": "attendance.py"}, {"path": "test_attendance.py"}]}}  # fmt: skip


def test_the_judge_accepts_only_a_real_independently_passed_run_within_the_cap(build: ModuleType) -> None:
    assert build.judge(good_summary()) == []
    breaks: dict[str, Any] = {
        "no real model request": lambda s: s.update(attempts=0),
        "simulated": lambda s: s["report"]["provenance"].update(simulated=["coding provider"]),
        "unknown provenance": lambda s: s["report"].pop("provenance"),
        "acceptance did not pass": lambda s: s["report"]["acceptance"].update(verdict="FAILED"),
        "acceptance missing": lambda s: s["report"].pop("acceptance"),
        "nothing delivered": lambda s: s["report"].update(delivered_files=[]),
        "over the cap": lambda s: s.update(cost_usd=0.51),
        "cost unknown": lambda s: s.update(cost_usd=None),
    }
    for label, damage in breaks.items():
        summary = good_summary()
        damage(summary)
        assert build.judge(summary), label


def test_the_suite_reaches_the_run_and_is_frozen_before_any_model_is_asked(build: ModuleType, demo: ModuleType, tmp_path: Path) -> None:
    """run_demo with a model that is stopped at once: the run still carries the evaluator's frozen suite, and reports it as not run."""
    settings = demo.live_settings(tmp_path, approved=False, limits=build.LIMITS)
    llm = FakeLLM(fail(LLMErrorKind.STOPPED, "rehearsal: no model is asked"))
    summary = demo.run_demo(settings, llm=llm, coding=None, sandbox=None, out=tmp_path, request=build.REQUEST, wait=60, name=build.NAME, acceptance=build.ACCEPTANCE)
    acceptance = summary["report"]["acceptance"]
    assert acceptance["configured"] is True and acceptance["verdict"] == "NOT_RUN" and len(acceptance["suite_sha256"]) == 64
    assert summary["report"]["project"]["name"] == build.NAME and summary["delivered"] == [] and summary["cost_usd"] == 0
    assert "test_at_" not in llm.requests[0].input[0]["content"] and "assertEqual" not in str(llm.requests[0])  # GPT never sees the tests


def test_a_suite_the_api_refuses_stops_the_run_before_it_starts(build: ModuleType, demo: ModuleType, tmp_path: Path) -> None:
    settings = demo.live_settings(tmp_path, approved=False, limits=build.LIMITS)
    llm = FakeLLM()
    broken = {**build.ACCEPTANCE, "criteria": [*build.ACCEPTANCE["criteria"], {"code": "AT-009", "description": "named by no test", "priority": "MUST"}]}
    with pytest.raises(Exception, match="422"):
        demo.run_demo(settings, llm=llm, coding=None, sandbox=None, out=tmp_path, request=build.REQUEST, wait=30, name=build.NAME, acceptance=broken)
    assert llm.requests == []


def test_the_script_holds_no_key_and_has_no_path_to_a_scripted_model(build: ModuleType) -> None:
    source = (SCRIPTS / "live_build.py").read_text(encoding="utf-8")
    assert "get_secret_value" not in source and "sk-" not in source and "FakeLLM" not in source and "fake_llm" not in source
