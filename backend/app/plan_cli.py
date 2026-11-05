"""Plan an application from your own prompt, in the terminal: the real GPT orchestrator, planning only.

Run from the backend folder (the `edge` package is found from there):

    python -m app.plan_cli                            # dry run: shows the first request and its worst case; sends nothing
    python -m app.plan_cli --live                     # real GPT: asks for the prompt, then for a typed "yes"
    python -m app.plan_cli --live --prompt "Build ..."

The run goes through the existing app, database, orchestrator, gate checks and approval queue. GPT is offered only
the analysis and planning tools: no repository, Drive, sandbox or coding tool is wired. Once the plan is accepted the
run stops before acceptance criteria, and it is closed, so it cannot be resumed outside these limits. **Nothing is
generated, executed or deployed.**

Limits are constants here, not `.env` values, so what was approved is what runs:
- at most 3 requests to OpenAI per run (MAX_REQUESTS), and no retries: the SDK's are off, and nothing resumes by itself;
- $0.25 per run (RUN_CAP_USD), checked before every request against its worst case (app/orchestrator/budget.py), and
  $1.50 across runs (EXPERIMENT_CAP_USD), in a ledger next to the database. A run counts at its cap until it is settled;
- 90,000 tokens per run (TOKEN_LIMIT) and 6,000 output tokens per request, checked before every request against the
  worst case. This holds even if the configured prices are wrong.

Spending is computed from OPENAI_PRICE_INPUT_PER_MTOK and OPENAI_PRICE_OUTPUT_PER_MTOK. OpenAI's invoice is the
authority: if those prices are lower than the model's real ones, the real cost is higher by the same ratio.

`.env` supplies the key, the model, the prices and the database, in both modes. The key is never printed or reported.
"""

from __future__ import annotations

import argparse
import json
import secrets
import sys
import tempfile
import time
import warnings
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from pydantic import ValidationError
from sqlalchemy import select

from app.coding.launcher import OpenCodeLauncher
from app.config import REPO_ROOT, Settings
from app.core.egress import EgressPolicy
from app.db.models import PlanTask, Run
from app.demo.live_smoke import describe_request
from app.main import create_app
from app.orchestrator.budget import turn_cost_usd, turn_input_tokens
from app.orchestrator.llm_client import LLMClient, LLMError, LLMErrorKind, LLMRequest, LLMResponse, OpenAIResponsesClient, Usage, llm_config_problem
from app.sandbox.docker_cli import DockerCli
from app.selection.adaptive import AdaptiveSelector, ScoringConfig, available_memory_mb
from app.selection.catalog import CODING_CAPABILITIES, OPENCODE_NAME, PLANNED_TOOLS, eligibility_policy, opencode_profile
from app.selection.model import SelectionContext
from app.selection.selector import NoProvider, ProviderOption
from app.verification.recipes import readiness

API = "/api/v1"
MAX_REQUESTS = 3
RUN_CAP_USD = 0.25
EXPERIMENT_CAP_USD = 1.50
MAX_OUTPUT_TOKENS = 6_000
TOKEN_LIMIT = 90_000
WALL_CLOCK_SECONDS = 600.0
MAX_REQUIREMENT_CHARS = 20_000  # what the projects API accepts
PLANNING_TOOLS = frozenset({"submit_analysis", "submit_plan", "request_human_input", "compare_tools", "submit_gap_analysis"})  # these only read or record
PLAN_ACCEPTED, NO_PLAN = "PLAN ACCEPTED", "NO ACCEPTED PLAN"
PLAN_DONE = "planning mode stops once the plan is accepted: no acceptance criteria, code or build"
_FORCED: dict[str, Any] = {
    "max_turns": MAX_REQUESTS, "max_tool_calls": 2 * MAX_REQUESTS, "max_nudges": 1, "max_repeated_tool_calls": 1, "run_max_tokens": TOKEN_LIMIT,
    "run_wall_clock_seconds": WALL_CLOCK_SECONDS, "openai_max_output_tokens": MAX_OUTPUT_TOKENS, "openai_max_retries": 0, "openai_timeout_seconds": 120.0, "log_level": "WARNING",
}  # fmt: skip
_MIN_RUN_USD = 0.01
_POLL_SECONDS = 0.2


class PlanningGuard:
    """Between the orchestrator and OpenAI. Every check runs before a request leaves; a refusal raises
    LLMError(STOPPED), so the run pauses as `llm_stopped` and nothing is billed for it."""

    def __init__(self, inner: LLMClient, *, max_output_tokens: int, forbidden_values: list[str]) -> None:
        self._inner = inner
        self._max_output = max_output_tokens
        self._forbidden = [v for v in forbidden_values if v]
        self._context = 0  # what OpenAI stores and bills again with previous_response_id
        self.sent = 0
        self.tokens = 0  # as measured, or the worst case for a request whose answer never came
        self.plan_complete = False
        self.refusal: str | None = None

    def create(self, request: LLMRequest) -> LLMResponse:
        offered = {t["name"] for t in request.tools}
        extra = sorted(offered - PLANNING_TOOLS - {"submit_criteria"})
        if extra:
            return self._refuse(f"planning mode offers GPT only the analysis and planning tools, and this request offered {', '.join(extra)}")
        if "submit_criteria" in offered:
            self.plan_complete = True
            return self._refuse(PLAN_DONE)
        if self.sent >= MAX_REQUESTS:
            return self._refuse(f"planning mode allows at most {MAX_REQUESTS} requests to OpenAI per run")
        worst = turn_input_tokens(request, Run(context_tokens=self._context)) + self._max_output
        if self.tokens + worst > TOKEN_LIMIT:
            return self._refuse(f"this request could use {worst} tokens and {self.tokens} are used, past the {TOKEN_LIMIT} planning mode allows")
        if any(value in json.dumps([request.instructions, request.input, request.tools]) for value in self._forbidden):
            return self._refuse("a configured secret appeared in the request, so it was not sent")
        self.sent += 1  # counted before sending: a request that fails on the way may still be billed
        try:
            response = self._inner.create(request)
        except Exception:
            self.tokens += worst
            raise
        self.tokens += response.usage.input_tokens + response.usage.output_tokens
        self._context = response.usage.input_tokens + response.usage.output_tokens
        return response

    def _refuse(self, reason: str) -> LLMResponse:
        self.refusal = reason
        raise LLMError(LLMErrorKind.STOPPED, reason)


class CaptureClient:
    """The dry run's client: keeps the request it is given and sends nothing."""

    def __init__(self) -> None:
        self.requests: list[LLMRequest] = []

    def create(self, request: LLMRequest) -> LLMResponse:
        self.requests.append(request)
        raise LLMError(LLMErrorKind.STOPPED, "dry run: the request was not sent")


class Ledger:
    """Spending across manual runs, appended to and never rewritten. A run counts at its cap until it is settled, so
    a run whose process was lost (a crash, a closed terminal) still counts."""

    def __init__(self, path: Path) -> None:
        self.path = path

    def spent(self) -> float:
        """Raises on a damaged ledger: nothing is sent without knowing what was spent."""
        if not self.path.is_file():
            return 0.0
        caps: dict[str, float] = {}
        settled: dict[str, float] = {}
        for line in self.path.read_text(encoding="utf-8").splitlines():
            if line.strip():
                entry = json.loads(line)
                (settled if entry["event"] == "settled" else caps)[str(entry["run_id"])] = float(entry["usd"])
        return sum(settled.get(run_id, cap) for run_id, cap in caps.items())

    def reserve(self, run_id: str, cap_usd: float) -> None:
        self._append({"event": "reserved", "run_id": run_id, "usd": cap_usd})

    def settle(self, run_id: str, spent_usd: float, requests: int | None = None) -> None:
        self._append({"event": "settled", "run_id": run_id, "usd": round(spent_usd, 6), "requests": requests})

    def _append(self, entry: dict[str, Any]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.path.open("a", encoding="utf-8") as ledger:
            ledger.write(json.dumps({**entry, "at": datetime.now(UTC).isoformat(timespec="seconds")}) + "\n")


class _NoProcesses:
    """The Docker CLI's process runner here: planning starts no process, and asking whether a tool is eligible needs none."""

    def run(self, argv: Any, **_kwargs: Any) -> Any:
        raise RuntimeError("planning mode starts no process")


def planning_settings(env_file: Path | None = REPO_ROOT / ".env", **overrides: Any) -> Settings:
    """The key, model, prices and database from `.env`; every limit forced here, whatever `.env` says. The API token is
    a throwaway: the app runs in this process and listens on no port."""
    values = {"devpilot_api_token": secrets.token_urlsafe(32), "run_budget_usd": RUN_CAP_USD, **_FORCED, **overrides}
    return Settings(_env_file=env_file if env_file is not None and env_file.is_file() else None, **values)  # type: ignore[call-arg]


def planning_dir(settings: Settings) -> Path:
    """The ledger and the reports, next to the database."""
    return (settings.database_path.parent if settings.database_path is not None else settings.workspaces_dir) / "planning"


def preflight(settings: Settings, egress: EgressPolicy) -> list[tuple[str, bool, str]]:
    """What a live run needs, read from configuration. The key's presence is checked, never its value."""
    problem = llm_config_problem(settings, egress)
    prices = f"${settings.openai_price_input_per_mtok} in / ${settings.openai_price_output_per_mtok} out per 1M tokens" if settings.cost_known else "not set"
    return [
        ("OPENAI_API_KEY is configured", settings.openai_api_key is not None, ""),
        ("OPENAI_MODEL is set", bool(settings.openai_model), settings.openai_model or ""),
        ("prices are set, so spending can be capped before each request", settings.cost_known, prices),
        ("requests go to OpenAI's own endpoint", not settings.openai_base_url, settings.openai_base_url or ""),
        ("the GPT client is configured and the egress allowlist permits it", problem is None, problem or ""),
    ]


def tool_eligibility(settings: Settings, egress: EgressPolicy, plan: dict[str, Any], *, memory_probe: Callable[[], int | None] = available_memory_mb) -> list[dict[str, Any]]:
    """What build mode would decide for each task with this configuration, asked without starting anything: the
    stack's verification runtime, which generate_code checks first, then the adaptive selector's checks."""
    launcher = OpenCodeLauncher(settings, DockerCli(settings.docker_cli, _NoProcesses()), egress)  # type: ignore[arg-type]
    selector = AdaptiveSelector([ProviderOption(OPENCODE_NAME, launcher, CODING_CAPABILITIES, profile=opencode_profile(settings))], policy=eligibility_policy(settings, egress),
                                config=ScoringConfig.from_settings(settings), planned=PLANNED_TOOLS, memory_probe=memory_probe)  # fmt: skip
    stacks = {c["name"]: c["stack"] for c in plan.get("components") or []}
    rows = []
    for task in plan["tasks"]:
        stack = stacks.get(task.get("component") or "") or plan["stack"]
        ready, why = readiness(stack)
        try:
            chosen = selector.choose(task["capability"], SelectionContext(stack=stack))
            assert chosen.report is not None
            selection = {"would_choose": chosen.provider, "rejected": {r.tool_id: "; ".join(r.reasons) for r in chosen.report.rejected}}
        except NoProvider as exc:
            selection = {"would_choose": None, "rejected": exc.excluded}
        rows.append({"task": task["order"], "title": task["title"], "capability": task["capability"], "stack": stack, "verification_ready": ready, "verification": why, **selection})
    return rows


def blockers(eligibility: list[dict[str, Any]], analysis: dict[str, Any] | None) -> list[str]:
    found: list[str] = []
    for row in eligibility:
        if not row["verification_ready"]:
            found.append(f"{row['stack']}: {row['verification']}")
        if row["would_choose"] is None and OPENCODE_NAME in row["rejected"]:
            found.append(f"no coding tool is eligible for {row['capability']} on {row['stack']}: {OPENCODE_NAME}: {row['rejected'][OPENCODE_NAME]}")
    found.append("build mode is not part of this command: it needs Docker running with the approved sandbox images, OPENCODE_ENABLED, CODING_LIVE_APPROVED and an approved "
                 "OpenCode runtime image, and none of these is used here")  # fmt: skip
    found += [f"open question from the analysis: {q}" for q in (analysis or {}).get("ambiguities", [])]
    return list(dict.fromkeys(found))


def run_planning(settings: Settings, inner: LLMClient, requirement: str, *, label: str, ask: Callable[[str], str] = input, emit: Callable[[str], None] = print,
                 ledger: Ledger | None = None, memory_probe: Callable[[], int | None] = available_memory_mb) -> dict[str, Any]:  # fmt: skip
    """One planning run through the existing app. The run is closed at the end, whatever happened."""
    with warnings.catch_warnings():
        warnings.filterwarnings("ignore", message=".*httpx.*")
        from fastapi.testclient import TestClient

    guard = PlanningGuard(inner, max_output_tokens=settings.openai_max_output_tokens or 0, forbidden_values=settings.secret_values())
    app = create_app(settings, llm=guard, gpt_label=label)
    headers = {"Authorization": f"Bearer {settings.devpilot_api_token.get_secret_value()}"}
    with TestClient(app) as client:
        project = _ok(client.post(f"{API}/projects", json={"name": _project_name(requirement), "requirement_text": requirement}, headers=headers))
        run_id = _ok(client.post(f"{API}/projects/{project['id']}/runs", headers=headers))["id"]
        if ledger is not None:
            ledger.reserve(run_id, settings.run_budget_usd)
        person_stopped = False
        try:
            # The limits are this command's constants, accepted when it was launched; the API still requires the approval to name them.
            _ok(client.post(f"{API}/runs/{run_id}/start", json={"approved_max_usd": settings.run_budget_usd}, headers=headers))
            person_stopped = _wait(client, headers, run_id, settings, ask, emit)
        except KeyboardInterrupt:
            person_stopped = True
            emit("\nInterrupted: closing the run. A request already on its way is waited for, and counted.")
        finally:
            before = client.get(f"{API}/runs/{run_id}", headers=headers).json()
            if before["ended_at"] is None:
                client.post(f"{API}/runs/{run_id}/cancel", headers=headers)
        report = _collect(app, client, headers, run_id, before, settings, guard, requirement, label, memory_probe)
        report["run"]["stopped_by_person"] = person_stopped
        if ledger is not None:
            ledger.settle(run_id, report["usage"]["spent_usd"], guard.sent)
    return report


def _ok(response: Any) -> dict[str, Any]:
    if response.status_code >= 400:
        raise RuntimeError(f"the DevPilot API answered {response.status_code}: {response.text[:300]}")
    body: dict[str, Any] = response.json()
    return body


def _project_name(requirement: str) -> str:
    words = " ".join(requirement.split())
    return "Plan: " + (words if len(words) <= 60 else words[:59] + "…")


def _wait(client: Any, headers: dict[str, str], run_id: str, settings: Settings, ask: Callable[[str], str], emit: Callable[[str], None]) -> bool:
    """Until the run pauses, ends or runs out of time. True when the person stopped it at a question."""
    deadline = time.monotonic() + settings.run_wall_clock_seconds + 60
    while True:
        run = client.get(f"{API}/runs/{run_id}", headers=headers).json()
        if run["ended_at"] or run["pause_reason"]:
            return False
        if run["state"] == "AWAITING_APPROVAL":
            for approval in client.get(f"{API}/approvals", params={"run_id": run_id}, headers=headers).json():
                if not _decide(client, headers, approval, ask, emit):
                    return True
            deadline = time.monotonic() + settings.run_wall_clock_seconds + 60  # time spent answering is not the run's
        elif time.monotonic() > deadline:
            emit("The run did not finish in time, so it is being closed.")
            return False
        time.sleep(_POLL_SECONDS)


def _decide(client: Any, headers: dict[str, str], approval: dict[str, Any], ask: Callable[[str], str], emit: Callable[[str], None]) -> bool:
    """The person decides in the terminal; nothing is approved for them. False: the person stopped the run."""
    payload = approval["payload"] if isinstance(approval["payload"], dict) else {}
    if approval["kind"] == "HUMAN_INPUT":
        emit(f"\nGPT asks: {payload.get('question', '')}\n  Why: {payload.get('reason', '')}")
        answer = ask("Your answer (Enter stops the run): ").strip()
        if not answer:
            return False
        body: dict[str, Any] = {"decision": "approve", "response": answer}
    else:
        emit(f"\nApproval needed for {approval['tool']} ({approval['risk']}):\n  {json.dumps(approval.get('details') or payload, ensure_ascii=False)[:1500]}")
        body = {"decision": "approve" if ask("Type yes to approve; anything else denies: ").strip().lower() == "yes" else "deny"}
    _ok(client.post(f"{API}/approvals/{approval['id']}/decision", json=body, headers=headers))
    return True


def _collect(app: Any, client: Any, headers: dict[str, str], run_id: str, before: dict[str, Any], settings: Settings, guard: PlanningGuard, requirement: str, label: str,
             memory_probe: Callable[[], int | None]) -> dict[str, Any]:  # fmt: skip
    def get(path: str) -> Any:
        """The body, or None when the run has none yet (404)."""
        response = client.get(f"{API}/runs/{run_id}{path}", headers=headers)
        return response.json() if response.status_code == 200 else None

    after, analysis, plan, invocations, events = get(""), get("/analysis"), get("/plan"), get("/invocations"), get("/events?limit=500")
    with app.state.session_factory() as session:
        row = session.get(Run, run_id)
        reserved = row.reserved_usd  # a request still on its way counts at its ceiling
        components = json.loads(row.plan_json or "{}").get("components") or []
        task_components = {t.order: t.component for t in session.scalars(select(PlanTask).where(PlanTask.run_id == run_id))}
    if plan is not None:
        plan["components"] = components
        for task in plan["tasks"]:
            task["component"] = task_components.get(task["order"])
    eligibility = tool_eligibility(settings, app.state.egress, plan, memory_probe=memory_probe) if plan is not None else []
    report = {
        "requirement": requirement, "gpt": label, "model": settings.openai_model, "verdict": PLAN_ACCEPTED if plan is not None else NO_PLAN,
        "run": {"id": run_id, "stopped_in": before["state"], "pause_reason": before["pause_reason"], "detail": before["status_detail"], "outcome": before["outcome"], "closed_as": after["state"]},
        "responses": [e["payload"] for e in events if e["type"] == "llm.response"],
        "tool_calls": [{"tool": i["tool"], "state": i["state_at_call"], "ok": i["ok"], "arguments": i["arguments"], "result": i["output"]} for i in invocations],  # GPT's own words
        "stops": [{"type": e["type"], **e["payload"]} for e in events if e["type"] in ("llm.stopped", "llm.error")],
        "analysis": analysis, "plan": plan, "tool_eligibility": eligibility, "blockers": blockers(eligibility, analysis),
        "usage": {"requests_sent": guard.sent, "request_limit": MAX_REQUESTS, "tokens_in": after["tokens_in"], "tokens_out": after["tokens_out"], "token_limit": TOKEN_LIMIT,
                  "cost_known": after["cost_known"], "spent_usd": round(after["cost_usd"] + reserved, 6), "run_cap_usd": settings.run_budget_usd,
                  "price_input_per_mtok": settings.openai_price_input_per_mtok, "price_output_per_mtok": settings.openai_price_output_per_mtok},
        "generated": False, "executed": False, "deployed": False,
    }  # fmt: skip
    redacted: dict[str, Any] = app.state.redactor.redact(report)
    return redacted


# --- Terminal -------------------------------------------------------------------------------------------------------


def render(report: dict[str, Any], emit: Callable[[str], None], *, ledger_line: str = "", path: Path | None = None) -> None:
    usage, plan, analysis = report["usage"], report["plan"], report["analysis"]
    emit("\n== 1. Your requirement ==")
    emit(report["requirement"])
    emit(f"\n== 2. GPT's responses ({report['gpt']}) ==")
    calls = list(report["tool_calls"])
    for n, response in enumerate(report["responses"], start=1):
        cost = f", ${response['cost_usd']:.4f}" if response.get("cost_usd") is not None else ""
        emit(f"Request {n}: {', '.join(response['function_calls']) or 'no tool call'} ({response['input_tokens']:,} in / {response['output_tokens']:,} out tokens{cost})")
        if response.get("text"):
            emit(f"  GPT wrote: {response['text']}")
        for name in response["function_calls"]:
            if calls and calls[0]["tool"] == name:
                _emit_call(calls.pop(0), emit)
    for call in calls:
        _emit_call(call, emit)
    for stop in report["stops"]:
        emit(("Stopped on purpose: " if stop["type"] == "llm.stopped" else f"OpenAI error ({stop.get('kind')}): ") + str(stop.get("message", "")))
    if not report["responses"]:
        emit("No response was received.")
    emit("\n== 3. Development plan ==")
    if analysis is not None:
        emit(f"Analysis: {analysis['summary']}")
        for r in analysis["requirements"]:
            emit(f"  {r['code']} [{r['kind']}] {r['text']}")
        for label, items in (("Assumptions", analysis["assumptions"]), ("Open questions", analysis["ambiguities"])):
            if items:
                emit(f"  {label}:")
                emit("\n".join(f"    - {item}" for item in items))
    if plan is None:
        emit("No plan was accepted.")
    else:
        emit(f"Stack: {plan['stack']}. {plan['stack_rationale']}")
        for c in plan["components"]:
            emit(f"  Component {c['name']}: {c['stack']} in {c['path']}/")
        emit(f"Summary: {plan['summary']}")
        emit("\n== 4. Planned tasks ==")
        for t in plan["tasks"]:
            emit(f"{t['order']:>2}. {t['title']} [{t['capability']}{', ' + t['component'] if t.get('component') else ''}] covers {', '.join(t['requirement_refs'])}")
            emit(f"    {t['description']}")
    emit("\n== 5. Tool eligibility and selection (what build mode would decide; nothing was started) ==")
    for row in report["tool_eligibility"]:
        emit(f"Task {row['task']} ({row['capability']}, {row['stack']}): verification {'ready' if row['verification_ready'] else 'BLOCKED'}: {row['verification']}")
        emit(f"  would choose: {row['would_choose'] or 'no tool: none is eligible'}")
        for tool, why in row["rejected"].items():
            emit(f"  {tool}: not eligible: {why}")
    if not report["tool_eligibility"]:
        emit("Not assessed: there is no accepted plan.")
    emit("\n== 6. Security and runtime blockers ==")
    emit("\n".join(f"- {b}" for b in report["blockers"]))
    emit("\n== 7. Tokens and estimated cost ==")
    emit(f"Requests sent to OpenAI: {usage['requests_sent']} of {usage['request_limit']}. Tokens: {usage['tokens_in']:,} in, {usage['tokens_out']:,} out (limit {usage['token_limit']:,}).")
    if usage["cost_known"]:
        emit(f"Estimated cost: ${usage['spent_usd']:.4f} of this run's ${usage['run_cap_usd']:.2f} cap, at the configured ${usage['price_input_per_mtok']} in / ${usage['price_output_per_mtok']} out per 1M tokens.")
    if ledger_line:
        emit(ledger_line)
    emit("These are estimates from the configured prices. OpenAI's usage page and invoice are the authority.")
    emit("\n== 8. Final planning report ==")
    run = report["run"]
    reason = run["detail"] or run["outcome"] or run["pause_reason"] or ""
    emit(f"{report['verdict']}. The run stopped in {run['stopped_in']}{': ' + reason if reason else ''}.")
    emit(f"It was then closed ({run['closed_as']}), so it cannot continue outside these limits.")
    emit("Nothing was generated, executed or deployed. Build mode was not started.")
    if path is not None:
        emit(f"Full report: {path}")


def _emit_call(call: dict[str, Any], emit: Callable[[str], None]) -> None:
    error = (call["result"] if isinstance(call["result"], dict) else {}).get("error") or {}
    emit(f"  -> {call['tool']}: " + ("accepted by the gate" if call["ok"] else f"rejected: {error.get('summary', 'no result')}"))
    for detail in error.get("details") or []:
        emit(f"     - {detail}")


def dry_run(settings: Settings, requirement: str, emit: Callable[[str], None]) -> int:
    """The first request exactly as it would be sent, from a throwaway database. Nothing is sent."""
    capture = CaptureClient()
    folder = planning_dir(settings)
    folder.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="dry-run-", dir=folder) as scratch:
        throwaway = settings.model_copy(update={"database_url": f"sqlite:///{(Path(scratch) / 'dry.db').as_posix()}", "workspaces_dir": Path(scratch) / "workspaces"})
        run_planning(throwaway, capture, requirement, label="dry run: nothing sent", ask=lambda _q: "", emit=emit, memory_probe=lambda: None)
    (request,) = capture.requests
    emit("\nDRY RUN: request 1 would be:")
    emit("\n".join(describe_request(request, settings)))
    worst_in = turn_input_tokens(request, Run(context_tokens=0))
    cost = turn_cost_usd(Usage(input_tokens=worst_in, output_tokens=MAX_OUTPUT_TOKENS), settings)
    emit(f"  worst case:          {worst_in:,} input + {MAX_OUTPUT_TOKENS:,} output tokens" + (f" = ${cost:.4f} at the configured prices" if cost is not None else " (prices not set)"))
    problems = [f"{label}{' (' + detail + ')' if detail else ''}" for label, ok, detail in preflight(settings, EgressPolicy.from_file(settings.egress_allowlist_file)) if not ok]
    emit(f"\nReady for --live: {'no: ' + '; '.join(problems) if problems else 'yes'}. Nothing was sent.")
    return 0


def main(argv: list[str] | None = None, *, ask: Callable[[str], str] = input, emit: Callable[[str], None] = print, env_file: Path | None = REPO_ROOT / ".env") -> int:
    parser = argparse.ArgumentParser(prog="python -m app.plan_cli", description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--live", action="store_true", help="really call OpenAI (costs money); without it nothing is sent")
    parser.add_argument("--prompt", help="the requirement; without it you are asked for it")
    args = parser.parse_args(argv)
    try:
        settings = planning_settings(env_file)
    except ValidationError as exc:
        emit(".env has invalid values (the values are not shown):")
        emit("\n".join(f"  {'.'.join(str(p) for p in err['loc']).upper()}: {err['msg']}" for err in exc.errors(include_input=False)))
        return 1
    emit(f"DevPilot planning mode ({'LIVE: real OpenAI requests' if args.live else 'dry run: nothing is sent'}). Model: {settings.openai_model or 'not set'}.")
    emit(f"Limits: {MAX_REQUESTS} requests, ${RUN_CAP_USD:.2f} and {TOKEN_LIMIT:,} tokens per run, {MAX_OUTPUT_TOKENS:,} output tokens per request, no retries, "
         f"${EXPERIMENT_CAP_USD:.2f} across runs. Stops once the plan is accepted.")  # fmt: skip
    requirement = (args.prompt if args.prompt is not None else ask("\nDescribe the application to plan (Enter or 'exit' quits):\n> ")).strip()
    if requirement.lower() in ("", "exit", "quit"):
        emit("Nothing was sent.")
        return 0
    if len(requirement) > MAX_REQUIREMENT_CHARS:
        emit(f"Refused: the requirement is longer than {MAX_REQUIREMENT_CHARS} characters. Nothing was sent.")
        return 1
    if not args.live:
        return dry_run(settings, requirement, emit)

    failed = [f"{label}{' (' + detail + ')' if detail else ''}" for label, ok, detail in preflight(settings, EgressPolicy.from_file(settings.egress_allowlist_file)) if not ok]
    if failed:
        emit("Refused before sending anything:\n" + "\n".join(f"  - {f}" for f in failed))
        return 1
    ledger = Ledger(planning_dir(settings) / "ledger.jsonl")
    try:
        spent = ledger.spent()
    except (OSError, ValueError, KeyError, TypeError):
        emit(f"Refused: the spending ledger {ledger.path} cannot be read, so the remaining budget is unknown. Nothing was sent.")
        return 1
    cap = round(min(RUN_CAP_USD, EXPERIMENT_CAP_USD - spent), 6)
    if cap < _MIN_RUN_USD:
        emit(f"Refused: ${spent:.4f} of the ${EXPERIMENT_CAP_USD:.2f} experiment budget is used (ledger: {ledger.path}). Nothing was sent.")
        return 1
    answer = ask(f"\nSend this requirement to OpenAI ({settings.openai_model})? At most {MAX_REQUESTS} requests and ${cap:.2f}; ${spent:.4f} of ${EXPERIMENT_CAP_USD:.2f} is spent. Type yes: ")
    if answer.strip().lower() != "yes":
        emit("Nothing was sent.")
        return 0
    try:
        llm = OpenAIResponsesClient(settings)
    except LLMError as exc:
        emit(f"Refused: {exc.message}. Nothing was sent.")
        return 1
    report = run_planning(settings.model_copy(update={"run_budget_usd": cap}), llm, requirement, label=f"OpenAI Responses API, {settings.openai_model}", ask=ask, emit=emit, ledger=ledger)
    path = planning_dir(settings) / f"{report['run']['id']}.json"
    path.write_text(json.dumps(report, indent=1, ensure_ascii=False, default=str), encoding="utf-8")
    total = ledger.spent()
    render(report, emit, ledger_line=f"Experiment: ${total:.4f} of ${EXPERIMENT_CAP_USD:.2f} spent, ${max(EXPERIMENT_CAP_USD - total, 0):.4f} left (ledger: {ledger.path}).", path=path)
    return 0 if report["verdict"] == PLAN_ACCEPTED else 2


if __name__ == "__main__":
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(errors="replace")  # a console that cannot show a character GPT wrote must not crash the run
    sys.exit(main())
