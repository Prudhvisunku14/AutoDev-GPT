"""Fixed bounded retry against intelligent recovery, in simulation. **No real tool, model or Docker is involved.**

    & $env:DEVPILOT_PYTHON -m app.demo.recovery_benchmark

The policies are the real code: `decide_coding` and `decide_verification` for the baseline, and the classifier
and `decide` for the proposal. They read real-format unittest output and attempt signals. The world they act in
is a seeded simulation, so this shows how the policies decide, not how any tool performs, and its outcomes are
never written to the history a real tool learns from.

How it is kept honest:
- "Correct" comes from an oracle that reads each scenario's true chances, which no policy can see, and looks
  ahead: stopping when a later try would work is wrong, and trying when nothing can work is wrong too.
- Both policies meet the same luck: a result depends on the seed, scenario, episode, step and action, never on
  the policy.
- The baseline keeps what Stage 2 gives it: with two tools a failed one ranks last, so its next attempt really
  does go to the other tool.
- The constants and scenarios were fixed before the first run, and were not changed to improve a result.
- Scenarios are included where the proposal should lose: failures that clear on a third try, and repairs that
  work on a second attempt. Its rule that a repeated failure ends a line of recovery costs it those.
- The overall figures depend on this mix of scenarios. They describe the mix, and are no forecast of real use.
- Each try has the same chance as the last, which is the kindest assumption for trying again. No real repair
  data exists to say otherwise, so `sensitivity` shows the result with that assumption changed, and with the
  repeat limit changed. It is reported beside the main result, and it changes no default.
- The main result lets a timed-out attempt be given longer where a scenario approves it. As deployed, no attempt
  is (RECOVERY_MAX_ATTEMPT_TIMEOUT_SECONDS is unset), so `sensitivity` also shows that world.
"""

from __future__ import annotations

import argparse
import json
import random
import statistics
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any

from app.coding.types import CodingStatus
from app.recovery.diagnosis import CODING, VERIFICATION, AttemptSignal, Diagnosis, FailureCategory, classify_attempt, classify_verification
from app.recovery.engine import Action, Decision, decide_coding, decide_verification
from app.recovery.policy import RecoveryConfig, RecoveryMemory, RecoveryState, ToolOptions, decide, longer_timeout
from app.sandbox.spec import ExecResult, ExecStatus
from app.verification.results import VerificationResult, failure_kind, judge

OUT_DIR = Path(r"D:\DevPilotCache\recovery-benchmark")
SEEDS, EPISODES = 20, 50
MAX_ATTEMPTS, MAX_REPAIRS = 3, 2  # the settings' defaults
ATTEMPT_SECONDS, TEST_SECONDS, ATTEMPT_COST = 60.0, 15.0, 0.20
TIMEOUT_SECONDS, TIMEOUT_CEILING = 600.0, 1_200.0
_MAX_STEPS = 8
_POLICIES = ("bounded", "intelligent")
Failure = ExecResult | AttemptSignal
Chances = Mapping[Action, float]


@dataclass(frozen=True)
class Scenario:
    """The truth about one kind of failure. `chances` is known to the world and the oracle, never to a policy."""

    name: str
    stage: str
    failure: Callable[[int, random.Random], Failure]
    chances: tuple[Chances, ...]  # by step; the last one repeats
    category: FailureCategory
    tools: int = 1
    money: float | None = None  # what the run can still spend when the failure happens
    headroom: bool = True  # whether a longer time limit is approved
    forbidden: bool = False  # nothing more may run, whatever it would achieve


# --- Failures, in the sandbox's and the engine's own form --------------------------------------------------


def _box(status: ExecStatus, output: str = "", *, detail: str | None = None) -> ExecResult:
    return ExecResult(status, "python-unittest", "devpilot-sbx-sim", exit_code=None if status is ExecStatus.TIMEOUT else 1, stderr=output, cleanup="removed", detail=detail)


def _problem(kind: str, test: str, line: str, counts: str) -> str:
    where = f"test_calculator.T.{test}"
    return (f"{test} ({where}) ... {kind}\n\n======\n{kind}: {test} ({where})\n------\nTraceback (most recent call last):\n  File \"/workspace/test_calculator.py\", line 9, in {test}\n"
            f"    self.assertEqual(subtract(1, 4), 3)\n{line}\n\n------\nRan 2 tests in 0.001s\n\nFAILED ({counts})\n")  # fmt: skip


def _assertion(step: int, rng: random.Random) -> ExecResult:
    number = rng.randint(1, 99)
    return _box(ExecStatus.FAILED, _problem("FAIL", "test_ac_002_subtract", f"AssertionError: -{number} != {number}", "failures=1"))


def _moving_assertion(step: int, rng: random.Random) -> ExecResult:
    return _box(ExecStatus.FAILED, _problem("FAIL", f"test_ac_00{step + 1}_case", "AssertionError: 1 != 2", "failures=1"))


def _misleading(step: int, rng: random.Random) -> ExecResult:
    return _box(ExecStatus.FAILED, "SyntaxError: printed by the program under test, nothing is wrong with the syntax\n" + _problem("FAIL", "test_ac_002_parse", "AssertionError: 1 != 2", "failures=1"))


def _syntax(step: int, rng: random.Random) -> ExecResult:
    return _box(ExecStatus.FAILED, '  File "/workspace/calculator.py", line 3\n    def add(a, b\n           ^\nSyntaxError: invalid syntax\n\n------\nRan 1 test in 0.0s\n\nFAILED (errors=1)\n')


def _missing(step: int, rng: random.Random) -> ExecResult:
    return _box(ExecStatus.FAILED, _problem("ERROR", "test_ac_001_fetch", "ModuleNotFoundError: No module named 'requests'", "errors=1"))


def _exception(step: int, rng: random.Random) -> ExecResult:
    return _box(ExecStatus.FAILED, _problem("ERROR", "test_ac_003_divide", "ZeroDivisionError: division by zero", "errors=1"))


def _endless(step: int, rng: random.Random) -> ExecResult:
    return _box(ExecStatus.TIMEOUT, detail="the command ran longer than 120 seconds and was stopped")


def _memory(step: int, rng: random.Random) -> ExecResult:
    return _box(ExecStatus.MEMORY_LIMIT, detail="the tests used too much memory")


def _no_answer(step: int, rng: random.Random) -> ExecResult:
    return _box(ExecStatus.UNAVAILABLE, detail="Docker is not reachable")


def _attempt(status: CodingStatus, detail: str = "the attempt failed", *, phase: str = "RESTORED", violations: tuple[str, ...] = ()) -> Callable[[int, random.Random], AttemptSignal]:
    return lambda step, rng: AttemptSignal(1, status, phase, detail, violations)


# --- The scenarios -----------------------------------------------------------------------------------------

R, W, T, N = Action.REPAIR, Action.SWITCH_TOOL, Action.RETRY, Action.RECONFIGURE
C = FailureCategory


def scenarios() -> tuple[Scenario, ...]:
    return (
        Scenario("syntax error, a repair usually works", VERIFICATION, _syntax, ({R: 0.85},), C.SYNTAX_ERROR),
        Scenario("assertion failure, a repair works half the time", VERIFICATION, _assertion, ({R: 0.5},), C.TEST_FAILURE),
        Scenario("a new failure after each repair", VERIFICATION, _moving_assertion, ({R: 0.6},), C.TEST_FAILURE),
        Scenario("the requirement cannot be met", VERIFICATION, _assertion, ({R: 0.0},), C.TEST_FAILURE),
        Scenario("third-party package, a repair can replace it", VERIFICATION, _missing, ({R: 0.6},), C.MISSING_DEPENDENCY),
        Scenario("exception in the code", VERIFICATION, _exception, ({R: 0.7},), C.RUNTIME_EXCEPTION),
        Scenario("endless loop in the tests", VERIFICATION, _endless, ({R: 0.6},), C.TIMEOUT),
        Scenario("tests run out of memory", VERIFICATION, _memory, ({R: 0.5},), C.RESOURCE_EXHAUSTION),
        Scenario("printed text misleads the classifier", VERIFICATION, _misleading, ({R: 0.6},), C.TEST_FAILURE),
        Scenario("sandbox blip, the tests can run again", VERIFICATION, _no_answer, ({T: 0.8},), C.TOOL_UNAVAILABLE),
        Scenario("sandbox down for good", VERIFICATION, _no_answer, ({T: 0.0},), C.TOOL_UNAVAILABLE),
        Scenario("two tools, only the second can fix it", VERIFICATION, _assertion, ({R: 0.0, W: 0.8},), C.TEST_FAILURE, tools=2),
        Scenario("two tools, the first is better at repairs", VERIFICATION, _assertion, ({R: 0.7, W: 0.2},), C.TEST_FAILURE, tools=2),
        Scenario("provider blip", CODING, _attempt(CodingStatus.UNAVAILABLE, "cannot reach the engine"), ({T: 0.7},), C.TOOL_UNAVAILABLE),
        Scenario("outage that clears on the third try", CODING, _attempt(CodingStatus.UNAVAILABLE, "cannot reach the engine"), ({T: 0.0}, {T: 1.0}), C.TOOL_UNAVAILABLE),
        Scenario("provider down for good", CODING, _attempt(CodingStatus.UNAVAILABLE, "cannot reach the engine"), ({T: 0.0},), C.TOOL_UNAVAILABLE),
        Scenario("engine error, a retry often works", CODING, _attempt(CodingStatus.FAILED, "the model returned an error"), ({T: 0.6},), C.TOOL_UNAVAILABLE),
        Scenario("two tools, the first is down", CODING, _attempt(CodingStatus.UNAVAILABLE, "cannot reach the engine"), ({T: 0.0, W: 0.8},), C.TOOL_UNAVAILABLE, tools=2),
        Scenario("attempt needs more time", CODING, _attempt(CodingStatus.TIMEOUT, "no result within 600 seconds"), ({T: 0.0, N: 0.8},), C.TIMEOUT),
        Scenario("timed out by bad luck", CODING, _attempt(CodingStatus.TIMEOUT, "no result within 600 seconds"), ({T: 0.6, N: 0.7},), C.TIMEOUT),
        Scenario("timed out, no longer limit is approved", CODING, _attempt(CodingStatus.TIMEOUT, "no result within 600 seconds"), ({T: 0.3},), C.TIMEOUT, headroom=False),
        Scenario("the task's tokens ran out", CODING, _attempt(CodingStatus.LIMIT, "the task used more than its budget"), ({T: 0.0},), C.RESOURCE_EXHAUSTION),
        Scenario("policy breach, a retry would look fine", CODING, _attempt(CodingStatus.POLICY_VIOLATION, "the audit refused the changes", violations=("opencode.json: protected path (added); it could change the engine's configuration",)), ({T: 0.7},), C.SECURITY_VIOLATION, forbidden=True),
        Scenario("stop not confirmed, a retry would look fine", CODING, _attempt(CodingStatus.FAILED, "the attempt failed", phase="QUARANTINED"), ({T: 0.7},), C.AMBIGUOUS_STATE, forbidden=True),
        Scenario("no money for another attempt", CODING, _attempt(CodingStatus.FAILED, "the model returned an error"), ({T: 0.6},), C.TOOL_UNAVAILABLE, money=0.05),
        Scenario("money for one more attempt", CODING, _attempt(CodingStatus.FAILED, "the model returned an error"), ({T: 0.6},), C.TOOL_UNAVAILABLE, money=0.25),
        Scenario("ambiguous delivery, proved undone", CODING, _attempt(CodingStatus.UNAVAILABLE, "the prompt's delivery is ambiguous (no answer)"), ({T: 0.6},), C.TOOL_UNAVAILABLE),
        Scenario("oversized file, a retry can shrink it", CODING, _attempt(CodingStatus.POLICY_VIOLATION, "the audit refused the changes", violations=("big.py: 2000000 bytes is over the 1000000-byte limit",)), ({T: 0.6},), C.INVALID_CODE),
    )


# --- The oracle ---------------------------------------------------------------------------------------------


def _chances(scenario: Scenario, step: int) -> Chances:
    return scenario.chances[min(step, len(scenario.chances) - 1)]


def _true_action(action: Action, switched: bool) -> Action:
    """Once on the second tool, every attempt is that tool's."""
    return Action.SWITCH_TOOL if switched and action in (Action.REPAIR, Action.RETRY) else action


def _allowed(scenario: Scenario, failure: Failure, *, attempts_used: int, repairs_used: int, money: float | None, switched: bool, timeout: float) -> list[Action]:
    if scenario.forbidden:
        return []
    affordable = money is None or money >= ATTEMPT_COST
    if isinstance(failure, ExecResult):
        tests_only = [Action.RETRY] if failure.status is ExecStatus.UNAVAILABLE else []
        if tests_only or repairs_used >= MAX_REPAIRS or not affordable:
            return tests_only
        return [Action.REPAIR] + ([Action.SWITCH_TOOL] if scenario.tools > 1 and not switched else [])
    if attempts_used >= MAX_ATTEMPTS or not affordable:
        return []
    options = [Action.RETRY]
    options += [Action.SWITCH_TOOL] if scenario.tools > 1 and not switched else []
    options += [Action.RECONFIGURE] if scenario.headroom and timeout < TIMEOUT_CEILING else []
    return options


def oracle(scenario: Scenario, step: int, allowed: list[Action], switched: bool) -> frozenset[Action]:
    """What is right, from the truth alone. STOP when nothing allowed can work now or later."""
    ahead = scenario.chances[min(step, len(scenario.chances) - 1) :]
    worth = {action: max(c.get(_true_action(action, switched), 0.0) for c in ahead) for action in allowed}
    best = max(worth.values(), default=0.0)
    return frozenset(a for a, chance in worth.items() if chance == best) if best > 0 else frozenset({Action.STOP})


def _succeeds(seed: int, scenario: Scenario, episode: int, step: int, action: Action, fade: float = 1.0) -> bool:
    """The same for both policies: the policy is not an input. `fade` is what each failed try does to the next
    one's chance. A failure on a timetable, such as an outage, keeps its own."""
    chance = _chances(scenario, step).get(action, 0.0)
    if len(scenario.chances) == 1:
        chance *= fade**step
    return random.Random(f"{seed}|{scenario.name}|{episode}|{step}|{action}").random() < chance


# --- An episode ------------------------------------------------------------------------------------------------


def _bounded(failure: Failure, attempts_used: int, repairs_used: int) -> Decision:
    if isinstance(failure, ExecResult):
        return decide_verification(failure_kind(judge(failure, ())), repairs_used=repairs_used, max_repairs=MAX_REPAIRS)
    return decide_coding(failure.status, quarantined=failure.phase == "QUARANTINED", attempts_used=attempts_used, max_attempts=MAX_ATTEMPTS)


def _intelligent(scenario: Scenario, failure: Failure, memory: RecoveryMemory, repairs: Mapping[str, tuple[int, int]], config: RecoveryConfig, *, attempts_used: int, repairs_used: int, money: float | None, switched: bool, timeout: float) -> tuple[Decision, Diagnosis]:
    verification: VerificationResult | None = judge(failure, ()) if isinstance(failure, ExecResult) else None
    diagnosis = classify_verification(verification) if verification is not None else classify_attempt(failure)  # type: ignore[arg-type]
    affordable = money is None or money >= ATTEMPT_COST
    tools = ToolOptions(can_run=affordable, alternatives=("second",) if scenario.tools > 1 and not switched else (), why="" if affordable else "an attempt needs more than the run has left")
    judged, verified = repairs.get(diagnosis.category.value, (0, 0))
    state = RecoveryState(
        attempts_used=attempts_used, max_attempts=MAX_ATTEMPTS, repairs_used=repairs_used, max_repairs=MAX_REPAIRS, streak=memory.streak_after(diagnosis), tools=tools, repairs_judged=judged, repairs_verified=verified,
        next_timeout=longer_timeout(timeout, ceiling=TIMEOUT_CEILING if scenario.headroom else timeout, seconds_left=None),
    )  # fmt: skip
    return decide(diagnosis, state, config), diagnosis


def run_episode(policy: str, scenario: Scenario, seed: int, episode: int, repairs: dict[str, tuple[int, int]], *, config: RecoveryConfig | None = None, fade: float = 1.0) -> dict[str, Any]:
    """From the first failure to a recovery or a stop. `repairs` is what this policy has learned so far."""
    config = config or RecoveryConfig()
    attempts_used, repairs_used, switched, timeout, money, memory = 1, 0, False, TIMEOUT_SECONDS, scenario.money, RecoveryMemory()
    seconds = cost = 0.0
    attempts = decisions = wrong = violations = classified = 0
    recovered, timings = False, []
    for step in range(_MAX_STEPS):
        failure = scenario.failure(step, random.Random(f"{seed}|{scenario.name}|{episode}|{step}|log"))
        right = oracle(scenario, step, _allowed(scenario, failure, attempts_used=attempts_used, repairs_used=repairs_used, money=money, switched=switched, timeout=timeout), switched)
        began = time.perf_counter_ns()
        diagnosis: Diagnosis | None = None
        if policy == "bounded":
            decision = _bounded(failure, attempts_used, repairs_used)
        else:
            decision, diagnosis = _intelligent(scenario, failure, memory, repairs, config, attempts_used=attempts_used, repairs_used=repairs_used, money=money, switched=switched, timeout=timeout)
            memory = memory.after(diagnosis, decision, task=None)
            classified += diagnosis.category is scenario.category
        timings.append(time.perf_counter_ns() - began)
        action = decision.action
        if policy == "bounded" and scenario.tools > 1 and action in (Action.REPAIR, Action.RETRY):
            action = Action.SWITCH_TOOL  # Stage 2 ranks a failed tool last, so the next attempt goes to the other one
        taken = _true_action(action, switched)
        decisions += 1
        wrong += action not in right and taken not in right
        if action is Action.STOP:
            break
        tests_only = isinstance(failure, ExecResult) and action is Action.RETRY
        if scenario.forbidden or (not tests_only and money is not None and money < ATTEMPT_COST):
            violations += 1  # a later guard would refuse it, so the attempt does not run and the episode ends
            break
        limit_broken = (not isinstance(failure, ExecResult) and attempts_used >= MAX_ATTEMPTS) or (isinstance(failure, ExecResult) and not tests_only and repairs_used >= MAX_REPAIRS)
        violations += limit_broken
        if not tests_only:
            attempts += 1
            cost += ATTEMPT_COST
            money = None if money is None else money - ATTEMPT_COST
            attempts_used += 1
            repairs_used += isinstance(failure, ExecResult)
            seconds += ATTEMPT_SECONDS * (2 if action is Action.RECONFIGURE else 1)
        seconds += TEST_SECONDS if isinstance(failure, ExecResult) else 0.0
        switched = switched or action is Action.SWITCH_TOOL
        timeout = timeout * 2 if action is Action.RECONFIGURE else timeout
        recovered = _succeeds(seed, scenario, episode, step, taken, fade)
        if diagnosis is not None and action is Action.REPAIR:
            judged, verified = repairs.get(diagnosis.category.value, (0, 0))
            repairs[diagnosis.category.value] = (judged + 1, verified + int(recovered))
        if recovered:
            break
    return {"recovered": recovered, "seconds": seconds, "cost": cost, "attempts": attempts, "decisions": decisions, "wrong": wrong, "violations": violations, "classified": classified, "timings": timings}


def run_scenario(policy: str, scenario: Scenario, *, seeds: int, episodes: int, config: RecoveryConfig | None = None, fade: float = 1.0) -> dict[str, Any]:
    done = [[run_episode(policy, scenario, seed, episode, repairs, config=config, fade=fade) for episode in range(episodes)] for seed, repairs in ((seed, {}) for seed in range(seeds))]
    rows = [row for per_seed in done for row in per_seed]
    total = len(rows)
    timings = [t for row in rows for t in row["timings"]]
    decisions = sum(r["decisions"] for r in rows)
    return {
        "recovery_rate": sum(r["recovered"] for r in rows) / total, "per_seed": [sum(r["recovered"] for r in per_seed) / len(per_seed) for per_seed in done],
        "decisions": decisions, "wrong": sum(r["wrong"] for r in rows), "mean_attempts": sum(r["attempts"] for r in rows) / total, "mean_seconds": sum(r["seconds"] for r in rows) / total,
        "mean_cost_usd": sum(r["cost"] for r in rows) / total, "safe_termination_rate": sum(r["violations"] == 0 for r in rows) / total, "violations": sum(r["violations"] for r in rows),
        "classified": sum(r["classified"] for r in rows), "median_decide_us": statistics.median(timings) / 1000,
    }  # fmt: skip


def _recoverable(scenario: Scenario) -> bool:
    return not scenario.forbidden and any(chance > 0 for c in scenario.chances for chance in c.values())


def run_benchmark(*, seeds: int = SEEDS, episodes: int = EPISODES) -> dict[str, Any]:
    rows = []
    for scenario in scenarios():
        row: dict[str, Any] = {"scenario": scenario.name, "stage": scenario.stage, "category": scenario.category.value, "recoverable": _recoverable(scenario)}
        for policy in _POLICIES:
            row[policy] = run_scenario(policy, scenario, seeds=seeds, episodes=episodes)
        rows.append(row)
    return {"simulated": True, "seeds": seeds, "episodes": episodes, "scenarios": rows, "summary": summarise(rows), "sensitivity": sensitivity(seeds=seeds, episodes=episodes)}


def sensitivity(*, seeds: int, episodes: int) -> list[dict[str, Any]]:
    """The same scenarios with one assumption changed at a time. None of this changes a default."""
    rows = []
    deployed = [replace(s, headroom=False) for s in scenarios()]  # RECOVERY_MAX_ATTEMPT_TIMEOUT_SECONDS unset: no attempt is given longer
    for world, fade, cases in (("each try has the same chance", 1.0, scenarios()), ("a failed try halves the next one's chance", 0.5, scenarios()), ("no attempt is given longer, as deployed", 1.0, deployed)):
        for name, policy, config in (("bounded", "bounded", None), ("intelligent, repeat limit 2 (the default)", "intelligent", RecoveryConfig()), ("intelligent, repeat limit 3", "intelligent", RecoveryConfig(repeat_limit=3))):
            outcomes = [(s, run_scenario(policy, s, seeds=seeds, episodes=episodes, config=config, fade=fade)) for s in cases]
            recoverable = [o for s, o in outcomes if _recoverable(s)]
            rows.append({
                "world": world, "policy": name, "recovery_rate": sum(o["recovery_rate"] for o in recoverable) / len(recoverable), "mean_attempts": sum(o["mean_attempts"] for _, o in outcomes) / len(outcomes),
                "wrong_rate": sum(o["wrong"] for _, o in outcomes) / sum(o["decisions"] for _, o in outcomes),
                "mean_cost_usd": sum(o["mean_cost_usd"] for _, o in outcomes) / len(outcomes), "cost_per_recovery_usd": sum(o["mean_cost_usd"] for o in recoverable) / sum(o["recovery_rate"] for o in recoverable),
            })  # fmt: skip
    return rows


def summarise(rows: list[dict[str, Any]]) -> dict[str, Any]:
    recoverable = [r for r in rows if r["recoverable"]]

    def mean(policy: str, key: str, among: list[dict[str, Any]]) -> float:
        return sum(r[policy][key] for r in among) / len(among)

    summary: dict[str, Any] = {}
    for policy in _POLICIES:
        seeds = [statistics.mean(values) for values in zip(*(r[policy]["per_seed"] for r in recoverable), strict=True)]
        summary[policy] = {
            "recovery_rate": mean(policy, "recovery_rate", recoverable), "lowest_seed": min(seeds), "highest_seed": max(seeds), "wrong": sum(r[policy]["wrong"] for r in rows), "decisions": sum(r[policy]["decisions"] for r in rows),
            "wrong_rate": sum(r[policy]["wrong"] for r in rows) / sum(r[policy]["decisions"] for r in rows), "mean_attempts": mean(policy, "mean_attempts", rows), "mean_seconds": mean(policy, "mean_seconds", rows),
            "mean_cost_usd": mean(policy, "mean_cost_usd", rows), "safe_termination_rate": mean(policy, "safe_termination_rate", rows), "violations": sum(r[policy]["violations"] for r in rows),
            "median_decide_us": statistics.median(r[policy]["median_decide_us"] for r in rows),
        }  # fmt: skip
    better = [r["scenario"] for r in recoverable if r["intelligent"]["recovery_rate"] > r["bounded"]["recovery_rate"] + 0.01]
    worse = [r["scenario"] for r in recoverable if r["intelligent"]["recovery_rate"] < r["bounded"]["recovery_rate"] - 0.01]
    summary["scenarios"] = {"total": len(rows), "recoverable": len(recoverable), "intelligent_recovers_more": better, "bounded_recovers_more": worse}
    summary["classification_accuracy"] = sum(r["intelligent"]["classified"] for r in rows) / sum(r["intelligent"]["decisions"] for r in rows)
    return summary


def render(result: dict[str, Any]) -> str:
    def percent(value: float) -> str:
        return f"{value:.1%}"

    lines = ["# Recovery benchmark: SIMULATED", "", f"{result['seeds']} seeds, {result['episodes']} failures each. A seeded world. The decisions are the real code. No real tool, model or Docker ran.", ""]
    lines += ["| Scenario | True category | Recovered: bounded | Recovered: intelligent | Wrong decisions: bounded | Wrong decisions: intelligent | Attempts: bounded | Attempts: intelligent | Seconds: bounded | Seconds: intelligent | Cost: bounded | Cost: intelligent | Violations: bounded | Violations: intelligent |", "|" + "---|" * 14]
    for r in result["scenarios"]:
        b, i = r["bounded"], r["intelligent"]
        lines.append(f"| {r['scenario']} | {r['category']} | {percent(b['recovery_rate'])} | {percent(i['recovery_rate'])} | {b['wrong']} of {b['decisions']} | {i['wrong']} of {i['decisions']} | {b['mean_attempts']:.2f} | {i['mean_attempts']:.2f} | {b['mean_seconds']:.0f} | {i['mean_seconds']:.0f} | ${b['mean_cost_usd']:.3f} | ${i['mean_cost_usd']:.3f} | {b['violations']} | {i['violations']} |")
    b, i = result["summary"]["bounded"], result["summary"]["intelligent"]
    lines += ["", "| Overall, each scenario counted equally | Bounded | Intelligent |", "|---|---|---|"]
    lines += [f"| Recovery success, recoverable scenarios | {percent(b['recovery_rate'])} | {percent(i['recovery_rate'])} |", f"| Lowest and highest seed | {percent(b['lowest_seed'])} to {percent(b['highest_seed'])} | {percent(i['lowest_seed'])} to {percent(i['highest_seed'])} |"]
    lines += [f"| Incorrect decisions | {b['wrong']} of {b['decisions']} ({percent(b['wrong_rate'])}) | {i['wrong']} of {i['decisions']} ({percent(i['wrong_rate'])}) |", f"| Attempts per failure | {b['mean_attempts']:.2f} | {i['mean_attempts']:.2f} |"]
    lines += [f"| Recovery time per failure | {b['mean_seconds']:.0f} s | {i['mean_seconds']:.0f} s |", f"| Estimated cost per failure | ${b['mean_cost_usd']:.3f} | ${i['mean_cost_usd']:.3f} |", f"| Safe termination | {percent(b['safe_termination_rate'])} | {percent(i['safe_termination_rate'])} |"]
    lines += [f"| Policy violations | {b['violations']} | {i['violations']} |", f"| Median time per decision | {b['median_decide_us']:.1f} µs | {i['median_decide_us']:.1f} µs |"]
    scenes = result["summary"]["scenarios"]
    lines += ["", f"The classifier named the true category in {percent(result['summary']['classification_accuracy'])} of its decisions, on logs written for this benchmark.", ""]
    lines += [f"The intelligent policy recovered more in {len(scenes['intelligent_recovers_more'])} of {scenes['recoverable']} recoverable scenarios: {', '.join(scenes['intelligent_recovers_more']) or 'none'}.", ""]
    lines += [f"The bounded policy recovered more in {len(scenes['bounded_recovers_more'])}: {', '.join(scenes['bounded_recovers_more']) or 'none'}.", ""]
    lines += ["## What the result depends on", "", "The same scenarios with one assumption changed. The table above is the result: this changes no default.", ""]
    lines += ["| World | Policy | Recovery success | Incorrect decisions | Attempts per failure | Cost per failure | Cost per recovery |", "|---|---|---|---|---|---|---|"]
    lines += [f"| {r['world']} | {r['policy']} | {percent(r['recovery_rate'])} | {percent(r['wrong_rate'])} | {r['mean_attempts']:.2f} | ${r['mean_cost_usd']:.3f} | ${r['cost_per_recovery_usd']:.3f} |" for r in result["sensitivity"]]
    return "\n".join([*lines, ""])


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--seeds", type=int, default=SEEDS)
    parser.add_argument("--episodes", type=int, default=EPISODES)
    args = parser.parse_args()
    result = run_benchmark(seeds=args.seeds, episodes=args.episodes)
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    (OUT_DIR / "recovery-benchmark.json").write_text(json.dumps(result, indent=2), encoding="utf-8")
    text = render(result)
    (OUT_DIR / "recovery-benchmark.md").write_text(text, encoding="utf-8")
    print(text)


if __name__ == "__main__":
    main()
