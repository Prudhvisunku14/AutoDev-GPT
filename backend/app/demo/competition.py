"""The competition demonstration, offline and free: `python -m app.demo.competition` (add `--serve` for the dashboard).

Real here: the REST API, GPT's tool loop and its gates, the database, adaptive selection and its eligibility
rules, the attempt lifecycle (A1 and A2), the audit, the disposable copy, the verdict, the frozen acceptance
suite, failure classification, recovery, delivery and the report.

Simulated, and named in every report's `provenance`:
- GPT's replies: FakeLLM plays a fixed script per scenario. No model is called and no key is read.
- Every coding provider: SimulatedLauncher writes fixed files from app/demo/fixtures.py. No model writes code.
- Test execution: ReplaySandbox answers with recorded runs of those exact files. It starts no container, and
  any file it has no recording for gets "needs the Docker sandbox". It is not a Docker run.

OpenCode appears with its real profile, and the real eligibility policy refuses it offline (no OPENAI_API_KEY
is configured and its network is not approved). Its launcher is never started.
"""

from __future__ import annotations

import argparse
import json
import secrets
import shutil
import sys
import time
import warnings
from collections.abc import Callable
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any

from app.coding.audit import take_snapshot
from app.coding.candidate import tree_digest
from app.coding.launcher import LaunchRefused, LaunchRequest
from app.coding.simulated import PROVIDER_NAME, ScriptedAttempt, SimulatedLauncher
from app.coding.types import Termination
from app.config import REPO_ROOT, Settings
from app.core.egress import EgressPolicy
from app.demo import mvp
from app.demo.fixtures import ACCEPTANCE_SUITE, LENIENT_CALCULATOR, LENIENT_TESTS, SEED
from app.main import create_app
from app.orchestrator.fake_llm import FakeLLM, Step, call
from app.orchestrator.llm_client import LLMClient, LLMRequest, LLMResponse
from app.sandbox.replay import ReplaySandbox
from app.selection.catalog import CODING_CAPABILITIES, OPENCODE_NAME, SIMULATED_PROFILE, build_selector, opencode_profile
from app.selection.model import SelectionContext
from app.selection.selector import ProviderOption, Selection, Selector
from app.verification.report import render_markdown

API = "/api/v1"
OUT_DIR = REPO_ROOT / "data" / "competition-demo"
MARKER = ".devpilot-demo"  # only a folder holding this is ever cleared
_DEADLINE_SECONDS = 60


# --- What lets one app play several scripted scenarios, one run at a time ----------------------------------------


class ScriptSwitch:
    """GPT for the run in progress: each scenario brings its own FakeLLM script."""

    def __init__(self) -> None:
        self.current = FakeLLM()

    def create(self, request: LLMRequest) -> LLMResponse:
        return self.current.create(request)


class SelectorSwitch:
    """The selector for the run in progress: each scenario brings its own providers."""

    def __init__(self) -> None:
        self.current: Selector | None = None
        self._idle = SimulatedLauncher([])

    def first_launcher(self) -> Any:
        return self.current.first_launcher() if self.current is not None else self._idle

    def choose(self, capability: str, context: SelectionContext | None = None) -> Selection:
        assert self.current is not None, "a scenario sets its providers before its run starts"
        return self.current.choose(capability, context)


class OfflineOpenCode:
    """Stands in for the OpenCode launcher, which needs Docker. Eligibility refuses OpenCode first, so this never starts."""

    name = OPENCODE_NAME

    def unavailable_reason(self) -> str | None:
        return "the OpenCode launcher needs Docker, which this offline demonstration does not start"

    def start(self, request: LaunchRequest) -> Any:
        raise LaunchRefused(self.unavailable_reason() or "")

    def writer_for(self, container_id: str | None) -> Any:
        return _NoWriter()

    def usage_for(self, run_id: str, call_id: str) -> dict[str, Any]:
        return {}

    def sweep(self) -> None:
        return None


class _NoWriter:
    def terminate(self) -> Termination:
        return Termination(True, "no OpenCode container was ever started")


class _Down(SimulatedLauncher):
    def __init__(self, reason: str, script: list[ScriptedAttempt]) -> None:
        super().__init__(script)
        self._reason = reason

    def unavailable_reason(self) -> str | None:
        return self._reason


# --- The scenarios ---------------------------------------------------------------------------------------------------

GOOD = ScriptedAttempt({"calculator.py": mvp.CALCULATOR})
TESTS = ScriptedAttempt({"test_calculator.py": mvp.CALCULATOR_TESTS})
SUBTRACT_BUG = ScriptedAttempt({"calculator.py": mvp.BROKEN_CALCULATOR})
UNSAFE = ScriptedAttempt({"calculator.py": mvp.CALCULATOR, "opencode.json": '{"permission": {"*": "allow"}}'})
LENIENT_BOTH = ScriptedAttempt({"calculator.py": LENIENT_CALCULATOR, "test_calculator.py": LENIENT_TESTS})
GOOD_BOTH = ScriptedAttempt({"calculator.py": mvp.CALCULATOR, "test_calculator.py": mvp.CALCULATOR_TESTS})

_ONE_TASK_PLAN: dict[str, Any] = {
    **mvp._CALCULATOR_PLAN,
    "summary": "One module and its unittest file, in one task.",
    "tasks": [{"title": "Calculator and its tests", "description": "calculator.py with the four operations, and test_calculator.py with a test for each criterion.",
               "capability": "code_generation", "requirement_refs": ["R-001", "R-002", "R-003"], "features": mvp._PY}],
}  # fmt: skip


def _opening(plan: dict[str, Any] | None = None) -> list[Step]:
    return [call("submit_analysis", mvp._CALCULATOR_ANALYSIS), call("submit_plan", plan or mvp._CALCULATOR_PLAN), call("submit_criteria", mvp._CALCULATOR_CRITERIA)]


def _generate(task: int, text: str) -> Step:
    return call("generate_code", {"task_order": task, "instructions": text})


def _submit(text: str = "Every task is done.") -> Step:
    return call("submit_implementation", {"summary": text})


def _finish() -> Step:
    return call("finish", {"summary": "The calculator is built, its tests pass and the independent acceptance suite passes."})


def _simulated(name: str, script: list[ScriptedAttempt], *, down: str | None = None, isolated: bool = True) -> ProviderOption:
    launcher = _Down(down, script) if down else SimulatedLauncher(script)
    return ProviderOption(name, launcher, CODING_CAPABILITIES, simulated=True, profile=replace(SIMULATED_PROFILE, tool_id=name, isolated=isolated))


def _opencode(settings: Settings) -> ProviderOption:
    return ProviderOption(OPENCODE_NAME, OfflineOpenCode(), CODING_CAPABILITIES, simulated=False, profile=opencode_profile(settings))  # type: ignore[arg-type]


@dataclass(frozen=True)
class Scenario:
    key: str
    title: str
    shows: str
    gpt: list[Step]
    providers: Callable[[Settings], list[ProviderOption]]
    expected_state: str
    expected_outcome: str
    expected_provider: str | None = None
    expected_acceptance: tuple[str, ...] = ()  # the verdict of each acceptance run, in order
    checks: dict[str, Any] = field(default_factory=dict)


def scenarios() -> list[Scenario]:
    build = [*_opening(), _generate(1, "Write calculator.py."), _generate(2, "Write test_calculator.py."), _submit(), _finish()]
    return [
        Scenario(
            "demo1_success", "Demo 1: a Python calculator, generated and verified", "The whole path once: plan, selection, code, the provider's tests, the evaluator's acceptance suite, delivery.",
            build, lambda s: [_opencode(s), _simulated(PROVIDER_NAME, [GOOD, TESTS])], "COMPLETED", "completed", PROVIDER_NAME, ("PASSED",),
        ),
        Scenario(
            "demo2_repair", "Demo 2: a unit-test failure, diagnosed and repaired", "subtract() returns b minus a. The tests catch it, the failure is classified, a repair is requested and verified.",
            [*_opening(), _generate(1, "Write calculator.py."), _generate(2, "Write test_calculator.py."), _submit(), _generate(1, "subtract() must return a minus b. Fix it."), _submit("The fix is in."), _finish()],
            lambda s: [_opencode(s), _simulated(PROVIDER_NAME, [SUBTRACT_BUG, TESTS, GOOD])], "COMPLETED", "completed", PROVIDER_NAME, ("PASSED",),
        ),
        Scenario(
            "demo3_security", "Demo 3: an attempt that changes a protected file is refused", "The attempt writes opencode.json, which could reconfigure the coding engine. The audit refuses it, the copy is restored, the run stops and the user's project is untouched.",
            [*_opening(), _generate(1, "Write calculator.py.")], lambda s: [_opencode(s), _simulated(PROVIDER_NAME, [UNSAFE])], "FAILED", "coding_failed", PROVIDER_NAME,
        ),
        Scenario(
            "demo4_alternative", "Demo 4a: the preferred provider is unavailable, an approved alternative is chosen", "OpenCode is refused by policy (no key, no approved network) and the first simulated provider is down. The selector chooses the approved backup.",
            build, lambda s: [_opencode(s), _simulated("simulated_primary", [GOOD, TESTS], down="simulated outage: this provider is down"), _simulated("simulated_backup", [GOOD, TESTS])],
            "COMPLETED", "completed", "simulated_backup", ("PASSED",),
        ),
        Scenario(
            "demo4_refusal", "Demo 4b: no provider is eligible, so nothing runs", "OpenCode is refused by policy and the only other provider would run on the host. Nothing starts and the run ends safely.",
            [*_opening(), _generate(1, "Write calculator.py.")], lambda s: [_opencode(s), _simulated("host_runner", [GOOD], isolated=False)], "FAILED", "no_eligible_tool",
        ),
        Scenario(
            "acceptance_check", "Acceptance check: a self-graded pass is caught", "The provider's code returns infinity for a division by zero, and its own test agrees, so its tests pass. The evaluator's frozen suite fails AT-005 (4 of 5), the repair is told only the criterion, and the second attempt passes all five.",
            [*_opening(_ONE_TASK_PLAN), _generate(1, "Write calculator.py and test_calculator.py."), _submit(), _generate(1, "Independent acceptance says AT-005 is not met: divide(a, 0) must raise ZeroDivisionError. Fix divide and its test."), _submit("The fix is in."), _finish()],
            lambda s: [_opencode(s), _simulated(PROVIDER_NAME, [LENIENT_BOTH, GOOD_BOTH])], "COMPLETED", "completed", PROVIDER_NAME, ("FAILED", "PASSED"),
        ),
    ]  # fmt: skip


# --- Running them ---------------------------------------------------------------------------------------------------


def demo_settings(data_dir: Path, token: str) -> Settings:
    return Settings(
        _env_file=None,  # never the real .env: no real key is involved
        devpilot_api_token=token, database_url=f"sqlite:///{(data_dir / 'demo.db').as_posix()}", workspaces_dir=data_dir / "workspaces",
        coding_checkpoints_dir=data_dir / "checkpoints", coding_work_dir=data_dir / "work", log_level="WARNING",
    )  # type: ignore[call-arg]  # fmt: skip


def _hashes(root: Path) -> dict[str, str]:
    snapshot = take_snapshot(root)
    return {"tree": tree_digest(snapshot), **{rel: state.sha256 for rel, state in snapshot.files.items()}}


def run_all(out: Path, *, only: str | None = None) -> tuple[list[dict[str, Any]], Settings]:
    with warnings.catch_warnings():
        warnings.filterwarnings("ignore", message=".*httpx.*")
        from fastapi.testclient import TestClient

    data_dir = out / "data"
    data_dir.mkdir(parents=True)
    settings = demo_settings(data_dir, secrets.token_urlsafe(32))
    egress = EgressPolicy.from_file(settings.egress_allowlist_file)
    gpt, providers = ScriptSwitch(), SelectorSwitch()
    app = create_app(settings, llm=gpt, sandbox=ReplaySandbox.load(), coding=providers)
    headers = {"Authorization": f"Bearer {settings.devpilot_api_token.get_secret_value()}"}
    results: list[dict[str, Any]] = []
    with TestClient(app) as client:
        for scenario in scenarios():
            if only and scenario.key != only:
                continue
            gpt.current = FakeLLM(*scenario.gpt)
            providers.current = build_selector(settings, egress, scenario.providers(settings))
            body = {"name": scenario.title[:200], "requirement_text": mvp.CALCULATOR_REQUIREMENT, "acceptance": ACCEPTANCE_SUITE.to_dict()}
            project = client.post(f"{API}/projects", json=body, headers=headers)
            project.raise_for_status()
            project_id = project.json()["id"]
            for rel, content in SEED.items():
                app.state.workspace_fs.write_text(project_id, rel, content)
            original = app.state.workspace_fs.project_dir(project_id)
            before = _hashes(original)
            run = client.post(f"{API}/projects/{project_id}/runs", headers=headers).json()
            client.post(f"{API}/runs/{run['id']}/start", headers=headers).raise_for_status()
            deadline = time.monotonic() + _DEADLINE_SECONDS
            while time.monotonic() < deadline:
                run = client.get(f"{API}/runs/{run['id']}", headers=headers).json()
                if run["ended_at"] or run["pause_reason"]:
                    break
                time.sleep(0.05)
            answer = client.get(f"{API}/runs/{run['id']}/report", headers=headers)
            report = answer.json() if answer.status_code == 200 else {"missing": True}
            results.append(_result(scenario, run, report, before, _hashes(original), original, gpt.current.remaining))
    return results, settings


def _result(scenario: Scenario, run: dict[str, Any], report: dict[str, Any], before: dict[str, str], after: dict[str, str], original: Path, gpt_left: int) -> dict[str, Any]:
    delivered = bool(report.get("delivered_files"))
    providers_used = sorted({s["provider"] for s in report.get("selection", [])})
    acceptance_runs = tuple(a["verdict"] for a in report.get("acceptance_runs", []))
    seed_intact = all((original / rel).read_text(encoding="utf-8") == text for rel, text in SEED.items())
    simulated = report.get("provenance", {}).get("simulated", [])
    checks = {
        "state": run["state"] == scenario.expected_state,
        "outcome": run["outcome"] == scenario.expected_outcome,
        "provider": scenario.expected_provider is None or providers_used == [scenario.expected_provider],
        "acceptance": acceptance_runs == scenario.expected_acceptance,
        "project_changed_only_by_a_delivery": (before == after) != delivered,
        "user_file_intact": seed_intact,
        "labelled_simulated": "GPT orchestration" in simulated and (not report.get("selection") or "coding provider" in simulated),  # no provider ran: none to label
    }
    return {
        "key": scenario.key, "title": scenario.title, "shows": scenario.shows, "as_expected": all(checks.values()), "checks": checks,
        "expected": {"state": scenario.expected_state, "outcome": scenario.expected_outcome, "provider": scenario.expected_provider, "acceptance": list(scenario.expected_acceptance)},
        "actual": {"state": run["state"], "outcome": run["outcome"], "detail": run["status_detail"], "providers": providers_used, "acceptance": list(acceptance_runs), "delivered": delivered},
        "gpt_steps_left": gpt_left, "report": report,
    }  # fmt: skip


def describe(result: dict[str, Any]) -> str:
    report, actual = result["report"], result["actual"]
    lines = [f"=== {result['title']}: {'as expected' if result['as_expected'] else 'NOT AS EXPECTED'}", f"    {result['shows']}"]
    for choice in report.get("selection", []):
        refused = "; ".join(f"{r['tool']}: {', '.join(r['reasons'])}" for r in choice.get("rejected", [])) or "none"
        lines.append(f"    selection, task {choice['task']}: {choice['provider']} ({'simulated' if choice['simulated'] else 'real'}). Refused: {refused}")
    for attempt in report.get("coding_attempts", []):
        lines.append(f"    coding, task {attempt['task_order']}: {attempt['status']}, kept={attempt['kept']}, phase {attempt['phase']}" + "".join(f"; audit refused {v}" for v in attempt.get("violations") or []))
    for check in report.get("verifications", []):
        lines.append(f"    provider's tests: {check['verdict']}, {check['passed']} of {check['ran']} passed. {check['reason']}")
    for run in report.get("acceptance_runs", []):
        lines.append(f"    independent acceptance: {run['verdict']}, {run['passed']} of {run['total']} criteria. {run['reason']}")
    for decision in report.get("recovery", []):
        lines.append(f"    recovery: {decision.get('category') or decision['failure_class']} -> {decision['action']}. {decision['reason']}")
        if decision.get("failure_class") == "NO_ELIGIBLE_TOOL":
            lines += [f"      refused: {why}" for why in decision.get("evidence", [])]
    lines.append(f"    final: {actual['state']} ({actual['outcome']}); delivered: {[f['path'] for f in report.get('delivered_files', [])] or 'nothing'}")
    lines.append(f"    simulated: {', '.join(report.get('provenance', {}).get('simulated', []))}")
    return "\n".join(lines)


def write_reports(out: Path, results: list[dict[str, Any]]) -> None:
    reports = out / "reports"
    reports.mkdir(parents=True, exist_ok=True)
    for result in results:
        (reports / f"{result['key']}.json").write_text(json.dumps(result, indent=2), encoding="utf-8")
        if "run" in result["report"]:
            (reports / f"{result['key']}.md").write_text(f"# {result['title']}\n\n{result['shows']}\n\n" + render_markdown(result["report"]), encoding="utf-8")
    summary = {"simulated": ["GPT orchestration (FakeLLM)", "coding providers (SimulatedLauncher)", "test execution (ReplaySandbox, not Docker)"],
               "scenarios": [{k: r[k] for k in ("key", "title", "as_expected", "expected", "actual", "checks")} for r in results]}  # fmt: skip
    (out / "competition-demo.json").write_text(json.dumps(summary, indent=2), encoding="utf-8")
    (out / "competition-demo.md").write_text("\n\n".join(describe(r) for r in results) + "\n", encoding="utf-8")


def _prepare(out: Path) -> None:
    """A previous demonstration's folder is cleared. Any other non-empty folder is left alone."""
    if out.exists() and any(out.iterdir()):
        if not (out / MARKER).is_file():
            raise SystemExit(f"{out} is not empty and was not made by this demonstration; choose another --out")
        shutil.rmtree(out)
    out.mkdir(parents=True, exist_ok=True)
    (out / MARKER).write_text("made by python -m app.demo.competition\n", encoding="utf-8")


def serve(settings: Settings, port: int) -> None:
    """The dashboard over the demonstration's database. Read-only in effect: no provider is wired, and any new run
    would meet an empty GPT script."""
    import uvicorn

    app = create_app(settings, llm=FakeLLM())
    token = settings.devpilot_api_token.get_secret_value()
    print(f"\nDashboard: http://127.0.0.1:{port}/ui/#token={token}\nLocal only. Press Ctrl+C to stop.", flush=True)  # the server blocks next: piped output must not wait in a buffer
    uvicorn.run(app, host="127.0.0.1", port=port, log_config=None, access_log=False)


def main() -> int:
    parser = argparse.ArgumentParser(description="DevPilot's offline competition demonstration (no Docker, no model, no key)")
    parser.add_argument("--out", type=Path, default=OUT_DIR, help=f"where the reports and the demonstration's database go (default {OUT_DIR})")
    parser.add_argument("--only", help="run one scenario by its key")
    parser.add_argument("--serve", action="store_true", help="then serve the dashboard on 127.0.0.1")
    parser.add_argument("--port", type=int, default=8765)
    args = parser.parse_args()
    _prepare(args.out)
    print("Offline demonstration. Simulated in every scenario: GPT (FakeLLM), the coding providers and test execution (replayed, not Docker).\n")
    results, settings = run_all(args.out, only=args.only)
    write_reports(args.out, results)
    for result in results:
        print(describe(result), end="\n\n")
    matched = sum(r["as_expected"] for r in results)
    print(f"{matched} of {len(results)} scenarios ended as expected. Reports: {args.out / 'reports'}", flush=True)
    if args.serve:
        serve(settings, args.port)
    return 0 if matched == len(results) else 1


if __name__ == "__main__":
    sys.exit(main())
