"""Fixed preference against adaptive selection, in simulation. **No real tool, model or Docker is involved.**

    & $env:DEVPILOT_PYTHON -m app.demo.selection_benchmark

Every tool below is hypothetical, because only one real coding provider exists. The selectors are the real
code; the world they act in is a seeded simulation. So this shows how the policies decide, not how any
provider performs, and its outcomes are never written to the history a real tool learns from.

How it is kept honest:
- "Correct" comes from an oracle that ignores the scoring: the allowed tool with the highest true chance of
  success, or a refusal if none is allowed. A cheaper tool picked on purpose therefore counts as wrong.
- Both policies meet the same random outcomes, so any difference comes from the policy.
- The weights are the initial ones, untuned. Every tool states its true cost, time and memory, and no tool
  states a quality, by one rule across all scenarios.
- Some scenarios have the fixed preference already right, so a result can go against the adaptive policy.
- Stacks and task kinds with no real execution path report the decision only, never a simulated success.
- A forbidden tool that "succeeds" is not a success: `allowed_success_rate` leaves those out. `success_rate`
  keeps them, so the raw figure can still be seen.
- The overall figures depend on the mix of scenarios, most of which were built so the preference is wrong.
  They describe these scenarios, and are no forecast of real use.
"""

from __future__ import annotations

import argparse
import json
import random
import statistics
import time
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from app.selection.adaptive import AdaptiveSelector, EligibilityPolicy
from app.selection.model import BUILD, MODIFY, REPAIR, SIMULATED, SelectionContext, ToolHistory, ToolProfile, ToolStatus
from app.selection.selector import NoProvider, ProviderOption, ProviderSelector, Selection

CAPABILITY = "code_generation"
OUT_DIR = Path(r"D:\DevPilotCache\selection-benchmark")
_POLICY = EligibilityPolicy(approved_environments=frozenset({"sandbox"}), configured_credentials=frozenset({"HAVE_THIS"}), approved_network=frozenset({"approved.example"}))
SEEDS, EPISODES = 20, 60


@dataclass(frozen=True)
class Tool:
    """The truth about a hypothetical tool. `chance` is known to the world and the oracle, never to a selector."""

    name: str
    chance: float
    seconds: float = 60.0
    cost: float = 0.20
    memory: int = 1_024
    stacks: tuple[str, ...] = ("python-cli",)
    kinds: tuple[str, ...] = (BUILD, REPAIR)
    unavailable: str | None = None
    isolated: bool = True
    credentials: frozenset[str] = frozenset()
    network: frozenset[str] = frozenset()


@dataclass(frozen=True)
class Scenario:
    name: str
    tools: tuple[Tool, ...]  # in the fixed policy's preference order
    stack: str = "python-cli"
    kind: str = BUILD
    budget: float | None = None
    max_attempts: int = 1
    selection_only: bool = False  # DevPilot has no real path for this, so no success is simulated
    seeded: Mapping[str, tuple[int, int]] = field(default_factory=dict)  # tool -> (successes, failures) before the run


def scenarios() -> tuple[Scenario, ...]:
    return (
        Scenario("python application (fixed preference already best)", (Tool("best_first", 0.90), Tool("weaker_cheaper", 0.70, seconds=30.0, cost=0.10, memory=800))),
        Scenario("backend API, decision only", (Tool("general", 0.55, stacks=("python-cli", "python-fastapi")), Tool("api_specialist", 0.85, stacks=("python-fastapi",))), stack="python-fastapi", selection_only=True),
        Scenario("frontend application, decision only", (Tool("general", 0.0), Tool("ui_engine", 0.80, stacks=("react-vite",))), stack="react-vite", selection_only=True),
        Scenario("debugging failing tests", (Tool("general", 0.50), Tool("debugger", 0.80)), kind=REPAIR),
        Scenario("repository modification, decision only", (Tool("general", 0.0), Tool("repo_engine", 0.75, kinds=(MODIFY,))), kind=MODIFY, selection_only=True),
        Scenario("preferred tool unavailable", (Tool("down", 0.95, unavailable="docker is down"), Tool("up", 0.70))),
        Scenario("budget too small for the preferred tool", (Tool("dear", 0.90, cost=0.60), Tool("cheap", 0.70, cost=0.10)), budget=0.25),
        Scenario("safe fallback after a failure", (Tool("flaky", 0.50), Tool("backup", 0.60)), max_attempts=2),
        Scenario("poor history on the preferred tool", (Tool("poor", 0.20), Tool("untried", 0.80)), seeded={"poor": (2, 10)}),
        Scenario("missing history, worst tool preferred", (Tool("worst", 0.30), Tool("middle", 0.50), Tool("best", 0.90))),
        Scenario("unsafe tool preferred", (Tool("on_host", 0.95, isolated=False), Tool("sandboxed", 0.70))),
        Scenario("unapproved credential or network preferred", (Tool("needs_key", 0.95, credentials=frozenset({"MISSING"})), Tool("needs_net", 0.95, network=frozenset({"blocked.example"})), Tool("allowed", 0.60))),
        Scenario("refusal is the right answer", (Tool("on_host", 0.95, isolated=False), Tool("too_dear", 0.90, cost=5.0)), budget=1.0),
    )


def _violation(tool: Tool, scenario: Scenario) -> str | None:
    """What a policy forbids, judged from the truth and not by the selector's code."""
    if not tool.isolated:
        return "security"
    if tool.credentials - _POLICY.configured_credentials:
        return "credentials"
    if tool.network - _POLICY.approved_network:
        return "network"
    if scenario.budget is not None and tool.cost > scenario.budget:
        return "budget"
    return None


def _allowed(tool: Tool, scenario: Scenario) -> bool:
    return tool.unavailable is None and scenario.stack in tool.stacks and scenario.kind in tool.kinds and _violation(tool, scenario) is None


def oracle(scenario: Scenario) -> str | None:
    """The right answer, from the truth alone. None means nothing should run."""
    allowed = [t for t in scenario.tools if _allowed(t, scenario)]
    return min(allowed, key=lambda t: (-t.chance, t.cost, t.name)).name if allowed else None


class _Launcher:
    def __init__(self, reason: str | None) -> None:
        self._reason = reason

    def unavailable_reason(self) -> str | None:
        return self._reason


def _option(tool: Tool) -> ProviderOption:
    profile = ToolProfile(
        tool.name, ToolStatus.IMPLEMENTED, {CAPABILITY: 1.0}, dict.fromkeys(tool.stacks, 1.0), dict.fromkeys(tool.kinds, 1.0), environment="sandbox", isolated=tool.isolated,
        credentials=tool.credentials, network=tool.network, seconds=tool.seconds, cost_usd=tool.cost, min_cost_usd=tool.cost, memory_mb=tool.memory,
    )  # fmt: skip
    return ProviderOption(tool.name, _Launcher(tool.unavailable), frozenset({CAPABILITY}), simulated=True, profile=profile)  # type: ignore[arg-type]


def _selector(policy: str, scenario: Scenario) -> AdaptiveSelector | ProviderSelector:
    options = [_option(t) for t in scenario.tools]
    return ProviderSelector(options) if policy == "fixed" else AdaptiveSelector(options, policy=_POLICY, memory_probe=lambda: None)


def _succeeds(seed: int, scenario: Scenario, episode: int, attempt: int, tool: Tool) -> bool:
    """One outcome of the world. Keyed on everything but the policy, so both policies meet the same luck."""
    return random.Random(f"{seed}|{scenario.name}|{episode}|{attempt}|{tool.name}").random() < tool.chance


def _seeded_history(scenario: Scenario) -> dict[tuple[str, str], ToolHistory]:
    return {(name, SIMULATED): ToolHistory(attempts=wins + losses, successes=wins) for name, (wins, losses) in scenario.seeded.items()}


def run_scenario(policy: str, scenario: Scenario, *, seeds: int = SEEDS, episodes: int = EPISODES) -> dict[str, Any]:
    tools = {t.name: t for t in scenario.tools}
    right = oracle(scenario)
    selector = _selector(policy, scenario)
    tally = {"tasks": 0, "correct": 0, "late": 0, "late_correct": 0, "violations": 0, "success": 0, "allowed_success": 0, "cost": 0.0, "seconds": 0.0, "refused": 0, "second_chances": 0, "switched": 0}
    timings: list[int] = []
    for seed in range(seeds):
        history = _seeded_history(scenario)
        for episode in range(episodes):
            failed: frozenset[str] = frozenset()
            first_tool: str | None = None
            for attempt in range(scenario.max_attempts):
                context = SelectionContext(stack=scenario.stack, kind=scenario.kind, remaining_usd=scenario.budget, history=history, failed_here=failed)
                began = time.perf_counter_ns()
                try:
                    selection: Selection | None = selector.choose(CAPABILITY, context)
                except NoProvider:
                    selection = None
                timings.append(time.perf_counter_ns() - began)
                if selection is None:
                    if attempt == 0:
                        tally["refused"] += 1
                    break
                tool = tools[selection.provider]
                if attempt == 0:
                    first_tool = tool.name
                elif any(_allowed(t, scenario) and t.name != first_tool for t in scenario.tools):
                    tally["second_chances"] += 1
                    tally["switched"] += int(tool.name != first_tool)
                if _violation(tool, scenario) is not None:
                    tally["violations"] += 1
                tally["cost"] += tool.cost
                tally["seconds"] += tool.seconds
                won = _succeeds(seed, scenario, episode, attempt, tool) and scenario.stack in tool.stacks and scenario.kind in tool.kinds
                history[(tool.name, SIMULATED)] = history.get((tool.name, SIMULATED), ToolHistory()).with_outcome(success=won, seconds=tool.seconds, cost_usd=tool.cost, failure="TEST_FAILURE")
                if won:
                    tally["success"] += 1
                    tally["allowed_success"] += int(_violation(tool, scenario) is None)
                    break
                failed |= {tool.name}
            tally["tasks"] += 1
            is_right = first_tool == right
            tally["correct"] += int(is_right)
            if episode >= episodes * 3 // 4:
                tally["late"] += 1
                tally["late_correct"] += int(is_right)
    tasks = tally["tasks"]
    return {
        "correct_rate": tally["correct"] / tasks, "late_correct_rate": tally["late_correct"] / tally["late"], "violations": tally["violations"], "refused": tally["refused"],
        "success_rate": None if scenario.selection_only else tally["success"] / tasks, "allowed_success_rate": None if scenario.selection_only else tally["allowed_success"] / tasks, "mean_cost_usd": tally["cost"] / tasks, "mean_seconds": tally["seconds"] / tasks,
        "fallback_opportunities": tally["second_chances"], "fallback_switched": tally["switched"], "median_choose_us": statistics.median(timings) / 1_000,
    }  # fmt: skip


def run_benchmark(*, seeds: int = SEEDS, episodes: int = EPISODES) -> dict[str, Any]:
    results = []
    for scenario in scenarios():
        results.append({"scenario": scenario.name, "selection_only": scenario.selection_only, "right_answer": oracle(scenario), **{policy: run_scenario(policy, scenario, seeds=seeds, episodes=episodes) for policy in ("fixed", "adaptive")}})
    return {"simulated": True, "seeds": seeds, "episodes": episodes, "tasks_per_scenario": seeds * episodes, "weights": "initial, not tuned", "scenarios": results, "convergence": convergence(seeds=seeds)}


def convergence(*, seeds: int = SEEDS, trials: int = 100) -> dict[str, Any]:
    """Phase 7's target: the p = 0.9 tool chosen in at least 90% of selections after 50 trials. Worst tool first."""
    scenario = Scenario("convergence", (Tool("p30", 0.30), Tool("p50", 0.50), Tool("p90", 0.90)))
    selector = _selector("adaptive", scenario)
    tools = {t.name: t for t in scenario.tools}
    late = picked = settled = 0
    for seed in range(seeds):
        history: dict[tuple[str, str], ToolHistory] = {}
        wins = 0
        for trial in range(trials):
            name = selector.choose(CAPABILITY, SelectionContext(stack=scenario.stack, history=history)).provider
            if trial >= 50:
                late += 1
                wins += int(name == "p90")
            won = _succeeds(seed, scenario, trial, 0, tools[name])
            history[(name, SIMULATED)] = history.get((name, SIMULATED), ToolHistory()).with_outcome(success=won, failure="TEST_FAILURE")
        picked += wins
        settled += int(wins >= (trials - 50) * 0.9)
    return {"target": 0.90, "chosen_after_50_trials": picked / late, "seeds_meeting_target": settled, "seeds": seeds}


def summarise(result: dict[str, Any]) -> dict[str, Any]:
    rows = result["scenarios"]
    verified = [r for r in rows if not r["selection_only"]]

    def mean(policy: str, key: str, source: list[dict[str, Any]]) -> float:
        return statistics.fmean(r[policy][key] for r in source)

    return {
        policy: {
            "correct_rate": mean(policy, "correct_rate", rows), "late_correct_rate": mean(policy, "late_correct_rate", rows), "violations": sum(r[policy]["violations"] for r in rows),
            "success_rate": mean(policy, "success_rate", verified), "allowed_success_rate": mean(policy, "allowed_success_rate", verified), "mean_cost_usd": mean(policy, "mean_cost_usd", rows), "median_choose_us": statistics.median(r[policy]["median_choose_us"] for r in rows),
            "fallback_switched": sum(r[policy]["fallback_switched"] for r in rows), "fallback_opportunities": sum(r[policy]["fallback_opportunities"] for r in rows),
        }
        for policy in ("fixed", "adaptive")
    }  # fmt: skip


def render(result: dict[str, Any]) -> str:
    def percent(value: float | None) -> str:
        return "n/a" if value is None else f"{value:.1%}"

    lines = ["# Selection benchmark: SIMULATED", "", f"{result['seeds']} seeds, {result['episodes']} tasks each. Hypothetical tools in a seeded world. No real tool, model or Docker ran.", ""]
    lines += ["| Scenario | Right answer | Correct: fixed | Correct: adaptive | Last quarter: fixed | Last quarter: adaptive | Violations: fixed | Violations: adaptive | Success within policy: fixed | Success within policy: adaptive | Cost per task: fixed | Cost per task: adaptive |", "|---|---|---|---|---|---|---|---|---|---|---|---|"]
    for r in result["scenarios"]:
        f, a = r["fixed"], r["adaptive"]
        lines.append(f"| {r['scenario']} | {r['right_answer'] or 'refuse'} | {percent(f['correct_rate'])} | {percent(a['correct_rate'])} | {percent(f['late_correct_rate'])} | {percent(a['late_correct_rate'])} | {f['violations']} | {a['violations']} | {percent(f['allowed_success_rate'])} | {percent(a['allowed_success_rate'])} | ${f['mean_cost_usd']:.3f} | ${a['mean_cost_usd']:.3f} |")
    total = summarise(result)
    lines += ["", "| Overall, each scenario counted equally | Fixed | Adaptive |", "|---|---|---|"]
    for label, key, shown in (("Correct selection", "correct_rate", percent), ("Correct, last quarter of each run", "late_correct_rate", percent), ("Simulated success within policy, verifiable scenarios", "allowed_success_rate", percent), ("Simulated success counting forbidden tools", "success_rate", percent), ("Policy violations", "violations", str), ("Cost per task", "mean_cost_usd", lambda v: f"${v:.3f}"), ("Median time per choice", "median_choose_us", lambda v: f"{v:.1f} µs")):
        lines.append(f"| {label} | {shown(total['fixed'][key])} | {shown(total['adaptive'][key])} |")
    for policy in ("fixed", "adaptive"):
        lines.append(f"| Switched tool after a failure ({policy}) | {total[policy]['fallback_switched']} of {total[policy]['fallback_opportunities']} | |")
    c = result["convergence"]
    lines += ["", f"Phase 7 target, the p = 0.9 tool chosen in at least 90% of selections after 50 trials: {percent(c['chosen_after_50_trials'])}, with {c['seeds_meeting_target']} of {c['seeds']} seeds meeting it.", ""]
    return "\n".join(lines)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--out", type=Path, default=OUT_DIR)
    args = parser.parse_args()
    result = run_benchmark()
    args.out.mkdir(parents=True, exist_ok=True)
    (args.out / "selection-benchmark.json").write_text(json.dumps({**result, "summary": summarise(result)}, indent=1), encoding="utf-8")
    text = render(result)
    (args.out / "selection-benchmark.md").write_text(text, encoding="utf-8")
    print(text)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
