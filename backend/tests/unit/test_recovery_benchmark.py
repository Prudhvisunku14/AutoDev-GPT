"""The recovery benchmark itself: repeatable, labelled as simulation, fair, and not able to reach real history."""

from __future__ import annotations

import inspect
import random

from app.coding.types import CodingStatus
from app.demo import recovery_benchmark as bench
from app.recovery.diagnosis import CODING, VERIFICATION, AttemptSignal, FailureCategory
from app.recovery.engine import Action
from app.sandbox.spec import ExecStatus
from app.verification.results import judge


def scenario(name: str) -> bench.Scenario:
    return next(s for s in bench.scenarios() if s.name == name)


def without_timing(result: dict) -> dict:
    return {**result, "scenarios": [{k: ({n: v for n, v in r[k].items() if n != "median_decide_us"} if k in ("bounded", "intelligent") else r[k]) for k in r} for r in result["scenarios"]],
            "summary": {k: ({n: v for n, v in s.items() if n != "median_decide_us"} if k in ("bounded", "intelligent") else s) for k, s in result["summary"].items()}}  # fmt: skip


def test_the_same_seeds_give_the_same_results() -> None:
    assert without_timing(bench.run_benchmark(seeds=2, episodes=5)) == without_timing(bench.run_benchmark(seeds=2, episodes=5))


def test_results_are_labelled_as_a_simulation() -> None:
    result = bench.run_benchmark(seeds=1, episodes=2)
    assert result["simulated"] is True and "SIMULATED" in bench.render(result) and "No real tool, model or Docker ran" in bench.render(result)


def test_it_covers_every_required_kind_of_failure() -> None:
    assert {s.category for s in bench.scenarios()} >= {c for c in FailureCategory if c is not FailureCategory.UNKNOWN} - {FailureCategory.UNKNOWN}
    assert {s.stage for s in bench.scenarios()} == {CODING, VERIFICATION}


def test_the_oracle_reads_the_truth_and_no_policy() -> None:
    source = inspect.getsource(bench.oracle)
    assert "decide" not in source and "classify" not in source and "policy" not in source


def test_both_policies_meet_the_same_luck() -> None:
    assert "policy" not in inspect.signature(bench._succeeds).parameters
    case = scenario("provider blip")
    assert bench._succeeds(3, case, 7, 0, Action.RETRY) == bench._succeeds(3, case, 7, 0, Action.RETRY)


def test_stopping_when_a_later_try_would_work_is_wrong() -> None:
    case = scenario("outage that clears on the third try")
    assert bench.oracle(case, 0, [Action.RETRY], False) == {Action.RETRY}


def test_trying_when_nothing_can_work_is_wrong() -> None:
    assert bench.oracle(scenario("provider down for good"), 0, [Action.RETRY], False) == {Action.STOP}


def test_with_nothing_allowed_the_right_answer_is_to_stop() -> None:
    assert bench.oracle(scenario("policy breach, a retry would look fine"), 0, [], False) == {Action.STOP}


def test_a_forbidden_action_is_never_the_right_one_however_well_it_would_work() -> None:
    case = scenario("stop not confirmed, a retry would look fine")
    failure = case.failure(0, random.Random(0))
    assert bench._allowed(case, failure, attempts_used=1, repairs_used=0, money=None, switched=False, timeout=600.0) == []


def test_the_baseline_keeps_the_switch_stage_2_gives_it() -> None:
    outcome = bench.run_scenario("bounded", scenario("two tools, only the second can fix it"), seeds=2, episodes=20)
    assert outcome["recovery_rate"] > 0.5  # a repair on the first tool has no chance, so these came from the second


def test_neither_policy_acts_on_an_unsafe_state() -> None:
    for name in ("policy breach, a retry would look fine", "stop not confirmed, a retry would look fine"):
        for policy in ("bounded", "intelligent"):
            outcome = bench.run_scenario(policy, scenario(name), seeds=2, episodes=10)
            assert outcome["mean_attempts"] == 0 and outcome["violations"] == 0 and outcome["recovery_rate"] == 0


def test_the_logs_are_in_the_sandboxs_own_form() -> None:
    result = judge(scenario("assertion failure, a repair works half the time").failure(0, random.Random(1)), ())
    assert result.counts.readable and result.counts.failures == 1 and result.counts.failed_names == ("test_ac_002_subtract",)
    assert judge(bench._no_answer(0, random.Random(1)), ()).exec_status == ExecStatus.UNAVAILABLE.value


def test_a_coding_failure_is_a_real_signal() -> None:
    signal = scenario("provider blip").failure(0, random.Random(1))
    assert isinstance(signal, AttemptSignal) and signal.status is CodingStatus.UNAVAILABLE


def test_the_mix_includes_failures_where_the_baseline_should_do_better() -> None:
    assert {"outage that clears on the third try", "assertion failure, a repair works half the time", "two tools, only the second can fix it"} <= {s.name for s in bench.scenarios()}


def test_the_sensitivity_table_is_apart_from_the_result_and_changes_no_default() -> None:
    result = bench.run_benchmark(seeds=1, episodes=4)
    assert len(result["sensitivity"]) == 9 and "changes no default" in bench.render(result)
    assert {r["world"] for r in result["sensitivity"]} >= {"no attempt is given longer, as deployed"} and all(0 <= r["wrong_rate"] <= 1 for r in result["sensitivity"])
    plain = bench.run_scenario("intelligent", scenario("provider blip"), seeds=1, episodes=4)
    assert plain["recovery_rate"] == result["scenarios"][[s.name for s in bench.scenarios()].index("provider blip")]["intelligent"]["recovery_rate"]


def test_a_failed_try_can_lower_the_next_ones_chance_and_never_the_first_ones() -> None:
    case = scenario("provider blip")
    assert all(bench._succeeds(1, case, n, 0, Action.RETRY, 0.5) == bench._succeeds(1, case, n, 0, Action.RETRY) for n in range(30))
    assert sum(bench._succeeds(1, case, n, 2, Action.RETRY, 0.5) for n in range(200)) < sum(bench._succeeds(1, case, n, 2, Action.RETRY) for n in range(200))


def test_a_failure_on_a_timetable_keeps_its_own() -> None:
    case = scenario("outage that clears on the third try")
    assert bench._succeeds(1, case, 0, 1, Action.RETRY, 0.5) is True


def test_it_writes_nothing_to_a_database() -> None:
    source = inspect.getsource(bench)
    assert "sqlalchemy" not in source and "ToolOutcome" not in source and "record_attempt" not in source


def test_it_imports_nothing_that_could_reach_outside() -> None:
    source = inspect.getsource(bench)
    assert not [name for name in ("docker", "subprocess", "openai", "socket", "httpx") if f"import {name}" in source]
