"""The recovery decision, on its own: no database, no Docker, no model, no network."""

from __future__ import annotations

import json
from dataclasses import replace
from typing import Any

import pytest

from app.coding.types import CodingStatus
from app.recovery.diagnosis import CODING, VERIFICATION, AttemptSignal, Confidence, Diagnosis, FailureCategory, classify_attempt
from app.recovery.engine import Action, decide_coding, decide_verification
from app.recovery.policy import RecoveryConfig, RecoveryMemory, RecoveryState, ToolOptions, decide, longer_timeout

ANOTHER = ToolOptions(alternatives=("backup",), real_alternative=True)
FIX = (Action.REPAIR, Action.SWITCH_TOOL, Action.STOP)


def state(**changes: Any) -> RecoveryState:
    return RecoveryState(**{"attempts_used": 1, "max_attempts": 3, "repairs_used": 0, "max_repairs": 2, **changes})


def failed_tests(category: FailureCategory = FailureCategory.TEST_FAILURE, actions: tuple[Action, ...] = FIX) -> Diagnosis:
    return Diagnosis(category, category.value, VERIFICATION, Confidence.HIGH, ("evidence",), actions, "same")


def attempt_failed(status: CodingStatus = CodingStatus.FAILED, phase: str = "RESTORED", detail: str = "it failed") -> Diagnosis:
    return classify_attempt(AttemptSignal(1, status, phase, detail))


def sandbox_down() -> Diagnosis:
    return Diagnosis(FailureCategory.TOOL_UNAVAILABLE, "ENVIRONMENT", VERIFICATION, Confidence.HIGH, ("down",), (Action.RETRY, Action.STOP), "same")


# --- Safety comes first --------------------------------------------------------------------------------

UNSAFE = [
    pytest.param(attempt_failed(CodingStatus.POLICY_VIOLATION), id="policy"),
    pytest.param(attempt_failed(CodingStatus.AUTH), id="auth"),
    pytest.param(attempt_failed(CodingStatus.FAILED, phase="QUARANTINED"), id="quarantined"),
    pytest.param(attempt_failed(CodingStatus.PROTOCOL), id="protocol"),
]


@pytest.mark.parametrize("diagnosis", UNSAFE)
@pytest.mark.parametrize("tools", [ToolOptions(), ANOTHER], ids=["alone", "with another tool"])
def test_an_unsafe_failure_stops_however_much_recovery_is_left(diagnosis: Diagnosis, tools: ToolOptions) -> None:
    decision = decide(diagnosis, state(attempts_used=0, tools=tools, next_timeout=9_999.0, repairs_judged=100, repairs_verified=100))
    assert decision.action is Action.STOP and decision.recovery_safe is False and decision.tool is None


def test_a_forged_diagnosis_that_offers_a_retry_on_an_unsafe_category_still_stops() -> None:
    forged = replace(attempt_failed(CodingStatus.POLICY_VIOLATION), actions=(Action.RETRY, Action.SWITCH_TOOL, Action.STOP))
    assert decide(forged, state(tools=ANOTHER)).action is Action.STOP


# --- The bounded limits still hold ------------------------------------------------------------------------


@pytest.mark.parametrize("status", [CodingStatus.FAILED, CodingStatus.TIMEOUT, CodingStatus.UNAVAILABLE, CodingStatus.CANCELLED])
@pytest.mark.parametrize("used", range(7))
def test_no_more_attempts_are_taken_than_the_bounded_policy_allows(status: CodingStatus, used: int) -> None:
    decision = decide(attempt_failed(status), state(attempts_used=used, tools=ANOTHER, next_timeout=1_200.0))
    baseline = decide_coding(status, quarantined=False, attempts_used=used, max_attempts=3)
    if baseline.action is Action.STOP:
        assert decision.action is Action.STOP and "limit of 3 attempts" in decision.reason


@pytest.mark.parametrize("used", range(5))
@pytest.mark.parametrize("tools", [ToolOptions(), ANOTHER], ids=["alone", "with another tool"])
def test_no_more_repairs_are_taken_than_the_bounded_policy_allows(used: int, tools: ToolOptions) -> None:
    decision = decide(failed_tests(), state(repairs_used=used, tools=tools))
    if decide_verification("TEST_FAILURE", repairs_used=used, max_repairs=2).action is Action.STOP:
        assert decision.action is Action.STOP and "repair limit of 2" in decision.reason


def test_no_repair_is_allowed_means_none_is_taken() -> None:
    assert decide(failed_tests(), state(max_repairs=0, tools=ANOTHER)).action is Action.STOP


# --- The class default ------------------------------------------------------------------------------------


@pytest.mark.parametrize("category", [FailureCategory.SYNTAX_ERROR, FailureCategory.TEST_FAILURE, FailureCategory.MISSING_DEPENDENCY, FailureCategory.RUNTIME_EXCEPTION, FailureCategory.INVALID_CODE])
def test_a_fault_in_the_code_is_repaired_on_the_same_tool_first(category: FailureCategory) -> None:
    decision = decide(failed_tests(category), state(tools=ANOTHER))
    assert decision.action is Action.REPAIR and decision.tool is None


def test_a_provider_failure_goes_to_another_tool_when_one_exists() -> None:
    decision = decide(attempt_failed(CodingStatus.UNAVAILABLE), state(tools=ANOTHER))
    assert decision.action is Action.SWITCH_TOOL and decision.tool == "backup" and decision.real_switch is True


def test_with_no_other_tool_a_provider_failure_is_retried_and_no_switch_is_claimed() -> None:
    decision = decide(attempt_failed(CodingStatus.UNAVAILABLE), state())
    assert decision.action is Action.RETRY and decision.tool is None and decision.real_switch is False


def test_a_switch_between_simulations_does_not_read_as_a_real_one() -> None:
    decision = decide(attempt_failed(CodingStatus.UNAVAILABLE), state(tools=ToolOptions(alternatives=("simulated_b",), real_alternative=False)))
    assert decision.action is Action.SWITCH_TOOL and decision.real_switch is False and "simulated" in decision.reason


def test_a_used_up_token_budget_is_never_retried_on_the_same_tool() -> None:
    assert decide(attempt_failed(CodingStatus.LIMIT), state()).action is Action.STOP
    assert decide(attempt_failed(CodingStatus.LIMIT), state(tools=ANOTHER)).action is Action.SWITCH_TOOL


# --- Reconfiguring ----------------------------------------------------------------------------------------


def test_a_timed_out_attempt_gets_more_time_when_it_is_approved() -> None:
    decision = decide(attempt_failed(CodingStatus.TIMEOUT), state(next_timeout=1_200.0, tools=ANOTHER))
    assert decision.action is Action.RECONFIGURE and decision.timeout_seconds == 1_200.0


def test_with_no_approved_headroom_it_is_not_reconfigured() -> None:
    assert decide(attempt_failed(CodingStatus.TIMEOUT), state(next_timeout=None)).action is Action.RETRY
    assert decide(attempt_failed(CodingStatus.TIMEOUT), state(next_timeout=None, tools=ANOTHER)).action is Action.SWITCH_TOOL


def test_nothing_but_a_timed_out_attempt_is_reconfigured() -> None:
    assert decide(failed_tests(FailureCategory.TIMEOUT), state(next_timeout=1_200.0)).action is Action.REPAIR


@pytest.mark.parametrize(
    ("current", "ceiling", "left", "expected"),
    [(600.0, 1_200.0, None, 1_200.0), (600.0, 900.0, None, 900.0), (600.0, 1_200.0, 800.0, 800.0), (600.0, 600.0, None, None), (600.0, 500.0, None, None), (600.0, 1_200.0, 300.0, None), (1_200.0, 1_200.0, None, None)],
)
def test_the_longer_limit_stays_inside_the_ceiling_and_the_time_left(current: float, ceiling: float, left: float | None, expected: float | None) -> None:
    assert longer_timeout(current, ceiling=ceiling, seconds_left=left) == expected


# --- A failure that comes back ------------------------------------------------------------------------------


def test_the_same_failure_twice_is_not_repaired_a_third_way_on_the_same_tool() -> None:
    decision = decide(failed_tests(), state(streak=2))
    assert decision.action is Action.STOP and "came back 2 times" in decision.reason and "no other eligible tool" in decision.reason


def test_the_same_failure_twice_goes_to_another_tool_when_one_exists() -> None:
    assert decide(failed_tests(), state(streak=2, tools=ANOTHER)).action is Action.SWITCH_TOOL


def test_more_time_is_given_once() -> None:
    assert decide(attempt_failed(CodingStatus.TIMEOUT), state(streak=2, next_timeout=2_400.0)).action is Action.STOP


def test_the_repeat_limit_can_be_set() -> None:
    assert decide(failed_tests(), state(streak=2), RecoveryConfig(repeat_limit=3)).action is Action.REPAIR
    assert decide(failed_tests(), state(streak=3), RecoveryConfig(repeat_limit=3)).action is Action.STOP


# --- Verified history -------------------------------------------------------------------------------------


def test_a_repair_verified_history_says_rarely_works_is_skipped() -> None:
    decision = decide(failed_tests(), state(repairs_judged=5, repairs_verified=0))
    assert decision.action is Action.STOP and "0 of 5 verified repairs" in decision.reason


def test_it_goes_to_another_tool_instead_when_one_exists() -> None:
    assert decide(failed_tests(), state(repairs_judged=5, repairs_verified=0, tools=ANOTHER)).action is Action.SWITCH_TOOL


def test_too_few_results_prove_nothing() -> None:
    assert decide(failed_tests(), state(repairs_judged=4, repairs_verified=0)).action is Action.REPAIR


def test_a_repair_that_usually_works_is_tried() -> None:
    assert decide(failed_tests(), state(repairs_judged=20, repairs_verified=15)).action is Action.REPAIR


def test_history_only_touches_repairs() -> None:
    assert decide(attempt_failed(CodingStatus.UNAVAILABLE), state(repairs_judged=50, repairs_verified=0)).action is Action.RETRY


# --- Budget, tools and time -----------------------------------------------------------------------------------


def test_with_no_tool_able_to_run_it_stops_and_says_why() -> None:
    gone = ToolOptions(can_run=False, why="an attempt needs about $0.1144 and the run has $0.0100 left")
    for diagnosis in (failed_tests(), attempt_failed(CodingStatus.FAILED), attempt_failed(CodingStatus.TIMEOUT)):
        decision = decide(diagnosis, state(tools=gone, next_timeout=1_200.0))
        assert decision.action is Action.STOP and "$0.0100 left" in decision.reason


def test_an_attempt_that_cannot_fit_in_the_time_left_is_not_started() -> None:
    decision = decide(failed_tests(), state(seconds_left=30.0, seconds_needed=90.0))
    assert decision.action is Action.STOP and "the run has 30 left" in decision.reason


def test_an_attempt_that_fits_is_started() -> None:
    assert decide(failed_tests(), state(seconds_left=91.0, seconds_needed=90.0)).action is Action.REPAIR


# --- A sandbox with no answer ---------------------------------------------------------------------------------


def test_the_tests_run_once_more_when_the_sandbox_gave_no_answer() -> None:
    decision = decide(sandbox_down(), state())
    assert decision.action is Action.RETRY and "submit_implementation" in decision.hint


def test_that_retry_needs_no_coding_tool_and_no_repair_budget() -> None:
    gone = ToolOptions(can_run=False, why="the budget is used up")
    assert decide(sandbox_down(), state(tools=gone, repairs_used=2, seconds_left=0.0, seconds_needed=90.0)).action is Action.RETRY


def test_a_sandbox_still_down_stops_the_run() -> None:
    assert decide(sandbox_down(), state(streak=2)).action is Action.STOP


# --- The decision itself --------------------------------------------------------------------------------------


def test_the_same_inputs_always_give_the_same_decision() -> None:
    made = [decide(failed_tests(), state(streak=2, tools=ANOTHER)).to_dict() for _ in range(20)]
    assert len({json.dumps(d, sort_keys=True) for d in made}) == 1


def test_a_decision_carries_what_gpt_and_the_report_need() -> None:
    decision = decide(failed_tests(FailureCategory.SYNTAX_ERROR), state()).to_dict()
    assert decision["category"] == "SYNTAX_ERROR" and decision["confidence"] == "HIGH" and decision["evidence"] == ("evidence",) and decision["recovery_safe"] is True
    assert decision["actions"] == ("REPAIR", "SWITCH_TOOL", "STOP") and decision["action"] == "REPAIR" and decision["attempts_allowed"] == 2


def test_attempts_are_counted_against_the_limit_that_applies() -> None:
    assert decide(attempt_failed(), state(attempts_used=2)).attempts_used == 2 and decide(attempt_failed(), state()).attempts_allowed == 3
    assert decide(failed_tests(), state(repairs_used=1)).attempts_used == 1


def test_the_sources_are_the_two_the_policy_knows() -> None:
    assert attempt_failed().source == CODING and failed_tests().source == VERIFICATION


# --- Memory -----------------------------------------------------------------------------------------------------


def test_a_failure_with_the_same_fingerprint_lengthens_the_streak() -> None:
    first = RecoveryMemory().after(failed_tests(), decide(failed_tests(), state()), task=None)
    assert first.streak == 1 and first.streak_after(failed_tests()) == 2
    assert first.streak_after(replace(failed_tests(), fingerprint="other")) == 1


def test_memory_survives_being_saved() -> None:
    memory = RecoveryMemory("abc", 2, 3, "RECONFIGURE", "TIMEOUT", "evidence", 1_200.0)
    assert RecoveryMemory.load(memory.dump()) == memory and RecoveryMemory.load(None) == RecoveryMemory()


def test_a_coding_decision_applies_to_its_task_and_a_verification_decision_to_all() -> None:
    assert RecoveryMemory(task=2, action="RETRY").applies_to(2) and not RecoveryMemory(task=2, action="RETRY").applies_to(1)
    assert RecoveryMemory(task=None, action="REPAIR").applies_to(1) and not RecoveryMemory().applies_to(1)


def test_a_raised_limit_stays_with_its_task_and_goes_with_another() -> None:
    raised = RecoveryMemory(task=1, action="RECONFIGURE", timeout_seconds=1_200.0)
    decision = decide(attempt_failed(), state())
    assert raised.after(attempt_failed(), decision, task=1).timeout_seconds == 1_200.0 and raised.after(attempt_failed(), decision, task=2).timeout_seconds is None
