"""The terminal planning mode (app/plan_cli.py), offline: FakeLLM or a stand-in for the OpenAI client, never the
network, never the real .env. Each test writes its own .env with a fake key."""

from __future__ import annotations

import ast
import json
from pathlib import Path
from typing import Any

import pytest

from app import plan_cli
from app.orchestrator.fake_llm import FakeLLM, call, text
from app.orchestrator.llm_client import LLMError, LLMErrorKind, LLMRequest
from app.plan_cli import (
    EXPERIMENT_CAP_USD,
    MAX_OUTPUT_TOKENS,
    MAX_REQUESTS,
    NO_PLAN,
    PLAN_ACCEPTED,
    PLAN_DONE,
    RUN_CAP_USD,
    TOKEN_LIMIT,
    Ledger,
    PlanningGuard,
    planning_dir,
    planning_settings,
    render,
    run_planning,
)
from app.verification.recipes import readiness
from tests.helpers import FAKE_OPENAI_KEY

PROMPT = "Build a Python weather application that accepts a city name, retrieves weather from a configurable API, and displays temperature, humidity and wind speed."
ANALYSIS = {
    "summary": "A command-line weather tool: it takes a city name, asks a configurable weather API, and prints temperature, humidity and wind speed.",
    "requirements": [
        {"code": "R-001", "text": "Accept a city name as a command-line argument and reject an empty one.", "kind": "functional"},
        {"code": "R-002", "text": "Retrieve the current weather for the city from the configured API.", "kind": "functional"},
        {"code": "R-003", "text": "Display the temperature, humidity and wind speed.", "kind": "functional"},
        {"code": "R-004", "text": "The weather API's base URL and key come from configuration, not code.", "kind": "non_functional"},
    ],
    "assumptions": ["Tests replace the weather API with fixed answers."],
    "ambiguities": ["Which weather provider to use."],
}
_PY = {"language": "python", "framework": None, "multi_file": True, "size": "small"}
PLAN = {
    "stack": "python-cli", "stack_rationale": "A small command-line program in Python.", "summary": "A CLI with a provider client and tests.",
    "tasks": [
        {"title": "Weather CLI", "description": "Parse the city, call the configured API and print the three values.", "capability": "code_generation",
         "requirement_refs": ["R-001", "R-002", "R-003", "R-004"], "features": _PY, "component": None, "depends_on": []},
        {"title": "Weather tests", "description": "Unit tests with the API replaced by fixed answers.", "capability": "test_generation",
         "requirement_refs": ["R-001", "R-003"], "features": _PY, "component": None, "depends_on": [1]},
    ],
}  # fmt: skip
BAD_PLAN = {**PLAN, "tasks": [{**PLAN["tasks"][1], "capability": "test_generation"}]}  # no code_generation, and R-002/R-004 uncovered


def write_env(tmp_path: Path, **extra: str) -> Path:
    values = {"OPENAI_API_KEY": FAKE_OPENAI_KEY, "OPENAI_MODEL": "test-model", "OPENAI_PRICE_INPUT_PER_MTOK": "2", "OPENAI_PRICE_OUTPUT_PER_MTOK": "10",
              "DATABASE_URL": f"sqlite:///{(tmp_path / 'data' / 'devpilot.db').as_posix()}", "WORKSPACES_DIR": str(tmp_path / "workspaces"),
              "MAX_TURNS": "50", "RUN_MAX_TOKENS": "1000000", "OPENAI_MAX_RETRIES": "5", "OPENAI_MAX_OUTPUT_TOKENS": "99999", "RUN_BUDGET_USD": "9"}  # fmt: skip
    values.update(extra)
    tmp_path.mkdir(parents=True, exist_ok=True)
    path = tmp_path / "test.env"
    path.write_text("".join(f"{k}={v}\n" for k, v in values.items() if v is not None), encoding="utf-8")
    return path


def plan(tmp_path: Path, *steps: Any, ask: Any = lambda _q: "", **env: str) -> tuple[dict[str, Any], FakeLLM, list[str]]:
    fake, lines = FakeLLM(*steps), []
    report = run_planning(planning_settings(write_env(tmp_path, **env)), fake, PROMPT, label="FakeLLM (scripted)", ask=ask, emit=lines.append, memory_probe=lambda: None)
    return report, fake, lines


def test_the_limits_are_forced_whatever_env_says(tmp_path: Path) -> None:
    s = planning_settings(write_env(tmp_path))
    assert (s.max_turns, s.run_max_tokens, s.openai_max_retries, s.openai_max_output_tokens, s.run_budget_usd) == (MAX_REQUESTS, TOKEN_LIMIT, 0, MAX_OUTPUT_TOKENS, RUN_CAP_USD)
    assert s.openai_model == "test-model" and s.cost_known and planning_dir(s) == tmp_path / "data" / "planning"


def test_the_prompt_reaches_the_orchestrator_and_the_run_stops_once_the_plan_is_accepted(tmp_path: Path) -> None:
    report, fake, _ = plan(tmp_path, call("submit_analysis", ANALYSIS), call("submit_plan", PLAN), call("submit_criteria", {"criteria": []}))
    assert f"<requirement>\n{PROMPT}\n</requirement>" in fake.requests[0].input[0]["content"]
    assert [sorted(t["name"] for t in r.tools) for r in fake.requests] == [["request_human_input", "submit_analysis"], ["request_human_input", "submit_plan"]]
    assert fake.remaining == 1  # acceptance criteria were never asked for
    assert report["verdict"] == PLAN_ACCEPTED and [t["title"] for t in report["plan"]["tasks"]] == ["Weather CLI", "Weather tests"]
    assert [r["code"] for r in report["analysis"]["requirements"]] == ["R-001", "R-002", "R-003", "R-004"]
    assert report["run"] | {"id": None} == {"id": None, "stopped_in": "DEFINING_CRITERIA", "pause_reason": "llm_stopped", "detail": PLAN_DONE, "outcome": None,
                                            "closed_as": "CANCELLED", "stopped_by_person": False}  # fmt: skip
    assert report["usage"]["requests_sent"] == 2 and report["usage"]["tokens_in"] == 2_000 and report["usage"]["spent_usd"] == pytest.approx(2 * (1_000 * 2 + 200 * 10) / 1e6)
    assert (report["generated"], report["executed"], report["deployed"]) == (False, False, False)


def test_the_plan_gate_still_judges_the_plan_and_a_rejection_gets_one_more_request(tmp_path: Path) -> None:
    report, _, _ = plan(tmp_path, call("submit_analysis", ANALYSIS), call("submit_plan", BAD_PLAN), call("submit_plan", PLAN))
    assert [(c["tool"], c["ok"]) for c in report["tool_calls"]] == [("submit_analysis", True), ("submit_plan", False), ("submit_plan", True)]
    problems = report["tool_calls"][1]["result"]["error"]["details"]
    assert "requirements not covered by any task: R-002, R-004" in problems and "at least one task must have capability code_generation" in problems
    assert report["verdict"] == PLAN_ACCEPTED and report["usage"]["requests_sent"] == MAX_REQUESTS


def test_never_more_than_three_requests_and_no_plan_is_reported_as_none(tmp_path: Path) -> None:
    steps = [call("submit_analysis", ANALYSIS)] + [call("submit_plan", {**BAD_PLAN, "summary": f"attempt {n}"}) for n in range(4)]
    report, fake, lines = plan(tmp_path, *steps)
    assert len(fake.requests) == MAX_REQUESTS and report["usage"]["requests_sent"] == MAX_REQUESTS
    assert report["verdict"] == NO_PLAN and report["run"]["outcome"] == "turn_limit" and report["run"]["closed_as"] == "STOPPED_BY_POLICY"
    render(report, lines.append)
    assert "NO ACCEPTED PLAN. The run stopped in STOPPED_BY_POLICY" in "\n".join(lines) and "Not assessed: there is no accepted plan." in lines


def test_the_guard_refuses_a_fourth_request_extra_tools_and_a_request_past_the_token_limit() -> None:
    fake = FakeLLM(*[call("submit_analysis", ANALYSIS)] * 4)
    guard = PlanningGuard(fake, max_output_tokens=MAX_OUTPUT_TOKENS, forbidden_values=[FAKE_OPENAI_KEY])

    def request(*tools: str, content: str = "plan") -> LLMRequest:
        return LLMRequest("i", [{"role": "user", "content": content}], [{"name": t} for t in tools], None)

    for _ in range(MAX_REQUESTS):
        guard.create(request("submit_analysis"))
    for refused, reason in ((request("submit_analysis"), "at most 3 requests"), (request("submit_plan", "search_repositories"), "only the analysis and planning tools"),
                            (request("submit_criteria"), PLAN_DONE)):  # fmt: skip
        with pytest.raises(LLMError, match=reason) as info:
            guard.create(refused)
        assert info.value.kind is LLMErrorKind.STOPPED
    fresh = PlanningGuard(fake, max_output_tokens=MAX_OUTPUT_TOKENS, forbidden_values=[FAKE_OPENAI_KEY])
    for refused, reason in ((request("submit_analysis", content="x" * TOKEN_LIMIT), "past the 90000"), (request("submit_analysis", content=f"key {FAKE_OPENAI_KEY}"), "secret")):
        with pytest.raises(LLMError, match=reason):
            fresh.create(refused)
    assert len(fake.requests) == MAX_REQUESTS and fresh.sent == 0


def test_a_request_that_could_pass_the_run_cap_is_never_sent(tmp_path: Path) -> None:
    report, fake, _ = plan(tmp_path, call("submit_analysis", ANALYSIS), OPENAI_PRICE_INPUT_PER_MTOK="500", OPENAI_PRICE_OUTPUT_PER_MTOK="500")
    assert fake.requests == [] and report["usage"]["requests_sent"] == 0 and report["usage"]["spent_usd"] == 0
    assert report["run"]["outcome"] == "budget_limit" and "RUN_BUDGET_USD=0.25" in report["run"]["detail"]


def test_a_question_waits_for_the_person_and_enter_stops_the_run(tmp_path: Path) -> None:
    question = call("request_human_input", {"question": "Which weather provider should be used?", "reason": "Providers differ in keys and fields."})
    report, fake, lines = plan(tmp_path, question, call("submit_analysis", ANALYSIS))
    assert "GPT asks: Which weather provider should be used?" in "\n".join(lines)
    assert len(fake.requests) == 1 and fake.remaining == 1 and report["run"]["stopped_by_person"] and report["run"]["closed_as"] == "CANCELLED"
    answers = iter(["Open-Meteo, with no key"])
    report, fake, _ = plan(tmp_path / "answered", question, call("submit_analysis", ANALYSIS), call("submit_plan", PLAN), ask=lambda _q: next(answers))
    assert "Open-Meteo, with no key" in json.dumps(fake.requests[1].input) and report["verdict"] == PLAN_ACCEPTED


def test_a_reply_without_a_tool_call_counts_as_a_request(tmp_path: Path) -> None:
    report, fake, _ = plan(tmp_path, text("Here is my plan in prose."), call("submit_analysis", ANALYSIS), call("submit_plan", PLAN))
    assert len(fake.requests) == 3 and report["verdict"] == PLAN_ACCEPTED and report["responses"][0]["text"] == "Here is my plan in prose."


def test_tool_eligibility_and_blockers_are_python_s_own_decisions(tmp_path: Path) -> None:
    report, _, lines = plan(tmp_path, call("submit_analysis", ANALYSIS), call("submit_plan", PLAN))
    rows = report["tool_eligibility"]
    assert [(r["task"], r["capability"], r["stack"]) for r in rows] == [(1, "code_generation", "python-cli"), (2, "test_generation", "python-cli")]
    assert all(r["verification_ready"] == readiness("python-cli")[0] and r["would_choose"] is None for r in rows)
    assert set(rows[0]["rejected"]) == {"opencode_server", "opencode_via_n8n", "gpt_writer"} and "not implemented" not in rows[0]["rejected"]["opencode_server"]
    assert "planned and has no implementation" in rows[0]["rejected"]["gpt_writer"]
    assert any(b.startswith("no coding tool is eligible for code_generation on python-cli: opencode_server:") for b in report["blockers"])
    assert "open question from the analysis: Which weather provider to use." in report["blockers"]
    render(report, lines.append, path=tmp_path / "r.json")
    out = "\n".join(lines)
    for heading in ("1. Your requirement", "2. GPT's responses (FakeLLM (scripted))", "3. Development plan", "4. Planned tasks", "5. Tool eligibility", "6. Security and runtime blockers",
                    "7. Tokens and estimated cost", "8. Final planning report"):  # fmt: skip
        assert f"== {heading}" in out
    assert "Nothing was generated, executed or deployed. Build mode was not started." in out and FAKE_OPENAI_KEY not in out + json.dumps(report)


def test_the_ledger_counts_a_run_at_its_cap_until_it_is_settled(tmp_path: Path) -> None:
    ledger = Ledger(tmp_path / "ledger.jsonl")
    assert ledger.spent() == 0
    ledger.reserve("a", 0.25)
    ledger.reserve("b", 0.25)
    ledger.settle("a", 0.031, 2)
    assert ledger.spent() == pytest.approx(0.281)
    ledger.path.write_text(ledger.path.read_text(encoding="utf-8") + "not json\n", encoding="utf-8")
    with pytest.raises(ValueError):
        ledger.spent()


class StandInClient:
    """Takes the OpenAI client's place in main(), to show which client live mode builds. Never touches the network."""

    built: list[Any] = []
    script: list[Any] = []

    def __init__(self, settings: Any) -> None:
        StandInClient.built.append(settings)
        self._fake = FakeLLM(*StandInClient.script)

    def create(self, request: LLMRequest) -> Any:
        return self._fake.create(request)


@pytest.fixture
def stand_in(monkeypatch: pytest.MonkeyPatch) -> type[StandInClient]:
    StandInClient.built, StandInClient.script = [], [call("submit_analysis", ANALYSIS), call("submit_plan", PLAN)]
    monkeypatch.setattr(plan_cli, "OpenAIResponsesClient", StandInClient)
    return StandInClient


def test_live_mode_builds_the_openai_client_settles_the_ledger_and_writes_the_report(tmp_path: Path, stand_in: type[StandInClient]) -> None:
    lines: list[str] = []
    asked: list[str] = []
    code = plan_cli.main(["--live", "--prompt", PROMPT], ask=lambda q: asked.append(q) or "yes", emit=lines.append, env_file=write_env(tmp_path))
    assert code == 0 and len(stand_in.built) == 1 and stand_in.built[0].openai_max_retries == 0 and stand_in.built[0].openai_max_output_tokens == MAX_OUTPUT_TOKENS
    assert "Type yes" in asked[0] and "At most 3 requests and $0.25" in asked[0]
    folder = tmp_path / "data" / "planning"
    (report_file,) = folder.glob("*.json")
    report = json.loads(report_file.read_text(encoding="utf-8"))
    assert report["gpt"] == "OpenAI Responses API, test-model" and report["verdict"] == PLAN_ACCEPTED and FAKE_OPENAI_KEY not in report_file.read_text(encoding="utf-8")
    entries = [json.loads(line) for line in (folder / "ledger.jsonl").read_text(encoding="utf-8").splitlines()]
    assert [e["event"] for e in entries] == ["reserved", "settled"] and entries[1]["requests"] == 2
    out = "\n".join(lines)
    assert f"of ${EXPERIMENT_CAP_USD:.2f} spent" in out and "FakeLLM" not in out and FAKE_OPENAI_KEY not in out


@pytest.mark.parametrize(
    ("env", "answers", "expected_code", "message"),
    [
        ({"OPENAI_API_KEY": None}, [], 1, "OPENAI_API_KEY is configured"),
        ({"OPENAI_PRICE_INPUT_PER_MTOK": None}, [], 1, "prices are set, so spending can be capped"),
        ({"OPENAI_BASE_URL": "https://proxy.example.com/v1"}, [], 1, "requests go to OpenAI's own endpoint"),
        ({}, ["no"], 0, "Nothing was sent."),
    ],
)
def test_live_mode_refuses_or_stops_before_any_request(tmp_path: Path, stand_in: type[StandInClient], env: dict[str, Any], answers: list[str], expected_code: int, message: str) -> None:
    lines: list[str] = []
    replies = iter(answers)
    code = plan_cli.main(["--live", "--prompt", PROMPT], ask=lambda _q: next(replies), emit=lines.append, env_file=write_env(tmp_path, **env))
    assert code == expected_code and message in "\n".join(lines) and stand_in.built == []
    assert not (tmp_path / "data" / "devpilot.db").exists()  # no project or run was created


def test_live_mode_refuses_once_the_experiment_budget_is_spent_or_unknown(tmp_path: Path, stand_in: type[StandInClient]) -> None:
    env = write_env(tmp_path)
    ledger = Ledger(tmp_path / "data" / "planning" / "ledger.jsonl")
    for n in range(6):
        ledger.reserve(f"run-{n}", 0.25)  # six runs that never settled: $1.50
    lines: list[str] = []
    assert plan_cli.main(["--live", "--prompt", PROMPT], ask=lambda _q: "yes", emit=lines.append, env_file=env) == 1
    assert "$1.5000 of the $1.50 experiment budget is used" in "\n".join(lines) and stand_in.built == []
    ledger.path.write_text("{broken\n", encoding="utf-8")
    assert plan_cli.main(["--live", "--prompt", PROMPT], ask=lambda _q: "yes", emit=lines.append, env_file=env) == 1
    assert "cannot be read" in lines[-1] and stand_in.built == []


def test_the_last_run_gets_only_what_is_left_of_the_experiment_budget(tmp_path: Path, stand_in: type[StandInClient]) -> None:
    ledger = Ledger(tmp_path / "data" / "planning" / "ledger.jsonl")
    for n in range(5):
        ledger.reserve(f"run-{n}", 0.25)
    ledger.reserve("run-5", 0.20)
    asked: list[str] = []
    env = write_env(tmp_path, OPENAI_PRICE_INPUT_PER_MTOK="0.1", OPENAI_PRICE_OUTPUT_PER_MTOK="0.5")  # cheap enough for a $0.05 run to send
    assert plan_cli.main(["--live", "--prompt", PROMPT], ask=lambda q: asked.append(q) or "yes", emit=lambda _l: None, env_file=env) == 0
    assert "At most 3 requests and $0.05" in asked[0] and ledger.spent() <= EXPERIMENT_CAP_USD


def test_exit_or_an_empty_prompt_sends_nothing(tmp_path: Path, stand_in: type[StandInClient]) -> None:
    for typed in ("exit", "", "  quit "):
        lines: list[str] = []
        assert plan_cli.main(["--live"], ask=lambda _q, t=typed: t, emit=lines.append, env_file=write_env(tmp_path)) == 0
        assert lines[-1] == "Nothing was sent."
    assert stand_in.built == []


def test_ctrl_c_at_a_question_closes_the_run_and_settles_the_ledger(tmp_path: Path, stand_in: type[StandInClient]) -> None:
    stand_in.script = [call("request_human_input", {"question": "Which provider?", "reason": "It decides the fields."}), call("submit_analysis", ANALYSIS)]

    def ask(question: str) -> str:
        if question.startswith("Your answer"):
            raise KeyboardInterrupt
        return "yes"

    lines: list[str] = []
    code = plan_cli.main(["--live", "--prompt", PROMPT], ask=ask, emit=lines.append, env_file=write_env(tmp_path))
    out = "\n".join(lines)
    assert code == 2 and "Interrupted: closing the run." in out and "NO ACCEPTED PLAN. The run stopped in AWAITING_APPROVAL" in out
    entries = [json.loads(line) for line in (tmp_path / "data" / "planning" / "ledger.jsonl").read_text(encoding="utf-8").splitlines()]
    assert [e["event"] for e in entries] == ["reserved", "settled"] and entries[1]["requests"] == 1
    (report_file,) = (tmp_path / "data" / "planning").glob("*.json")
    report = json.loads(report_file.read_text(encoding="utf-8"))
    assert report["run"]["stopped_by_person"] is True and report["run"]["closed_as"] == "CANCELLED"


def test_the_dry_run_sends_nothing_and_leaves_the_database_alone(tmp_path: Path, stand_in: type[StandInClient]) -> None:
    lines: list[str] = []
    assert plan_cli.main(["--prompt", PROMPT], emit=lines.append, env_file=write_env(tmp_path)) == 0
    out = "\n".join(lines)
    assert "DRY RUN: request 1 would be:" in out and PROMPT in out and "model:               test-model" in out and f"max_output_tokens:   {MAX_OUTPUT_TOKENS}" in out
    assert "at the configured prices" in out and "Ready for --live: yes. Nothing was sent." in out and FAKE_OPENAI_KEY not in out
    assert stand_in.built == [] and not (tmp_path / "data" / "devpilot.db").exists() and list((tmp_path / "data" / "planning").iterdir()) == []


def test_live_mode_has_no_path_to_a_scripted_model() -> None:
    tree = ast.parse(Path(plan_cli.__file__).read_text(encoding="utf-8"))
    modules = {n.module for n in ast.walk(tree) if isinstance(n, ast.ImportFrom)} | {a.name for n in ast.walk(tree) if isinstance(n, ast.Import) for a in n.names}
    assert "app.orchestrator.fake_llm" not in modules and "FakeLLM" not in Path(plan_cli.__file__).read_text(encoding="utf-8")
