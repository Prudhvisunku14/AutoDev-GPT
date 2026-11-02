"""Recovery inside a whole run: real orchestrator, tools, database, files, guard and selector.

GPT and the coding provider are scripted, and the sandbox is a fake returning real unittest output, so
no Docker, no model and no network are involved. **None of this is real Docker execution.**
"""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest

from app.coding.launcher import LaunchRequest
from app.coding.simulated import PROVIDER_NAME, ScriptedAttempt, SimulatedLauncher
from app.coding.types import AttemptPhase, CodingStatus, Termination
from app.db.models import RunState
from app.orchestrator.fake_llm import call
from app.sandbox.fake import FakeSandbox
from app.sandbox.spec import ExecResult, ExecSpec, ExecStatus
from app.selection.adaptive import AdaptiveSelector
from app.selection.catalog import CODING_CAPABILITIES, SIMULATED_PROFILE
from app.selection.history import record_attempt, repair_record, settle_verification
from app.selection.model import REAL, SIMULATED
from app.selection.selector import ProviderOption
from tests.unit.test_coding_tools import FAILING, GOOD, TESTS, Rig, failed, finish, generate, opening, passed, pause, submit
from tests.unit.test_selection_feedback import BUILD_CATEGORY, POLICY, adaptive, in_order, outcomes, rig

BROKEN = ScriptedAttempt({"half.py": "def half("}, CodingStatus.FAILED, "the engine reported an error")
TIMED_OUT = ScriptedAttempt({"half.py": "x"}, CodingStatus.TIMEOUT, "no result within 600 seconds")
SYNTAX_OUTPUT = '  File "/workspace/calculator.py", line 3\n    def add(a, b\n           ^\nSyntaxError: invalid syntax\n\n----\nRan 1 test in 0.0s\n\nFAILED (errors=1)\n'


def syntax(spec: ExecSpec) -> ExecResult:
    return ExecResult(ExecStatus.FAILED, spec.command, spec.container_name, exit_code=1, stderr=SYNTAX_OUTPUT, cleanup="removed")


def down(spec: ExecSpec) -> ExecResult:
    return ExecResult.unavailable(spec, "Docker is not reachable")


class Watched(SimulatedLauncher):
    """Records what each attempt was given, and can report a spend."""

    def __init__(self, script: list[ScriptedAttempt], *, cost: float | None = None, tokens: int = 0) -> None:
        super().__init__(script)
        self.timeouts: list[float] = []
        self.cost = cost
        self.tokens = tokens
        self.seen: list[str] = []

    def start(self, request: LaunchRequest) -> Any:
        session = super().start(request)
        if self.cost is not None or self.tokens:
            session.usage = {"cost_usd": self.cost, "all_known": True, "charged_tokens": self.tokens}
        real = session.provider.run_task

        def watch(task: Any, **kwargs: Any) -> Any:
            self.timeouts.append(task.timeout_seconds)
            self.seen.append(task.brief)
            return real(task, **kwargs)

        session.provider.run_task = watch  # type: ignore[method-assign]
        return session


class Blocked(SimulatedLauncher):
    def unavailable_reason(self) -> str | None:
        return "no approved runtime image"


class Unconfirmed:
    def terminate(self) -> Termination:
        return Termination(False, "docker did not answer")


class Stuck(SimulatedLauncher):
    def start(self, request: LaunchRequest) -> Any:
        session = super().start(request)
        session.writer = Unconfirmed()
        return session


def decisions(r: Rig) -> list[dict[str, Any]]:
    return r.h.events_of(r.run_id, "recovery.decided")


def actions(r: Rig) -> list[str]:
    return [d["action"] for d in decisions(r)]


def two_tools(first: SimulatedLauncher, second: SimulatedLauncher) -> AdaptiveSelector:
    return adaptive(first, second, names=("first", "second"))


# --- The ordinary path -------------------------------------------------------------------------------------


def test_a_clean_run_needs_no_recovery(tmp_path: Path) -> None:
    r = rig(tmp_path, *opening(), generate(1), generate(2), submit(), finish(), coding=[GOOD, TESTS])
    run = r.advance()
    assert run.state is RunState.COMPLETED and decisions(r) == [] and run.recovery_json is None and r.report()["recovery_outcomes"] == []


def test_a_syntax_error_is_repaired_and_the_repair_is_verified(tmp_path: Path) -> None:
    r = rig(tmp_path, *opening(), generate(1), generate(2), submit(), generate(1, "Fix it."), submit("Fixed."), finish(), coding=[GOOD, TESTS, GOOD], sandbox=FakeSandbox(in_order(syntax, passed)))
    run = r.advance()
    assert run.state is RunState.COMPLETED and run.recovery_json is None and "calculator.py" in r.h.workspace.list_files(r.project_id)
    decision = decisions(r)[0]
    assert decision["category"] == "SYNTAX_ERROR" and decision["action"] == "REPAIR" and decision["location"] == "calculator.py:3" and "SyntaxError: invalid syntax" in decision["evidence"]
    repair = outcomes(r)[-1]
    assert (repair.recovers_from, repair.recovery_action, repair.status) == ("SYNTAX_ERROR", "REPAIR", "VERIFIED")
    assert r.report()["recovery_outcomes"] == [{"category": "SYNTAX_ERROR", "action": "REPAIR", "resolved": True}]


def test_the_repair_brief_carries_the_diagnosis(tmp_path: Path) -> None:
    launcher = Watched([GOOD, TESTS, GOOD])
    r = Rig(tmp_path, *opening(), generate(1), generate(2), submit(), generate(1, "Fix it."), pause(), launcher=launcher, selector=adaptive(launcher), sandbox=FakeSandbox(syntax))
    r.advance()
    assert "Diagnosis" not in launcher.seen[0] and "Diagnosis: SYNTAX_ERROR" in launcher.seen[2] and "SyntaxError: invalid syntax" in launcher.seen[2]


def test_a_repair_that_does_not_help_is_recorded_as_not_resolved(tmp_path: Path) -> None:
    r = rig(tmp_path, *opening(), generate(1), generate(2), submit(), generate(1, "Fix it."), submit("Fixed."), pause(), coding=[GOOD, TESTS, GOOD], sandbox=FakeSandbox(in_order(failed, syntax)))
    r.advance()
    assert outcomes(r)[-1].status == "FAILED" and r.h.events_of(r.run_id, "recovery.outcome") == [{"category": "TEST_FAILURE", "action": "REPAIR", "resolved": False}]


# --- A failure that comes back ----------------------------------------------------------------------------------


def test_a_repeated_test_failure_stops_before_the_repair_limit_and_delivers_nothing(tmp_path: Path) -> None:
    r = rig(tmp_path, *opening(), generate(1), generate(2), submit(), generate(1, "Fix."), submit("Fixed."), generate(1, "Never runs."), coding=[GOOD, TESTS, GOOD, GOOD], sandbox=FakeSandbox(failed))
    run = r.advance()
    assert run.state is RunState.FAILED and run.outcome == "verification_failed" and "came back 2 times" in run.status_detail
    assert actions(r) == ["REPAIR", "STOP"] and len(r.sandbox.specs) == 2 and r.llm.remaining == 1 and r.h.settings.max_repair_attempts == 2
    assert r.untouched() and r.report()["delivered_files"] == [] and not r.candidates.exists(r.run_id)


def test_a_failure_that_changes_keeps_being_repaired(tmp_path: Path) -> None:
    r = rig(tmp_path, *opening(), generate(1), generate(2), submit(), generate(1, "Fix."), submit("Fixed."), generate(1, "Fix."), submit("Fixed."), finish(), coding=[GOOD, TESTS, GOOD, GOOD], sandbox=FakeSandbox(in_order(failed, syntax, passed)))
    assert r.advance().state is RunState.COMPLETED and actions(r) == ["REPAIR", "REPAIR"]


def test_a_repeated_failure_goes_to_another_tool_when_one_is_eligible(tmp_path: Path) -> None:
    first, second = SimulatedLauncher([GOOD, TESTS, GOOD]), SimulatedLauncher([GOOD])
    r = Rig(tmp_path, *opening(), generate(1), generate(2), submit(), generate(1, "Fix."), submit("Fixed."), generate(1, "Other."), submit("Fixed."), finish(), launcher=first, selector=two_tools(first, second), sandbox=FakeSandbox(in_order(failed, failed, passed)))
    assert r.advance().state is RunState.COMPLETED and actions(r) == ["REPAIR", "SWITCH_TOOL"]
    switched = decisions(r)[1]
    assert switched["tool"] == "second" and switched["real_switch"] is False and "simulated" in switched["reason"] and outcomes(r)[-1].tool_id == "second" and r.h.run(r.run_id).repairs == 2


def test_a_first_repair_stays_on_the_tool_even_when_another_exists(tmp_path: Path) -> None:
    first, second = SimulatedLauncher([GOOD, TESTS, GOOD]), SimulatedLauncher([GOOD])
    r = Rig(tmp_path, *opening(), generate(1), generate(2), submit(), generate(1, "Fix."), pause(), launcher=first, selector=two_tools(first, second), sandbox=FakeSandbox(failed))
    r.advance()
    assert actions(r) == ["REPAIR"] and r.output("generate_code", -1)["data"]["provider"] == "first"


def test_a_repair_verified_history_says_rarely_works_is_not_tried(tmp_path: Path) -> None:
    r = rig(tmp_path, *opening(), generate(1), generate(2), submit(), pause(), coding=[GOOD, TESTS], sandbox=FakeSandbox(failed))
    with r.h.sessions.begin() as s:
        for number in range(5):
            record_attempt(s, run_id=r.run_id, call_id=f"old{number}", task_key="earlier", tool_id=PROVIDER_NAME, category="cat", evidence=SIMULATED, status=CodingStatus.COMPLETED, kept=True, seconds=None, cost_usd=None, recovers_from="TEST_FAILURE", recovery_action="REPAIR")
        settle_verification(s, r.run_id, passed=False, failure="TEST_FAILURE")
    assert r.advance().state is RunState.FAILED and "0 of 5 verified repairs" in decisions(r)[0]["reason"]


@pytest.mark.parametrize(("evidence", "settled"), [(REAL, True), (SIMULATED, False)], ids=["real evidence for a simulated tool", "attempts nothing has judged"])
def test_only_verified_evidence_of_the_tools_own_kind_counts(tmp_path: Path, evidence: str, settled: bool) -> None:
    r = rig(tmp_path, *opening(), generate(1), generate(2), submit(), pause(), coding=[GOOD, TESTS], sandbox=FakeSandbox(failed))
    earlier = r.h.new_run()  # a verification settles its own run, so the waiting attempts must be another run's
    with r.h.sessions.begin() as s:
        for number in range(5):
            record_attempt(s, run_id=earlier, call_id=f"old{number}", task_key="earlier", tool_id=PROVIDER_NAME, category="cat", evidence=evidence, status=CodingStatus.COMPLETED, kept=True, seconds=None, cost_usd=None, recovers_from="TEST_FAILURE", recovery_action="REPAIR")
        if settled:
            settle_verification(s, earlier, passed=False, failure="TEST_FAILURE")
    r.advance()
    assert actions(r) == ["REPAIR"]


def test_a_repair_is_counted_for_its_tool_and_kind_only_once_judged(tmp_path: Path) -> None:
    r = rig(tmp_path, *opening(), generate(1), generate(2), submit(), generate(1, "Fix."), submit("Fixed."), finish(), coding=[GOOD, TESTS, GOOD], sandbox=FakeSandbox(in_order(failed, passed)))
    r.advance()
    with r.h.sessions() as s:
        assert repair_record(s, tool_id=PROVIDER_NAME, evidence=SIMULATED, category="TEST_FAILURE") == (1, 1)
        assert repair_record(s, tool_id=PROVIDER_NAME, evidence=REAL, category="TEST_FAILURE") == (0, 0) and repair_record(s, tool_id="other", evidence=SIMULATED, category="TEST_FAILURE") == (0, 0)


# --- A provider or sandbox that cannot do the work ---------------------------------------------------------------


def test_a_launch_refused_twice_stops_and_each_try_was_a_new_attempt(tmp_path: Path) -> None:
    r = rig(tmp_path, *opening(), generate(1), generate(1, "Again."), generate(1, "Never runs."), coding=[])
    run = r.advance()
    assert run.state is RunState.FAILED and run.outcome == "coding_failed" and actions(r) == ["RETRY", "STOP"] and r.attempts() == [AttemptPhase.ABANDONED, AttemptPhase.ABANDONED]
    assert r.llm.remaining == 1 and r.untouched()


def test_a_provider_that_comes_back_on_the_retry_succeeds(tmp_path: Path) -> None:
    r = rig(tmp_path, *opening(), generate(1), generate(1, "Again."), generate(2), submit(), finish(), coding=[BROKEN, GOOD, TESTS])
    run = r.advance()
    assert run.state is RunState.COMPLETED and actions(r) == ["RETRY"] and r.attempts()[0] is AttemptPhase.RESTORED and "half.py" not in r.h.workspace.list_files(r.project_id)
    assert outcomes(r)[1].recovery_action == "RETRY" and outcomes(r)[1].recovers_from == "TOOL_UNAVAILABLE"


def test_a_retry_sends_each_prompt_once_under_its_own_call(tmp_path: Path) -> None:
    from app.db.models import CodingAttempt

    launcher = Watched([BROKEN, GOOD, TESTS])
    r = Rig(tmp_path, *opening(), generate(1), generate(1, "Again."), generate(2), submit(), submit("Again."), finish(), launcher=launcher, selector=adaptive(launcher), sandbox=FakeSandbox(in_order(down, passed)))
    assert r.advance().state is RunState.COMPLETED and actions(r) == ["RETRY", "RETRY"]
    with r.h.sessions() as s:
        calls = [row.call_id for row in s.query(CodingAttempt).order_by(CodingAttempt.id)]
    assert len(calls) == len(set(calls)) == len(launcher.seen) == 3  # the tests' rerun sent no prompt


def test_no_eligible_tool_ends_the_run_at_once_and_starts_nothing(tmp_path: Path) -> None:
    launcher = Blocked([GOOD])
    r = Rig(tmp_path, *opening(), generate(1), pause(), launcher=launcher, selector=adaptive(launcher))
    run = r.advance()
    assert run.state is RunState.FAILED and run.outcome == "no_eligible_tool" and "no approved runtime image" in run.status_detail
    assert r.output("generate_code")["error"]["class"] == "POLICY_REFUSED" and r.attempts() == [] and outcomes(r) == [] and r.llm.remaining == 1 and r.untouched()
    assert actions(r) == ["STOP"] and r.report()["run"]["outcome"] == "no_eligible_tool"


def test_no_eligible_fallback_means_no_switch_is_claimed(tmp_path: Path) -> None:
    first, second = SimulatedLauncher([BROKEN, BROKEN]), Blocked([GOOD])
    r = Rig(tmp_path, *opening(), generate(1), generate(1, "Again."), generate(1, "Never runs."), launcher=first, selector=two_tools(first, second))
    run = r.advance()
    assert run.state is RunState.FAILED and actions(r) == ["RETRY", "STOP"] and all(d["tool"] is None for d in decisions(r)) and "no other eligible tool" in decisions(r)[1]["reason"]
    assert all(o.tool_id == "first" for o in outcomes(r)) and r.untouched()


def test_a_failed_attempt_is_the_same_failure_however_the_provider_words_it(tmp_path: Path) -> None:
    worded = [ScriptedAttempt({"half.py": "def half("}, CodingStatus.FAILED, f"the coding containers did not start: No such container: devpilot-opencode-{s}") for s in ("abfe12", "cdea34")]
    launcher = SimulatedLauncher([*worded, GOOD])
    r = Rig(tmp_path, *opening(), generate(1), generate(1, "Again."), generate(1, "Never runs."), launcher=launcher, selector=adaptive(launcher))
    run = r.advance()
    assert run.state is RunState.FAILED and actions(r) == ["RETRY", "STOP"] and len(outcomes(r)) == 2 and r.untouched()


def test_a_provider_failure_goes_to_an_eligible_tool(tmp_path: Path) -> None:
    first, second = SimulatedLauncher([BROKEN, TESTS]), SimulatedLauncher([GOOD])
    r = Rig(tmp_path, *opening(), generate(1), generate(1, "Again."), generate(2), submit(), finish(), launcher=first, selector=two_tools(first, second))
    assert r.advance().state is RunState.COMPLETED and actions(r) == ["SWITCH_TOOL"] and r.output("generate_code", 1)["data"]["provider"] == "second"


def test_a_sandbox_that_gave_no_answer_is_asked_once_more_and_the_code_is_left_alone(tmp_path: Path) -> None:
    r = rig(tmp_path, *opening(), generate(1), generate(2), submit(), submit("Again."), finish(), coding=[GOOD, TESTS], sandbox=FakeSandbox(in_order(down, passed)))
    run = r.advance()
    assert run.state is RunState.COMPLETED and actions(r) == ["RETRY"] and r.h.run(r.run_id).repairs == 0 and len(outcomes(r)) == 2
    assert r.output("submit_implementation", 0)["data"]["next_state"] == "VERIFYING" and all(o.status == "VERIFIED" for o in outcomes(r))


def test_a_sandbox_with_no_answer_teaches_nothing_about_the_tool(tmp_path: Path) -> None:
    r = rig(tmp_path, *opening(), generate(1), generate(2), submit(), pause(), coding=[GOOD, TESTS], sandbox=FakeSandbox(down))
    r.advance()
    assert all(o.status == "PENDING" for o in outcomes(r))


def test_a_sandbox_still_down_stops_the_run(tmp_path: Path) -> None:
    r = rig(tmp_path, *opening(), generate(1), generate(2), submit(), submit("Again."), submit("Never runs."), coding=[GOOD, TESTS], sandbox=FakeSandbox(down))
    run = r.advance()
    assert run.state is RunState.FAILED and run.outcome == "verification_failed" and actions(r) == ["RETRY", "STOP"] and len(r.sandbox.specs) == 2 and r.untouched()


def test_a_sandbox_still_down_stops_the_run_however_docker_words_it(tmp_path: Path) -> None:
    def gone(container: str) -> Any:
        return lambda spec: ExecResult.unavailable(spec, f"No such container: {container}")

    r = rig(tmp_path, *opening(), generate(1), generate(2), submit(), submit("Again."), submit("Never runs."), coding=[GOOD, TESTS], sandbox=FakeSandbox(in_order(gone("abfe12"), gone("cdea34"), gone("efab56"))))
    run = r.advance()
    assert run.state is RunState.FAILED and actions(r) == ["RETRY", "STOP"] and len(r.sandbox.specs) == 2


# --- Time -------------------------------------------------------------------------------------------------------


@pytest.mark.parametrize("normal", [600.0, 300.0])
def test_by_default_a_timed_out_attempt_is_never_given_longer(tmp_path: Path, normal: float) -> None:
    launcher = Watched([TIMED_OUT, GOOD])
    r = Rig(tmp_path, *opening(), generate(1), generate(1, "Again."), pause(), launcher=launcher, selector=adaptive(launcher), opencode_timeout_seconds=normal)
    r.advance()
    assert r.h.settings.recovery_max_attempt_timeout_seconds is None and actions(r) == ["RETRY"] and launcher.timeouts == [normal, normal]
    assert decisions(r)[0]["timeout_seconds"] is None and r.h.settings.run_wall_clock_seconds == 1_800


def test_a_timed_out_attempt_gets_more_time_for_that_task_only(tmp_path: Path) -> None:
    launcher = Watched([TIMED_OUT, GOOD, TESTS])
    r = Rig(tmp_path, *opening(), generate(1), generate(1, "Again."), generate(2), submit(), finish(), launcher=launcher, selector=adaptive(launcher), recovery_max_attempt_timeout_seconds=1_200.0)
    assert r.advance().state is RunState.COMPLETED and actions(r) == ["RECONFIGURE"] and launcher.timeouts == [600.0, 1_200.0, 600.0]
    assert decisions(r)[0]["timeout_seconds"] == 1_200.0 and r.attempts()[0] is AttemptPhase.RESTORED and outcomes(r)[1].recovery_action == "RECONFIGURE"


def test_the_time_limit_is_only_raised_as_far_as_is_approved(tmp_path: Path) -> None:
    launcher = Watched([TIMED_OUT, GOOD])
    r = Rig(tmp_path, *opening(), generate(1), generate(1, "Again."), pause(), launcher=launcher, selector=adaptive(launcher), recovery_max_attempt_timeout_seconds=750.0)
    r.advance()
    assert launcher.timeouts == [600.0, 750.0]


def test_with_no_approved_time_it_retries_at_the_same_limit(tmp_path: Path) -> None:
    launcher = Watched([TIMED_OUT, GOOD])
    r = Rig(tmp_path, *opening(), generate(1), generate(1, "Again."), pause(), launcher=launcher, selector=adaptive(launcher), recovery_max_attempt_timeout_seconds=600.0)
    r.advance()
    assert actions(r) == ["RETRY"] and launcher.timeouts == [600.0, 600.0]


def test_a_second_timeout_is_not_given_more_time(tmp_path: Path) -> None:
    launcher = Watched([TIMED_OUT, TIMED_OUT, GOOD])
    r = Rig(tmp_path, *opening(), generate(1), generate(1, "Again."), generate(1, "Never runs."), launcher=launcher, selector=adaptive(launcher), recovery_max_attempt_timeout_seconds=1_200.0)
    run = r.advance()
    assert run.state is RunState.FAILED and actions(r) == ["RECONFIGURE", "STOP"] and launcher.timeouts == [600.0, 1_200.0] and r.untouched()


def test_tests_that_time_out_keep_their_limit_and_go_to_repair(tmp_path: Path) -> None:
    def slow(spec: ExecSpec) -> ExecResult:
        return ExecResult(ExecStatus.TIMEOUT, spec.command, spec.container_name, detail="the command ran longer than 120 seconds and was stopped", cleanup="removed")

    r = rig(tmp_path, *opening(), generate(1), generate(2), submit(), generate(1, "Fix."), submit("Fixed."), pause(), coding=[GOOD, TESTS, GOOD], sandbox=FakeSandbox(in_order(slow, passed)))
    r.advance()
    assert actions(r) == ["REPAIR"] and decisions(r)[0]["category"] == "TIMEOUT" and {s.timeout_seconds for s in r.sandbox.specs} == {120.0}


def test_gpt_cannot_ask_for_more_time(tmp_path: Path) -> None:
    r = rig(tmp_path, *opening(), call("generate_code", {"task_order": 1, "instructions": "x", "timeout_seconds": 3_600}), coding=[GOOD])
    r.advance()
    assert r.output("generate_code")["error"]["class"] == "INVALID_ARGS"


def test_an_attempt_that_cannot_fit_in_the_time_left_is_not_started(tmp_path: Path) -> None:
    r = rig(tmp_path, *opening(), generate(1), generate(1, "Never runs."), coding=[BROKEN, GOOD], run_wall_clock_seconds=30.0)
    with r.h.sessions.begin() as s:  # the run's attempts have used its whole allowance, though the clock on this test has not
        record_attempt(s, run_id=r.run_id, call_id="old", task_key="earlier", tool_id=PROVIDER_NAME, category="cat", evidence=SIMULATED, status=CodingStatus.FAILED, kept=False, seconds=31.0, cost_usd=None)
    run = r.advance()
    assert run.state is RunState.FAILED and actions(r) == ["STOP"] and "the run has 0 left" in decisions(r)[0]["reason"] and len(r.attempts()) == 1


# --- Budget --------------------------------------------------------------------------------------------------------


def test_recovery_stops_when_the_budget_cannot_pay_for_another_attempt(tmp_path: Path) -> None:
    launcher = Watched([BROKEN, GOOD], cost=1.9)
    tool = ProviderOption(PROVIDER_NAME, launcher, CODING_CAPABILITIES, simulated=True, profile=replace(SIMULATED_PROFILE, min_cost_usd=0.5))
    r = Rig(tmp_path, *opening(), generate(1), generate(1, "Never runs."), launcher=launcher, selector=AdaptiveSelector([tool], policy=POLICY, memory_probe=lambda: None), openai_price_input_per_mtok=2.0, openai_price_output_per_mtok=10.0)
    run = r.advance()
    assert run.state is RunState.FAILED and run.outcome == "coding_failed" and actions(r) == ["STOP"] and "an attempt needs about" in decisions(r)[0]["reason"]
    assert len(launcher.timeouts) == 1 and r.untouched()


def test_recovery_stops_when_the_tokens_cannot_pay_for_another_attempt(tmp_path: Path) -> None:
    launcher = Watched([BROKEN, GOOD], tokens=499_500)
    tool = ProviderOption(PROVIDER_NAME, launcher, CODING_CAPABILITIES, simulated=True, profile=replace(SIMULATED_PROFILE, min_tokens=1_000))
    r = Rig(tmp_path, *opening(), generate(1), generate(1, "Never runs."), launcher=launcher, selector=AdaptiveSelector([tool], policy=POLICY, memory_probe=lambda: None), run_max_tokens=500_000)
    run = r.advance()
    assert run.state is RunState.FAILED and run.outcome == "coding_failed" and actions(r) == ["STOP"] and "at least 1000 tokens" in decisions(r)[0]["reason"]
    assert len(launcher.timeouts) == 1 and r.untouched()


def test_a_repair_stops_when_the_budget_cannot_pay_for_it(tmp_path: Path) -> None:
    launcher = Watched([GOOD, TESTS, GOOD], cost=0.9)
    tool = ProviderOption(PROVIDER_NAME, launcher, CODING_CAPABILITIES, simulated=True, profile=replace(SIMULATED_PROFILE, min_cost_usd=0.5))
    r = Rig(tmp_path, *opening(), generate(1), generate(2), submit(), generate(1, "Never runs."), launcher=launcher, selector=AdaptiveSelector([tool], policy=POLICY, memory_probe=lambda: None), sandbox=FakeSandbox(failed), openai_price_input_per_mtok=2.0, openai_price_output_per_mtok=10.0)
    run = r.advance()
    assert run.state is RunState.FAILED and run.outcome == "verification_failed" and actions(r) == ["STOP"] and "an attempt needs about" in decisions(r)[0]["reason"] and r.untouched()


# --- Unsafe states -----------------------------------------------------------------------------------------------------


def test_an_unsafe_file_change_is_undone_and_ends_the_run_on_every_tool(tmp_path: Path) -> None:
    first, second = SimulatedLauncher([ScriptedAttempt({"calculator.py": "x = 1\n", "opencode.json": "{}"})]), SimulatedLauncher([GOOD])
    r = Rig(tmp_path, *opening(), generate(1), generate(1, "Again."), launcher=first, selector=two_tools(first, second))
    run = r.advance()
    decision = decisions(r)[0]
    assert run.state is RunState.FAILED and decision["category"] == "SECURITY_VIOLATION" and decision["recovery_safe"] is False and decision["action"] == "STOP" and len(r.attempts()) == 1
    assert r.attempts() == [AttemptPhase.RESTORED] and r.untouched() and not r.candidates.exists(r.run_id)


def test_an_oversized_file_may_be_tried_again_and_the_audit_still_applies(tmp_path: Path) -> None:
    big = ScriptedAttempt({"big.py": "x" * 2_000})
    r = rig(tmp_path, *opening(), generate(1), generate(1, "Smaller."), generate(1, "Never runs."), coding=[big, big, big], workspace_max_file_bytes=1_000)
    run = r.advance()
    assert run.state is RunState.FAILED and actions(r) == ["RETRY", "STOP"] and decisions(r)[0]["category"] == "INVALID_CODE" and r.attempts() == [AttemptPhase.RESTORED, AttemptPhase.RESTORED]
    assert "big.py" not in r.h.workspace.list_files(r.project_id)


def test_an_unconfirmed_stop_fails_closed_and_keeps_everything_for_a_person(tmp_path: Path) -> None:
    first, second = Stuck([BROKEN]), SimulatedLauncher([GOOD])
    r = Rig(tmp_path, *opening(), generate(1), generate(1, "Again."), launcher=first, selector=two_tools(first, second))
    run = r.advance()
    decision = decisions(r)[0]
    assert run.state is RunState.FAILED and run.outcome == "quarantined" and decision["category"] == "AMBIGUOUS_STATE" and decision["action"] == "STOP" and decision["recovery_safe"] is False
    assert r.attempts() == [AttemptPhase.QUARANTINED] and (r.candidates.path(r.run_id) / "half.py").is_file() and r.untouched() and second._script == [GOOD]


def test_a_held_project_fails_closed_and_keeps_the_copy(tmp_path: Path) -> None:
    from app.db.models import CodingAttempt

    r = rig(tmp_path, *opening(), generate(1), generate(1, "Never runs."), coding=[GOOD, GOOD])
    r.candidates.open_or_create(r.run_id, r.original)
    with r.h.sessions.begin() as s:
        s.add(CodingAttempt(project_id=r.project_id, run_id=r.run_id, call_id="older", task_key="t", phase=AttemptPhase.QUARANTINED))
    run = r.advance()
    assert run.state is RunState.FAILED and decisions(r)[0]["category"] == "AMBIGUOUS_STATE" and actions(r) == ["STOP"] and r.candidates.exists(r.run_id) and r.llm.remaining == 1


def test_a_test_container_that_could_not_be_removed_stops_and_keeps_the_copy(tmp_path: Path) -> None:
    def stuck(spec: ExecSpec) -> ExecResult:
        return ExecResult(ExecStatus.FAILED, spec.command, spec.container_name, exit_code=1, stderr=FAILING, cleanup="failed")

    r = rig(tmp_path, *opening(), generate(1), generate(2), submit(), generate(1, "Never runs."), coding=[GOOD, TESTS], sandbox=FakeSandbox(stuck))
    run = r.advance()
    assert run.state is RunState.FAILED and decisions(r)[0]["category"] == "AMBIGUOUS_STATE" and r.candidates.exists(r.run_id) and r.untouched()


# --- Limits ------------------------------------------------------------------------------------------------------------


def test_the_retry_limit_ends_the_run_even_when_each_failure_differs(tmp_path: Path) -> None:
    script = [ScriptedAttempt({"half.py": "x"}, status, "it failed") for status in (CodingStatus.FAILED, CodingStatus.UNAVAILABLE, CodingStatus.FAILED)]  # wording alone is not a difference
    r = rig(tmp_path, *opening(), generate(1), generate(1, "b"), generate(1, "c"), generate(1, "Never runs."), coding=script)
    run = r.advance()
    assert run.state is RunState.FAILED and "limit of 3" in run.status_detail and actions(r) == ["RETRY", "RETRY", "STOP"] and len(r.attempts()) == 3 and r.llm.remaining == 1


def test_a_switch_counts_against_the_same_limit(tmp_path: Path) -> None:
    first, second = SimulatedLauncher([BROKEN]), SimulatedLauncher([ScriptedAttempt({"half.py": "x"}, CodingStatus.FAILED, "another")])
    r = Rig(tmp_path, *opening(), generate(1), generate(1, "b"), generate(1, "Never runs."), launcher=first, selector=two_tools(first, second), opencode_max_attempts_per_task=2)
    run = r.advance()
    assert run.state is RunState.FAILED and actions(r) == ["SWITCH_TOOL", "STOP"] and "limit of 2" in run.status_detail


# --- The baseline stays available ----------------------------------------------------------------------------------------


def test_the_bounded_policy_keeps_repairing_up_to_its_limit(tmp_path: Path) -> None:
    r = rig(tmp_path, *opening(), generate(1), generate(2), submit(), generate(1, "Fix."), submit("Fixed."), generate(1, "Fix."), submit("Fixed."), coding=[GOOD, TESTS, GOOD, GOOD], sandbox=FakeSandbox(failed), recovery_policy="bounded")
    run = r.advance()
    assert run.state is RunState.FAILED and actions(r) == ["REPAIR", "REPAIR", "STOP"] and len(r.sandbox.specs) == 3 and decisions(r)[0]["category"] == "" and run.recovery_json is None


def test_the_bounded_policy_does_not_end_the_run_when_no_tool_can_start(tmp_path: Path) -> None:
    launcher = Blocked([GOOD])
    r = Rig(tmp_path, *opening(), generate(1), pause(), launcher=launcher, selector=adaptive(launcher), recovery_policy="bounded")
    run = r.advance()
    assert not run.is_terminal and r.output("generate_code")["error"]["class"] == "POLICY_REFUSED" and actions(r) == []


def test_the_bounded_policy_retries_with_no_change_of_time_limit(tmp_path: Path) -> None:
    launcher = Watched([TIMED_OUT, GOOD])
    r = Rig(tmp_path, *opening(), generate(1), generate(1, "Again."), pause(), launcher=launcher, selector=adaptive(launcher), recovery_policy="bounded")
    r.advance()
    assert actions(r) == ["RETRY"] and launcher.timeouts == [600.0, 600.0]


# --- Records -----------------------------------------------------------------------------------------------------------


def test_the_categories_of_history_are_unchanged_for_reliability(tmp_path: Path) -> None:
    r = rig(tmp_path, *opening(), generate(1), generate(2), submit(), generate(1, "Fix."), submit("Fixed."), finish(), coding=[GOOD, TESTS, GOOD], sandbox=FakeSandbox(in_order(failed, passed)))
    r.advance()
    assert outcomes(r)[0].task_category == BUILD_CATEGORY and outcomes(r)[0].failure_category == "TEST_FAILURE" and outcomes(r)[-1].status == "VERIFIED"


def test_a_new_database_has_the_new_columns(tmp_path: Path) -> None:
    from sqlalchemy import inspect

    r = rig(tmp_path, *opening(), coding=[])
    with r.h.sessions() as s:
        inspector = inspect(s.get_bind())
        assert {"recovers_from", "recovery_action"} <= {c["name"] for c in inspector.get_columns("tool_outcomes")} and "recovery_json" in {c["name"] for c in inspector.get_columns("runs")}
