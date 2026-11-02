"""The adaptive policy inside a whole run: real orchestrator, tools, database and files.

GPT and the coding provider are scripted, and the sandbox is a fake returning real unittest output, so
no Docker, no model and no network are involved.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from sqlalchemy.orm import Session

from app.coding.simulated import PROVIDER_NAME, ScriptedAttempt, SimulatedLauncher
from app.coding.types import AttemptPhase, CodingStatus
from app.db.models import RunState, ToolOutcome
from app.sandbox.fake import FakeSandbox
from app.sandbox.spec import ExecResult, ExecSpec, ExecStatus
from app.selection.adaptive import AdaptiveSelector, EligibilityPolicy
from app.selection.catalog import CODING_CAPABILITIES, SIMULATED_PROFILE, opencode_profile
from app.selection.history import evidence_of, failed_tools, load_history, record_attempt, settle_verification
from app.selection.model import REAL, SIMULATED, ToolHistory, task_category
from app.selection.selector import ProviderOption
from tests.unit.test_coding_tools import GOOD, TESTS, Rig, failed, finish, generate, opening, passed, pause, submit

BUILD_CATEGORY = task_category("code_generation", "python-cli", "build")
TESTS_CATEGORY = task_category("test_generation", "python-cli", "build")
REPAIR_CATEGORY = task_category("code_generation", "python-cli", "repair")
POLICY = EligibilityPolicy(approved_environments=frozenset({"in-process", "docker-internal"}))


def adaptive(*launchers: Any, names: tuple[str, ...] = (PROVIDER_NAME,), memory: int | None = None) -> AdaptiveSelector:
    from dataclasses import replace

    options = [ProviderOption(name, launcher, CODING_CAPABILITIES, simulated=True, profile=replace(SIMULATED_PROFILE, tool_id=name)) for name, launcher in zip(names, launchers, strict=True)]
    return AdaptiveSelector(options, policy=POLICY, memory_probe=lambda: memory)


def rig(tmp_path: Path, *steps: Any, coding: list[ScriptedAttempt], sandbox: FakeSandbox | None = None, **overrides: Any) -> Rig:
    launcher = SimulatedLauncher(coding)
    return Rig(tmp_path, *steps, launcher=launcher, selector=adaptive(launcher), sandbox=sandbox, **overrides)


def outcomes(r: Rig) -> list[ToolOutcome]:
    with r.h.sessions() as s:
        return list(s.query(ToolOutcome).order_by(ToolOutcome.id))


def history(r: Rig, category: str = BUILD_CATEGORY) -> dict[tuple[str, str], ToolHistory]:
    with r.h.sessions() as s:
        return load_history(s, category)


def in_order(*handlers: Any) -> Any:
    """A sandbox that answers with each handler in turn."""
    left = list(handlers)
    return lambda spec: left.pop(0)(spec)


def full_run(*extra: Any) -> tuple[Any, ...]:
    return (*opening(), generate(1), generate(2), submit(), finish(), *extra)


# --- The workflow is the same as before ------------------------------------------------------------------


def test_a_run_completes_through_the_adaptive_policy(tmp_path: Path) -> None:
    r = rig(tmp_path, *full_run(), coding=[GOOD, TESTS])
    run = r.advance()
    assert run.state is RunState.COMPLETED and run.outcome == "completed" and r.attempts() == [AttemptPhase.KEPT, AttemptPhase.KEPT]
    assert sorted(r.h.workspace.list_files(r.project_id)) == ["README.md", "calculator.py", "test_calculator.py"]


def test_the_fixed_policy_gives_the_same_run(tmp_path: Path) -> None:
    (tmp_path / "a").mkdir()
    (tmp_path / "b").mkdir()
    fixed = Rig(tmp_path / "a", *full_run(), coding=[GOOD, TESTS])
    chosen = rig(tmp_path / "b", *full_run(), coding=[GOOD, TESTS])
    for one in (fixed, chosen):
        one.advance()
    assert fixed.h.run(fixed.run_id).state is chosen.h.run(chosen.run_id).state is RunState.COMPLETED
    assert sorted(fixed.h.workspace.list_files(fixed.project_id)) == sorted(chosen.h.workspace.list_files(chosen.project_id))


def test_gpt_gets_structured_feedback_about_the_choice(tmp_path: Path) -> None:
    r = rig(tmp_path, *opening(), generate(1), coding=[GOOD])
    r.advance()
    selection = r.output("generate_code")["data"]["selection"]
    assert selection["policy"] == "adaptive" and selection["compared"] is False and "nothing was compared" in selection["explanation"]


def test_the_fixed_policy_says_it_is_fixed(tmp_path: Path) -> None:
    r = Rig(tmp_path, *opening(), generate(1), coding=[GOOD])
    r.advance()
    assert r.output("generate_code")["data"]["selection"] == {"policy": "fixed"}


def test_the_ranking_is_kept_in_the_run_record(tmp_path: Path) -> None:
    r = rig(tmp_path, *opening(), generate(1), coding=[GOOD])
    r.advance()
    payload = r.h.events_of(r.run_id, "selection.chosen")[0]
    assert payload["provider"] == PROVIDER_NAME and payload["policy"] == "adaptive" and payload["category"] == BUILD_CATEGORY
    assert set(payload["ranking"][0]["breakdown"]) == {"capability", "reliability", "quality", "latency", "cost", "resource"}


def test_the_report_still_says_the_provider_was_simulated(tmp_path: Path) -> None:
    r = rig(tmp_path, *full_run(), coding=[GOOD, TESTS])
    r.advance()
    report = r.report()
    assert "coding provider" in report["provenance"]["simulated"] and report["selection"][0]["policy"] == "adaptive"


def test_gpt_cannot_name_a_provider(tmp_path: Path) -> None:
    from app.orchestrator.fake_llm import call

    r = rig(tmp_path, *opening(), call("generate_code", {"task_order": 1, "instructions": "x", "provider": "other"}), coding=[GOOD])
    r.advance()
    assert r.output("generate_code")["error"]["class"] == "INVALID_ARGS"


# --- Learning only from verified results -------------------------------------------------------------------


def test_a_finished_attempt_alone_proves_nothing(tmp_path: Path) -> None:
    r = rig(tmp_path, *opening(), generate(1), pause(), coding=[GOOD])
    r.advance()
    assert [o.status for o in outcomes(r)] == ["PENDING"] and history(r) == {}


def test_passing_tests_verify_every_waiting_attempt(tmp_path: Path) -> None:
    r = rig(tmp_path, *full_run(), coding=[GOOD, TESTS])
    r.advance()
    assert [o.status for o in outcomes(r)] == ["VERIFIED", "VERIFIED"]
    found = history(r)[(PROVIDER_NAME, SIMULATED)]
    assert found.attempts == found.successes == 1 and found.last_outcome == "VERIFIED" and found.last_success is not None
    assert history(r, TESTS_CATEGORY)[(PROVIDER_NAME, SIMULATED)].successes == 1  # writing tests is other work, so it is counted apart


def test_failing_tests_fail_the_attempt_and_name_the_failure(tmp_path: Path) -> None:
    sandbox = FakeSandbox(failed)
    r = rig(tmp_path, *opening(), generate(1), generate(2), submit(), pause(), coding=[GOOD, TESTS], sandbox=sandbox, max_repair_attempts=1)
    r.advance()
    assert {(o.status, o.failure_category) for o in outcomes(r)} == {("FAILED", "TEST_FAILURE")}
    found = history(r)[(PROVIDER_NAME, SIMULATED)]
    assert found.successes == 0 and found.failures == {"TEST_FAILURE": 1} and found.last_outcome == "TEST_FAILURE" and found.last_success is None


def test_a_repair_is_judged_on_its_own_and_kept_apart(tmp_path: Path) -> None:
    sandbox = FakeSandbox(in_order(failed, passed))
    r = rig(tmp_path, *opening(), generate(1), generate(2), submit(), generate(1, "Fix it."), submit(), finish(), coding=[GOOD, TESTS, GOOD], sandbox=sandbox, max_repair_attempts=1)
    run = r.advance()
    assert run.state is RunState.COMPLETED and [o.status for o in outcomes(r)] == ["FAILED", "FAILED", "VERIFIED"]
    assert history(r)[(PROVIDER_NAME, SIMULATED)].successes == 0 and history(r, REPAIR_CATEGORY)[(PROVIDER_NAME, SIMULATED)].successes == 1


def test_a_sandbox_that_gave_no_answer_teaches_nothing(tmp_path: Path) -> None:
    def gone(spec: ExecSpec) -> ExecResult:
        return ExecResult.unavailable(spec, "docker is down")

    r = rig(tmp_path, *opening(), generate(1), generate(2), submit(), pause(), coding=[GOOD, TESTS], sandbox=FakeSandbox(gone))
    r.advance()
    assert [o.status for o in outcomes(r)] == ["PENDING", "PENDING"] and history(r) == {}


def test_a_failed_attempt_is_settled_at_once(tmp_path: Path) -> None:
    broken = ScriptedAttempt({"half.py": "def half("}, CodingStatus.FAILED, "simulated failure")
    r = rig(tmp_path, *opening(), generate(1), pause(), coding=[broken])
    r.advance()
    only = outcomes(r)[0]
    assert (only.status, only.failure_category) == ("FAILED", "CODING_FAILED") and only.settled_at is not None
    assert r.attempts() == [AttemptPhase.RESTORED]  # the guard undid it, as before


@pytest.mark.parametrize("status", [CodingStatus.CANCELLED, CodingStatus.BUSY])
def test_a_stopped_or_refused_attempt_says_nothing_about_the_tool(tmp_path: Path, status: CodingStatus) -> None:
    r = rig(tmp_path, *opening(), generate(1), pause(), coding=[ScriptedAttempt({}, status, "stopped")])
    r.advance()
    assert outcomes(r) == []


def test_the_attempt_records_how_long_it_took(tmp_path: Path) -> None:
    r = rig(tmp_path, *opening(), generate(1), pause(), coding=[GOOD])
    r.advance()
    assert outcomes(r)[0].duration_ms is not None


def test_cost_is_recorded_when_it_is_known(tmp_path: Path) -> None:
    class Metered(SimulatedLauncher):
        def start(self, request: Any) -> Any:
            session = super().start(request)
            session.usage = {"requests": 3, "charged_tokens": 5_000, "cost_usd": 0.25, "all_known": True}
            return session

    launcher = Metered([GOOD])
    r = Rig(tmp_path, *opening(), generate(1), pause(), launcher=launcher, selector=adaptive(launcher), openai_price_input_per_mtok=2.0, openai_price_output_per_mtok=10.0)
    r.advance()
    assert outcomes(r)[0].cost_usd == 0.25


def test_a_repair_knows_the_tool_failed_the_tests(tmp_path: Path) -> None:
    r = rig(tmp_path, *opening(), generate(1), generate(2), submit(), generate(1, "Once more."), pause(), coding=[GOOD, TESTS, GOOD], sandbox=FakeSandbox(failed), max_repair_attempts=1)
    r.advance()
    payloads = r.h.events_of(r.run_id, "selection.chosen")
    assert payloads[0]["ranking"][0]["attempts"] == 0 and payloads[-1]["category"] == REPAIR_CATEGORY and payloads[-1]["fallback"] == "retried_same"
    assert len(r.h.events_of(r.run_id, "selection.learned")) == 1


def test_verified_history_changes_a_later_choice(tmp_path: Path) -> None:
    """With no history the tools tie and the configured order picks `first`. Both results are from another task, so this is history, not a retry."""
    first, second = SimulatedLauncher([GOOD]), SimulatedLauncher([GOOD])
    r = Rig(tmp_path, *opening(), generate(1), pause(), launcher=first, selector=adaptive(first, second, names=("first", "second")))
    with r.h.sessions.begin() as s:
        for tool, verdict in (("first", False), ("second", True)):
            record_attempt(s, run_id=r.run_id, call_id=tool, task_key="an earlier task", tool_id=tool, category=BUILD_CATEGORY, evidence=SIMULATED, status=CodingStatus.COMPLETED, kept=True, seconds=None, cost_usd=None)
            settle_verification(s, r.run_id, passed=verdict, failure="TEST_FAILURE")
    r.advance()
    data = r.output("generate_code")["data"]
    assert data["provider"] == "second" and data["selection"]["fallback"] is None
    assert [c["attempts"] for c in r.h.events_of(r.run_id, "selection.chosen")[0]["ranking"]] == [1, 1]


# --- Evidence stays apart ----------------------------------------------------------------------------------


def test_only_a_real_provider_judged_by_the_real_sandbox_is_real_evidence() -> None:
    class DockerSandbox:
        pass

    class FakeBox:
        pass

    assert evidence_of(simulated=False, sandbox=DockerSandbox()) == REAL
    assert evidence_of(simulated=True, sandbox=DockerSandbox()) == SIMULATED
    assert evidence_of(simulated=False, sandbox=FakeBox()) == SIMULATED


def test_a_real_provider_judged_by_a_fake_sandbox_never_becomes_real_history(tmp_path: Path) -> None:
    launcher = SimulatedLauncher([GOOD, TESTS])
    settings_profile = opencode_profile(type("S", (), {"openai_price_input_per_mtok": None, "openai_price_output_per_mtok": None, "gateway_max_output_tokens": 8_000})())  # type: ignore[arg-type]
    from dataclasses import replace

    tool = ProviderOption("opencode_server", launcher, CODING_CAPABILITIES, simulated=False, profile=replace(settings_profile, credentials=frozenset(), network=frozenset()))
    r = Rig(tmp_path, *full_run(), launcher=launcher, selector=AdaptiveSelector([tool], policy=POLICY, memory_probe=lambda: None))
    r.advance()
    assert {o.evidence for o in outcomes(r)} == {SIMULATED} and (("opencode_server", REAL) not in history(r))


def test_the_two_kinds_of_evidence_are_counted_separately(tmp_path: Path) -> None:
    r = rig(tmp_path, *opening(), coding=[])
    with r.h.sessions.begin() as s:
        for number, evidence in enumerate((REAL, SIMULATED, SIMULATED)):
            record_attempt(s, run_id=r.run_id, call_id=f"c{number}", task_key="k", tool_id="t", category="cat", evidence=evidence, status=CodingStatus.COMPLETED, kept=True, seconds=1.0, cost_usd=0.1)
        settle_verification(s, r.run_id, passed=True)
    with r.h.sessions() as s:
        found = load_history(s, "cat")
    assert found[("t", REAL)].attempts == 1 and found[("t", SIMULATED)].attempts == 2


# --- Statistics --------------------------------------------------------------------------------------------


def _attempt(s: Session, r: Rig, call: str, *, kept: bool = True, status: CodingStatus = CodingStatus.COMPLETED, seconds: float | None = 2.0, cost: float | None = 0.5) -> None:
    record_attempt(s, run_id=r.run_id, call_id=call, task_key=f"{r.run_id}:1", tool_id="t", category="cat", evidence=SIMULATED, status=status, kept=kept, seconds=seconds, cost_usd=cost)


def test_the_statistics_add_up(tmp_path: Path) -> None:
    r = rig(tmp_path, *opening(), coding=[])
    with r.h.sessions.begin() as s:
        _attempt(s, r, "a")
        _attempt(s, r, "b", seconds=4.0)
        settle_verification(s, r.run_id, passed=True)
        _attempt(s, r, "c", kept=False, status=CodingStatus.TIMEOUT, cost=None)
    with r.h.sessions() as s:
        found = load_history(s, "cat")[("t", SIMULATED)]
    assert (found.attempts, found.successes) == (3, 2) and found.failures == {"CODING_TIMEOUT": 1}
    assert found.seconds_total == pytest.approx(8.0) and found.seconds_samples == 3 and found.cost_total == pytest.approx(1.0) and found.cost_samples == 2


def test_an_attempt_cannot_be_counted_twice(tmp_path: Path) -> None:
    r = rig(tmp_path, *opening(), coding=[])
    with r.h.sessions.begin() as s:
        _attempt(s, r, "same")
        _attempt(s, r, "same")
        assert s.query(ToolOutcome).count() == 1


def test_a_settled_attempt_is_not_settled_again(tmp_path: Path) -> None:
    r = rig(tmp_path, *opening(), coding=[])
    with r.h.sessions.begin() as s:
        _attempt(s, r, "a")
        assert settle_verification(s, r.run_id, passed=False, failure="TEST_FAILURE") == 1
        assert settle_verification(s, r.run_id, passed=True) == 0
    assert outcomes(r)[0].status == "FAILED"


def test_a_verdict_settles_only_its_own_run(tmp_path: Path) -> None:
    r = rig(tmp_path, *opening(), coding=[])
    other = r.h.new_run()
    with r.h.sessions.begin() as s:
        record_attempt(s, run_id=other, call_id="theirs", task_key="k", tool_id="t", category="cat", evidence=SIMULATED, status=CodingStatus.COMPLETED, kept=True, seconds=None, cost_usd=None)
        assert settle_verification(s, r.run_id, passed=True) == 0
    assert outcomes(r)[0].status == "PENDING"


def test_history_is_read_per_category(tmp_path: Path) -> None:
    r = rig(tmp_path, *opening(), coding=[])
    with r.h.sessions.begin() as s:
        _attempt(s, r, "a")
        settle_verification(s, r.run_id, passed=True)
    with r.h.sessions() as s:
        assert load_history(s, "cat") != {} and load_history(s, "another") == {}


# --- Fallback, under the existing safety rules ---------------------------------------------------------------


def test_after_a_failure_the_retry_goes_to_the_other_tool_and_the_copy_was_restored(tmp_path: Path) -> None:
    broken = ScriptedAttempt({"half.py": "def half("}, CodingStatus.FAILED, "simulated failure")
    first, second = SimulatedLauncher([broken, TESTS]), SimulatedLauncher([GOOD])  # task 2 is a new task, so the first tool may take it
    chooser = adaptive(first, second, names=("first", "second"))
    r = Rig(tmp_path, *opening(), generate(1), generate(1, "Again."), generate(2), submit(), finish(), launcher=first, selector=chooser)
    run = r.advance()
    assert run.state is RunState.COMPLETED and r.attempts()[0] is AttemptPhase.RESTORED and "half.py" not in r.h.workspace.list_files(r.project_id)
    assert r.output("generate_code", 0)["data"]["recovery"]["action"] == "RETRY"
    retry = r.output("generate_code", 1)["data"]
    assert retry["provider"] == "second" and retry["selection"]["fallback"] == "switched"


def test_with_one_tool_the_retry_stays_on_it_under_the_attempt_limit(tmp_path: Path) -> None:
    broken = ScriptedAttempt({"half.py": "x"}, CodingStatus.FAILED, "simulated failure")
    r = rig(tmp_path, *opening(), generate(1), generate(1, "Again."), generate(1, "Again."), pause(), coding=[broken, broken, broken], opencode_max_attempts_per_task=2)
    run = r.advance()
    assert run.state is RunState.FAILED and run.outcome == "coding_failed"  # the existing limit still ends it
    assert r.output("generate_code", 1)["data"]["selection"]["fallback"] == "retried_same"


def test_a_policy_breach_is_not_retried_on_any_tool(tmp_path: Path) -> None:
    breach = ScriptedAttempt({"opencode.json": "{}"})
    first, second = SimulatedLauncher([breach]), SimulatedLauncher([GOOD])
    r = Rig(tmp_path, *opening(), generate(1), generate(1, "Again."), pause(), launcher=first, selector=adaptive(first, second, names=("first", "second")))
    run = r.advance()
    assert run.state is RunState.FAILED and r.output("generate_code", 0)["data"]["recovery"]["action"] == "STOP" and len(r.attempts()) == 1


def test_a_failed_tool_is_remembered_per_task(tmp_path: Path) -> None:
    r = rig(tmp_path, *opening(), coding=[])
    with r.h.sessions.begin() as s:
        _attempt(s, r, "a", kept=False, status=CodingStatus.FAILED)
    with r.h.sessions() as s:
        assert failed_tools(s, f"{r.run_id}:1") == {"t"} and failed_tools(s, f"{r.run_id}:2") == frozenset()


def test_only_a_failure_marks_a_tool_as_failed(tmp_path: Path) -> None:
    r = rig(tmp_path, *opening(), coding=[])
    with r.h.sessions.begin() as s:
        _attempt(s, r, "waiting")
        assert failed_tools(s, f"{r.run_id}:1") == frozenset()
        settle_verification(s, r.run_id, passed=True)
        assert failed_tools(s, f"{r.run_id}:1") == frozenset()


def test_when_nothing_is_eligible_gpt_is_told_why_and_nothing_starts(tmp_path: Path) -> None:
    class Blocked(SimulatedLauncher):
        def unavailable_reason(self) -> str | None:
            return "no approved runtime image"

    launcher = Blocked([GOOD])
    r = Rig(tmp_path, *opening(), generate(1), pause(), launcher=launcher, selector=adaptive(launcher))
    r.advance()
    output = r.output("generate_code")
    assert output["error"]["class"] == "POLICY_REFUSED" and "no approved runtime image" in output["error"]["summary"] and r.attempts() == [] and outcomes(r) == []


def test_low_memory_refuses_the_attempt_before_anything_starts(tmp_path: Path) -> None:
    launcher = SimulatedLauncher([GOOD])
    r = Rig(tmp_path, *opening(), generate(1), pause(), launcher=launcher, selector=adaptive(launcher, memory=1))
    r.advance()
    assert "the machine can commit 1 MB" in r.output("generate_code")["error"]["summary"] and r.attempts() == []


def test_a_tool_the_budget_cannot_start_is_refused_before_anything_starts(tmp_path: Path) -> None:
    from dataclasses import replace

    launcher = SimulatedLauncher([GOOD])
    tool = ProviderOption(PROVIDER_NAME, launcher, CODING_CAPABILITIES, simulated=True, profile=replace(SIMULATED_PROFILE, min_cost_usd=50.0))
    r = Rig(tmp_path, *opening(), generate(1), pause(), launcher=launcher, selector=AdaptiveSelector([tool], policy=POLICY, memory_probe=lambda: None), openai_price_input_per_mtok=2.0, openai_price_output_per_mtok=10.0)
    r.advance()
    output = r.output("generate_code")
    assert output["error"]["class"] == "POLICY_REFUSED" and "an attempt needs about" in output["error"]["summary"] and r.attempts() == [] and outcomes(r) == []


def test_a_tool_the_remaining_tokens_cannot_pay_for_is_refused_before_anything_starts(tmp_path: Path) -> None:
    """It needs exactly the run's limit, so it fits only if nothing spent so far is counted."""
    from dataclasses import replace

    launcher = SimulatedLauncher([GOOD])
    tool = ProviderOption(PROVIDER_NAME, launcher, CODING_CAPABILITIES, simulated=True, profile=replace(SIMULATED_PROFILE, min_tokens=500_000))
    r = Rig(tmp_path, *opening(), generate(1), pause(), launcher=launcher, selector=AdaptiveSelector([tool], policy=POLICY, memory_probe=lambda: None), run_max_tokens=500_000)
    r.advance()
    output = r.output("generate_code")
    assert output["error"]["class"] == "POLICY_REFUSED" and "at least 500000 tokens" in output["error"]["summary"] and r.attempts() == [] and outcomes(r) == []


def test_the_run_budget_guard_still_stops_gpt_first(tmp_path: Path) -> None:
    r = rig(tmp_path, *opening(), generate(1), coding=[GOOD], openai_price_input_per_mtok=2.0, openai_price_output_per_mtok=10.0, run_budget_usd=0.001)
    run = r.advance()
    assert run.state is RunState.STOPPED_BY_POLICY and r.attempts() == [] and r.h.events_of(r.run_id, "selection.chosen") == []


def test_a_new_database_gets_the_table(tmp_path: Path) -> None:
    from sqlalchemy import inspect

    r = rig(tmp_path, *opening(), coding=[])
    with r.h.sessions() as s:
        assert "tool_outcomes" in inspect(s.get_bind()).get_table_names()
