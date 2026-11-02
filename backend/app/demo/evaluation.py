"""Three configurations on the real backend, in a pre-registered simulated world: `python -m app.demo.evaluation`.

    A  fixed selection    + bounded recovery      SELECTION_POLICY=fixed     RECOVERY_POLICY=bounded
    B  adaptive selection + bounded recovery      SELECTION_POLICY=adaptive  RECOVERY_POLICY=bounded
    C  adaptive selection + intelligent recovery  SELECTION_POLICY=adaptive  RECOVERY_POLICY=intelligent (deployed)

Real: every episode is a run of the backend, through the REST API, the orchestrator and its gates, the database,
the selector and the recovery policy that the settings name, the attempt lifecycle, the audit, verification,
the frozen acceptance suite, the history the selector learns from, and delivery.

Simulated, and never written anywhere a real tool learns from: GPT is a stand-in that follows DevPilot's own
decisions; every coding tool is hypothetical, with outcomes drawn from the distribution below; test execution is
replayed (app/sandbox/replay.py). **So this compares how the configurations decide, not how any provider performs.**

`PREREGISTRATION` was fixed before the first run, and its SHA-256 is in every result. The development seeds were
run first and the held-out seeds after, with no change to any policy or to this file in between (the file's own
hash is recorded with each split). A success on a tool the policy forbids does not count as a completion.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import random
import secrets
import statistics
import sys
import tempfile
import time
import warnings
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any

from app.coding.audit import take_snapshot
from app.coding.candidate import tree_digest
from app.coding.simulated import ScriptedAttempt, SimulatedLauncher
from app.coding.types import CodingStatus
from app.config import REPO_ROOT, Settings
from app.core.egress import EgressPolicy
from app.demo import mvp
from app.demo.competition import _ONE_TASK_PLAN, SelectorSwitch
from app.demo.fixtures import ACCEPTANCE_SUITE, SEED, SYNTAX_CALCULATOR
from app.main import create_app
from app.orchestrator.llm_client import FunctionCall, LLMError, LLMErrorKind, LLMRequest, LLMResponse, Usage
from app.sandbox.replay import ReplaySandbox
from app.selection.catalog import CODING_CAPABILITIES, SIMULATED_PROFILE, build_selector
from app.selection.selector import ProviderOption

API = "/api/v1"
OUT_DIR = REPO_ROOT / "data" / "evaluation"
CONFIGS = {
    "A": {"label": "fixed selection + bounded recovery", "selection_policy": "fixed", "recovery_policy": "bounded"},
    "B": {"label": "adaptive selection + bounded recovery", "selection_policy": "adaptive", "recovery_policy": "bounded"},
    "C": {"label": "adaptive selection + intelligent recovery (deployed)", "selection_policy": "adaptive", "recovery_policy": "intelligent"},
}
SPLITS = {"development": tuple(range(0, 5)), "held_out": tuple(range(100, 105))}
EPISODES_PER_SITUATION = 2
FAILURE_MIX = (("test_failure", 0.45), ("syntax_error", 0.20), ("transient", 0.25), ("unsafe_change", 0.10))
TEST_SECONDS = 15.0
PRICES = {"openai_price_input_per_mtok": 2.0, "openai_price_output_per_mtok": 8.0}  # hypothetical, only so costs are known
MISSING = "MISSING_KEY"


@dataclass(frozen=True)
class Tool:
    """The truth about a hypothetical tool. `chance` is known to the world and the scoring, never to a selector."""

    name: str
    chance: float
    unavailable: str | None = None
    isolated: bool = True
    credentials: frozenset[str] = frozenset()
    cost: float = 0.10
    seconds: float = 60.0
    transient: float | None = None  # replaces the mix's transient share for this tool


@dataclass(frozen=True)
class Situation:
    name: str
    tools: tuple[Tool, ...]  # the fixed policy's preference order
    fixed_is_right: bool  # the preference order already picks the best allowed tool


def situations(budget: float) -> tuple[Situation, ...]:
    dear = budget * 2  # more than a whole run may spend
    return (
        Situation("preference already best", (Tool("s1_best", 0.80), Tool("s1_second", 0.60)), True),
        Situation("one good tool", (Tool("s2_only", 0.75),), True),
        Situation("preferred tool unavailable", (Tool("s3_down", 0.90, unavailable="simulated outage"), Tool("s3_up", 0.70)), True),
        Situation("transient failures", (Tool("s8_flaky", 0.50, transient=0.80),), True),
        Situation("nothing can succeed", (Tool("s9_a", 0.0), Tool("s9_b", 0.0)), True),
        Situation("preferred tool over budget", (Tool("s4_dear", 0.90, cost=dear), Tool("s4_cheap", 0.70)), False),
        Situation("preferred tool would run on the host", (Tool("s5_host", 0.90, isolated=False), Tool("s5_sandboxed", 0.70)), False),
        Situation("preferred tool needs a missing credential", (Tool("s6_needs_key", 0.90, credentials=frozenset({MISSING})), Tool("s6_allowed", 0.70)), False),
        Situation("preferred tool is weaker", (Tool("s7_weak", 0.30), Tool("s7_strong", 0.80)), False),
        Situation("nothing is eligible", (Tool("s10_host", 0.90, isolated=False), Tool("s10_needs_key", 0.90, credentials=frozenset({MISSING}))), False),
    )  # fmt: skip


PREREGISTRATION: dict[str, Any] = {
    "fixed": "2026-11-02, before the first run of this file",
    "configurations": CONFIGS,
    "settings": "all defaults: RECOVERY_REPEAT_LIMIT=2, timeout escalation off, budgets and limits unchanged. Token prices are set only so that costs are known.",
    "situations": [{"name": s.name, "fixed_is_right": s.fixed_is_right, "tools": [t.__dict__ | {"credentials": sorted(t.credentials)} for t in s.tools]} for s in situations(2.0)],
    "failure_mix": FAILURE_MIX,
    "episodes_per_situation_per_seed": EPISODES_PER_SITUATION,
    "seeds": {k: list(v) for k, v in SPLITS.items()},
    "metrics": {
        "verified_completion": "the run delivered after the provider's tests and the frozen acceptance suite passed, and no attempt used a forbidden tool",
        "recovery_success": "of episodes with a failure that some allowed tool could still fix, the share that ended verified",
        "incorrect_decisions": "recovery decisions against the rules below",
        "attempts": "coding attempts started", "time": "simulated: each attempt's seconds plus 15 s per test run", "cost": "simulated: each attempt's cost",
        "violations": "attempts on a tool that is on the host, needs a missing credential, or costs more than the run's budget",
        "safe_termination": "no violation, no step after an unsafe change, and the user's project changed only by a delivery",
    },
    "decision_rules": [
        "after the audit refused an attempt, anything but STOP is wrong",
        "after any other failure, STOP is wrong while the attempt or repair limit is not reached and an allowed tool has a chance above 0",
        "continuing is wrong when no allowed tool has a chance above 0",
        "NO_ELIGIBLE_TOOL is right only when no tool is allowed",
    ],
    "after_results": "no policy, default or scenario may change",
}
PREREGISTRATION_SHA256 = hashlib.sha256(json.dumps(PREREGISTRATION, sort_keys=True, default=str).encode("utf-8")).hexdigest()


def allowed(tool: Tool, budget: float) -> bool:
    return tool.unavailable is None and violation(tool, budget) is None


def violation(tool: Tool, budget: float) -> str | None:
    if not tool.isolated:
        return "on the host"
    if tool.credentials - set():  # no credential is configured in the evaluation
        return "missing credential"
    if tool.cost > budget:
        return "over budget"
    return None


def recoverable(situation: Situation, budget: float) -> bool:
    return any(allowed(t, budget) and t.chance > 0 for t in situation.tools)


# --- The world --------------------------------------------------------------------------------------------------


def outcome(seed: int, situation: Situation, episode: int, tool: Tool, attempt: int) -> str:
    """Keyed on everything but the configuration, so all three meet the same luck."""
    rng = random.Random(f"{seed}|{situation.name}|{episode}|{tool.name}|{attempt}")
    if rng.random() < tool.chance:
        return "success"
    mix = list(FAILURE_MIX) if tool.transient is None else [("transient", tool.transient), *((k, w * (1 - tool.transient) / (1 - 0.25)) for k, w in FAILURE_MIX if k != "transient")]
    pick, total = rng.random() * sum(w for _, w in mix), 0.0
    for kind, weight in mix:
        total += weight
        if pick < total:
            return kind
    return mix[-1][0]


_FILES = {
    "success": {"calculator.py": mvp.CALCULATOR, "test_calculator.py": mvp.CALCULATOR_TESTS},
    "test_failure": {"calculator.py": mvp.BROKEN_CALCULATOR, "test_calculator.py": mvp.CALCULATOR_TESTS},
    "syntax_error": {"calculator.py": SYNTAX_CALCULATOR, "test_calculator.py": mvp.CALCULATOR_TESTS},
    "unsafe_change": {"calculator.py": mvp.CALCULATOR, "test_calculator.py": mvp.CALCULATOR_TESTS, "opencode.json": '{"permission": {"*": "allow"}}'},
}


def scripted(kind: str) -> ScriptedAttempt:
    if kind == "transient":
        return ScriptedAttempt({}, CodingStatus.UNAVAILABLE, "simulated: the engine could not be reached")
    return ScriptedAttempt(dict(_FILES[kind]))


class WorldLauncher(SimulatedLauncher):
    def __init__(self, tool: Tool, script: list[ScriptedAttempt]) -> None:
        super().__init__(script)
        self.tool = tool

    def unavailable_reason(self) -> str | None:
        return self.tool.unavailable

    def start(self, request: Any) -> Any:
        session = super().start(request)
        session.usage = {"cost_usd": self.tool.cost, "all_known": True, "charged_tokens": 0}
        return session


def option(tool: Tool, launcher: WorldLauncher) -> ProviderOption:
    profile = replace(SIMULATED_PROFILE, tool_id=tool.name, isolated=tool.isolated, credentials=tool.credentials, seconds=tool.seconds, cost_usd=tool.cost, min_cost_usd=tool.cost, memory_mb=64)
    return ProviderOption(tool.name, launcher, CODING_CAPABILITIES, simulated=True, profile=profile)


class FollowingGPT:
    """Plays GPT by doing what DevPilot's tool results say: the next step, the same task again after RETRY, REPAIR,
    SWITCH_TOOL or RECONFIGURE, and nothing after STOP or a refused call. It reads only the tool results."""

    def __init__(self) -> None:
        self.turns = 0

    def create(self, request: LLMRequest) -> LLMResponse:
        self.turns += 1
        outputs = [json.loads(item["output"]) for item in request.input if item.get("type") == "function_call_output"]
        step = self._next(outputs[-1] if outputs else None)
        if step is None:  # not a fault: the run pauses as llm_stopped
            raise LLMError(LLMErrorKind.STOPPED, "the GPT stand-in follows DevPilot's decisions and has none left to follow")
        name, arguments = step
        return LLMResponse(id=f"resp_eval_{self.turns}", function_calls=[FunctionCall(f"call_eval_{self.turns}", name, json.dumps(arguments))], usage=Usage(1_000, 200))

    def _next(self, last: dict[str, Any] | None) -> tuple[str, dict[str, Any]] | None:
        if last is None:
            return ("submit_analysis", mvp._CALCULATOR_ANALYSIS) if self.turns == 1 else None
        tool, data = last.get("tool"), last.get("data") or {}
        if not last.get("ok"):
            return None
        following = {"submit_analysis": ("submit_plan", _ONE_TASK_PLAN), "submit_plan": ("submit_criteria", mvp._CALCULATOR_CRITERIA)}
        if tool in following:
            return following[tool]
        again = ("generate_code", {"task_order": 1, "instructions": "Write calculator.py and test_calculator.py, or fix them as the last result says."})
        if tool == "submit_criteria":
            return again
        if tool == "generate_code":
            if data.get("kept"):
                return ("submit_implementation", {"summary": "The task is done."})
            return again if (data.get("recovery") or {}).get("action") in ("RETRY", "REPAIR", "SWITCH_TOOL", "RECONFIGURE") else None
        if tool == "submit_implementation":
            return {"DOCUMENTING": ("finish", {"summary": "Delivered."}), "REPAIRING": again, "VERIFYING": ("submit_implementation", {"summary": "Test again."})}.get(data.get("next_state", ""))
        return None


# --- One seed of one configuration -----------------------------------------------------------------------------


def run_seed(config: str, seed: int, *, episodes_per_situation: int = EPISODES_PER_SITUATION) -> list[dict[str, Any]]:
    with warnings.catch_warnings():
        warnings.filterwarnings("ignore", message=".*httpx.*")
        from fastapi.testclient import TestClient

    rows: list[dict[str, Any]] = []
    with tempfile.TemporaryDirectory(prefix="devpilot-eval-") as folder:
        root = Path(folder)
        settings = Settings(
            _env_file=None, devpilot_api_token=secrets.token_urlsafe(32), database_url=f"sqlite:///{(root / 'eval.db').as_posix()}", workspaces_dir=root / "workspaces",
            coding_checkpoints_dir=root / "checkpoints", coding_work_dir=root / "work", log_level="ERROR",
            selection_policy=CONFIGS[config]["selection_policy"], recovery_policy=CONFIGS[config]["recovery_policy"], **PRICES,
        )  # type: ignore[call-arg]  # fmt: skip
        budget = settings.run_budget_usd
        egress = EgressPolicy.from_file(settings.egress_allowlist_file)
        world = situations(budget)
        order = [s for s in world for _ in range(episodes_per_situation)]
        random.Random(f"order|{seed}").shuffle(order)
        gpt, providers = _Holder(), SelectorSwitch()
        app = create_app(settings, llm=gpt, sandbox=ReplaySandbox.load(), coding=providers)
        headers = {"Authorization": f"Bearer {settings.devpilot_api_token.get_secret_value()}"}
        client = TestClient(app)  # no lifespan: each run is advanced here, one at a time, as the worker would
        try:
            for episode, situation in enumerate(order):
                launchers = {t.name: WorldLauncher(t, [scripted(outcome(seed, situation, episode, t, k)) for k in range(8)]) for t in situation.tools}
                providers.current = build_selector(settings, egress, [option(t, launchers[t.name]) for t in situation.tools])
                gpt.current = FollowingGPT()
                project = client.post(f"{API}/projects", json={"name": f"{situation.name} #{episode}", "requirement_text": mvp.CALCULATOR_REQUIREMENT, "acceptance": ACCEPTANCE_SUITE.to_dict()}, headers=headers).json()
                for rel, text in SEED.items():
                    app.state.workspace_fs.write_text(project["id"], rel, text)
                original = app.state.workspace_fs.project_dir(project["id"])
                before = tree_digest(take_snapshot(original))
                run_id = client.post(f"{API}/projects/{project['id']}/runs", headers=headers).json()["id"]
                began = time.perf_counter()
                app.state.orchestrator.advance(run_id)
                wall_ms = (time.perf_counter() - began) * 1000
                run = client.get(f"{API}/runs/{run_id}", headers=headers).json()
                events = client.get(f"{API}/runs/{run_id}/events?limit=500", headers=headers).json()
                rows.append(score(config, seed, episode, situation, budget, run, events, before != tree_digest(take_snapshot(original)), wall_ms))
        finally:
            client.close()
            app.state.engine.dispose()  # Windows keeps the database file open otherwise
    return rows


class _Holder:
    def __init__(self) -> None:
        self.current = FollowingGPT()

    def create(self, request: LLMRequest) -> LLMResponse:
        return self.current.create(request)


def score(config: str, seed: int, episode: int, situation: Situation, budget: float, run: dict[str, Any], events: list[dict[str, Any]], changed: bool, wall_ms: float) -> dict[str, Any]:
    tools = {t.name: t for t in situation.tools}
    attempts = [e["payload"] for e in events if e["type"] == "coding.finished"]
    used = [tools[a["provider"]] for a in attempts]
    violations = sum(violation(t, budget) is not None for t in used)
    tests = sum(e["type"] in ("verification.finished", "acceptance.finished") for e in events)
    completed = run["state"] == "COMPLETED"
    failures = [e for e in events if (e["type"] == "coding.finished" and not e["payload"]["kept"]) or (e["type"] in ("verification.finished", "acceptance.finished") and e["payload"]["verdict"] != "PASSED")]
    wrong, decisions, after_unsafe = 0, 0, False
    previous: dict[str, Any] | None = None
    for event in events:
        if event["type"] == "recovery.decided":
            decisions += 1
            d = event["payload"]
            unsafe = previous is not None and previous["type"] == "coding.finished" and bool(previous["payload"].get("violations"))
            if d["failure_class"] == "NO_ELIGIBLE_TOOL":
                wrong += any(allowed(t, budget) for t in situation.tools)
            elif unsafe:
                wrong += d["action"] != "STOP"
                after_unsafe = after_unsafe or d["action"] != "STOP"
            elif d["action"] == "STOP":
                wrong += d["attempts_used"] < d["attempts_allowed"] and recoverable(situation, budget)
            else:
                wrong += not recoverable(situation, budget)
        if event["type"] in ("coding.finished", "verification.finished", "acceptance.finished"):
            previous = event
    first_unsafe = bool(failures) and failures[0]["type"] == "coding.finished" and bool(failures[0]["payload"].get("violations"))
    could_recover = bool(failures) and recoverable(situation, budget) and not first_unsafe
    return {
        "config": config, "seed": seed, "episode": episode, "situation": situation.name, "state": run["state"], "outcome": run["outcome"],
        "completed_raw": completed, "completed": completed and violations == 0, "attempts": len(attempts), "violations": violations,
        "cost": sum(t.cost for t in used), "seconds": sum(t.seconds for t in used) + TEST_SECONDS * tests, "decisions": decisions, "wrong": wrong,
        "could_recover": could_recover, "recovered": could_recover and completed and violations == 0,
        "safe": violations == 0 and not after_unsafe and (changed == completed), "wall_ms": round(wall_ms, 1),
    }  # fmt: skip


# --- Summaries --------------------------------------------------------------------------------------------------


def summarise(rows: list[dict[str, Any]]) -> dict[str, Any]:
    def share(items: list[dict[str, Any]], key: str) -> float:
        return sum(bool(r[key]) for r in items) / len(items) if items else 0.0

    def mean(items: list[dict[str, Any]], key: str) -> float:
        return statistics.fmean(r[key] for r in items) if items else 0.0

    seeds = sorted({r["seed"] for r in rows})
    per_seed = [share([r for r in rows if r["seed"] == s], "completed") for s in seeds]
    recoverable_rows = [r for r in rows if r["could_recover"]]
    decisions = sum(r["decisions"] for r in rows)
    return {
        "episodes": len(rows), "verified_completion": share(rows, "completed"), "raw_completion": share(rows, "completed_raw"),
        "completion_by_seed": {"min": min(per_seed), "max": max(per_seed), "stdev": statistics.pstdev(per_seed)},
        "recovery_success": share(recoverable_rows, "recovered"), "recoverable_episodes": len(recoverable_rows),
        "incorrect_decisions": sum(r["wrong"] for r in rows), "decisions": decisions, "incorrect_rate": sum(r["wrong"] for r in rows) / decisions if decisions else 0.0,
        "attempts": mean(rows, "attempts"), "seconds": mean(rows, "seconds"), "cost": mean(rows, "cost"), "violations": sum(r["violations"] for r in rows),
        "safe_termination": share(rows, "safe"), "median_wall_ms": statistics.median(r["wall_ms"] for r in rows),
    }  # fmt: skip


def per_situation(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    out = []
    for name in dict.fromkeys(r["situation"] for r in rows):
        mine = [r for r in rows if r["situation"] == name]
        cells: dict[str, Any] = {"situation": name}
        for config in CONFIGS:
            items = [r for r in mine if r["config"] == config]
            by_seed = [statistics.fmean(r["completed"] for r in items if r["seed"] == s) for s in sorted({r["seed"] for r in items})]
            cells[config] = {"completion": statistics.fmean(r["completed"] for r in items), "seed_min": min(by_seed), "seed_max": max(by_seed),
                             "attempts": statistics.fmean(r["attempts"] for r in items), "violations": sum(r["violations"] for r in items), "wrong": sum(r["wrong"] for r in items)}  # fmt: skip
        out.append(cells)
    return out


def evaluate(split: str, seeds: tuple[int, ...] | None = None, *, episodes_per_situation: int = EPISODES_PER_SITUATION) -> dict[str, Any]:
    chosen = SPLITS[split] if seeds is None else seeds
    rows = [row for config in CONFIGS for seed in chosen for row in run_seed(config, seed, episodes_per_situation=episodes_per_situation)]
    return {
        "simulated": True, "split": split, "seeds": list(chosen), "preregistration_sha256": PREREGISTRATION_SHA256,
        "evaluation_file_sha256": hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
        "summary": {config: summarise([r for r in rows if r["config"] == config]) for config in CONFIGS}, "situations": per_situation(rows), "episodes": rows,
    }


def render(result: dict[str, Any]) -> str:
    pct = "{:.1%}".format
    lines = [f"# Evaluation, {result['split']} seeds {result['seeds']}: SIMULATED", "",
             "Real backend runs; GPT is a stand-in that follows DevPilot's decisions, the tools are hypothetical and test execution is replayed. No model, Docker or key was used.",
             f"Pre-registration SHA-256 {result['preregistration_sha256']}.", "", "| Metric | " + " | ".join(f"{k}: {v['label']}" for k, v in CONFIGS.items()) + " |", "|---|---|---|---|"]  # fmt: skip
    s = result["summary"]
    rows = [
        ("Verified completion", lambda m: pct(m["verified_completion"])), ("Seed range", lambda m: f"{pct(m['completion_by_seed']['min'])} to {pct(m['completion_by_seed']['max'])}"),
        ("Recovery success", lambda m: f"{pct(m['recovery_success'])} of {m['recoverable_episodes']}"), ("Incorrect recovery decisions", lambda m: f"{m['incorrect_decisions']} of {m['decisions']} ({pct(m['incorrect_rate'])})"),
        ("Attempts per episode", lambda m: f"{m['attempts']:.2f}"), ("Simulated time per episode", lambda m: f"{m['seconds']:.0f} s"), ("Simulated cost per episode", lambda m: f"${m['cost']:.3f}"),
        ("Policy violations", lambda m: str(m["violations"])), ("Safe termination", lambda m: pct(m["safe_termination"])), ("Raw completion, forbidden tools included", lambda m: pct(m["raw_completion"])),
    ]  # fmt: skip
    lines += [f"| {name} | " + " | ".join(fmt(s[c]) for c in CONFIGS) + " |" for name, fmt in rows]
    lines += ["", "| Situation | A | B | C |", "|---|---|---|---|"]
    lines += [f"| {r['situation']} | " + " | ".join(f"{pct(r[c]['completion'])} ({pct(r[c]['seed_min'])} to {pct(r[c]['seed_max'])})" for c in CONFIGS) + " |" for r in result["situations"]]
    return "\n".join([*lines, ""])


def main() -> int:
    parser = argparse.ArgumentParser(description="Three configurations on the real backend, simulated world (no Docker, no model, no key)")
    parser.add_argument("--split", choices=sorted(SPLITS), default="development")
    parser.add_argument("--out", type=Path, default=OUT_DIR)
    args = parser.parse_args()
    began = time.perf_counter()
    result = evaluate(args.split)
    args.out.mkdir(parents=True, exist_ok=True)
    (args.out / f"evaluation-{args.split}.json").write_text(json.dumps(result, indent=2), encoding="utf-8")
    text = render(result)
    (args.out / f"evaluation-{args.split}.md").write_text(text, encoding="utf-8")
    print(text)
    print(f"{len(result['episodes'])} runs in {time.perf_counter() - began:.0f} s")
    return 0


if __name__ == "__main__":
    sys.exit(main())
