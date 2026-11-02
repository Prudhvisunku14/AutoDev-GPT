"""The benchmark itself: repeatable, labelled as simulation, and not able to reach real history."""

from __future__ import annotations

import inspect

from app.demo import selection_benchmark as bench


def strip_timing(result: dict) -> dict:
    return {**result, "scenarios": [{k: ({n: v for n, v in r[k].items() if n != "median_choose_us"} if k in ("fixed", "adaptive") else r[k]) for k in r} for r in result["scenarios"]]}


def test_the_same_seeds_give_the_same_results() -> None:
    assert strip_timing(bench.run_benchmark(seeds=2, episodes=8)) == strip_timing(bench.run_benchmark(seeds=2, episodes=8))


def test_results_are_labelled_as_a_simulation() -> None:
    result = bench.run_benchmark(seeds=1, episodes=4)
    assert result["simulated"] is True and "SIMULATED" in bench.render(result) and "No real tool, model or Docker ran" in bench.render(result)


def test_it_covers_every_required_situation() -> None:
    names = " ".join(s.name for s in bench.scenarios())
    for wanted in ("python application", "backend API", "frontend", "debugging", "repository modification", "unavailable", "budget", "fallback", "poor history", "missing history", "unsafe", "refusal"):
        assert wanted in names


def test_stacks_with_no_real_path_never_report_a_success() -> None:
    result = bench.run_benchmark(seeds=1, episodes=4)
    only = [r for r in result["scenarios"] if r["selection_only"]]
    assert len(only) == 3 and all(r[p]["success_rate"] is None for r in only for p in ("fixed", "adaptive"))


def test_the_oracle_ignores_the_scoring_and_refuses_when_nothing_is_allowed() -> None:
    answers = {s.name: bench.oracle(s) for s in bench.scenarios()}
    assert answers["refusal is the right answer"] is None and answers["unsafe tool preferred"] == "sandboxed"
    assert answers["python application (fixed preference already best)"] == "best_first"  # the dearer tool, on purpose
    assert "score" not in inspect.getsource(bench.oracle)


def test_a_control_exists_where_the_fixed_preference_is_already_right() -> None:
    assert any(s.tools[0].name == bench.oracle(s) for s in bench.scenarios())


def test_both_policies_meet_the_same_luck() -> None:
    scenario = bench.scenarios()[0]
    assert bench._succeeds(3, scenario, 7, 0, scenario.tools[0]) == bench._succeeds(3, scenario, 7, 0, scenario.tools[0])
    assert "policy" not in inspect.signature(bench._succeeds).parameters


def test_the_adaptive_policy_commits_no_violation_and_the_fixed_one_does() -> None:
    result = bench.run_benchmark(seeds=2, episodes=10)
    assert sum(r["adaptive"]["violations"] for r in result["scenarios"]) == 0 and sum(r["fixed"]["violations"] for r in result["scenarios"]) > 0


def test_every_task_in_the_refusal_scenario_is_refused_by_the_adaptive_policy() -> None:
    scenario = next(s for s in bench.scenarios() if s.name == "refusal is the right answer")
    outcome = bench.run_scenario("adaptive", scenario, seeds=1, episodes=5)
    assert outcome["refused"] == 5 and outcome["correct_rate"] == 1.0 and outcome["mean_cost_usd"] == 0.0


def test_it_writes_nothing_to_a_database() -> None:
    source = inspect.getsource(bench)
    assert "sqlalchemy" not in source and "ToolOutcome" not in source and "record_attempt" not in source


def test_it_imports_nothing_that_could_reach_outside() -> None:
    source = inspect.getsource(bench)
    assert not [name for name in ("docker", "subprocess", "openai", "socket", "httpx") if f"import {name}" in source]


def test_a_forbidden_tool_that_works_is_not_counted_as_a_success() -> None:
    scenario = next(s for s in bench.scenarios() if s.name == "unsafe tool preferred")
    outcome = bench.run_scenario("fixed", scenario, seeds=1, episodes=20)
    assert outcome["success_rate"] > 0.5 and outcome["allowed_success_rate"] == 0.0
