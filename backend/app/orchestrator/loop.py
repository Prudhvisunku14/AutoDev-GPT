"""The GPT function-call loop (ARCHITECTURE §5.1).

`advance(run_id)` drives one run until it pauses (approval, human input, a state that a
later phase implements, a resumable LLM error) or ends (completed, failed, cancelled,
stopped by a limit). It is safe to call again at any time: all progress is persisted.

Crash safety and exactly-once execution:
- Every function call from GPT is stored as a `tool_invocations` row (unique per
  `call_id`) before anything runs. A replayed call_id is ignored.
- A call is claimed (status STARTED) in its own transaction before its handler runs.
  The handler and the result are committed together. A crash in between leaves the
  call STARTED; the next `advance` marks it INTERRUPTED and reports that to GPT
  instead of running it a second time.
- Outputs are marked delivered only after GPT accepted the request that carried them,
  so an interrupted LLM call is simply repeated with the same input.
- No database transaction is open while GPT is being called.
"""

from __future__ import annotations

import hashlib
import json
import logging
import threading
import time
from collections.abc import Callable
from typing import Any

from sqlalchemy import func, select, update
from sqlalchemy.orm import Session, sessionmaker

from app.config import Settings
from app.core.events import EventRecorder
from app.core.redaction import Redactor
from app.db.models import (
    FINAL_INVOCATION_STATUSES,
    Approval,
    ApprovalKind,
    InvocationStatus,
    PlanTask,
    Project,
    Requirement,
    Run,
    RunState,
    ToolInvocation,
    utcnow,
)
from app.orchestrator import prompts
from app.orchestrator.budget import LimitHit, RunGuard, turn_ceiling_usd, turn_cost_usd, turn_input_tokens
from app.orchestrator.llm_client import LLMClient, LLMError, LLMErrorKind, LLMRequest, LLMResponse
from app.orchestrator.run_control import change_state, end_run, pause_run
from app.orchestrator.state_machine import ORCHESTRATED_STATES, REQUIRED_PHASE
from app.coding.types import CodingProvider
from app.sandbox.runner import SandboxRunner
from app.sources.check import for_gpt
from app.sources.events import latest_check
from app.stacks.catalog import StackCatalog
from app.tools.adapters.workspace_fs import WorkspaceFS
from app.tools.base import ErrorClass, RecoveryInfo, ToolContext, ToolResult
from app.tools.executor import PreparedCall, ToolExecutor
from app.tools.policy import Decision
from app.tools.registry import ToolRegistry

logger = logging.getLogger("devpilot.orchestrator")

LLMProvider = Callable[[], LLMClient]
_EVENT_TEXT_LIMIT = 500
# Refused before any work began, so these cannot have been billed. Anything else may have been.
_NOT_BILLED = frozenset({LLMErrorKind.AUTH, LLMErrorKind.QUOTA, LLMErrorKind.RATE_LIMIT, LLMErrorKind.MODEL_NOT_FOUND, LLMErrorKind.BAD_REQUEST, LLMErrorKind.CONFIG, LLMErrorKind.STOPPED})


class Orchestrator:
    def __init__(
        self,
        *,
        settings: Settings,
        session_factory: sessionmaker[Session],
        registry: ToolRegistry,
        executor: ToolExecutor,
        events: EventRecorder,
        catalog: StackCatalog,
        redactor: Redactor,
        llm_provider: LLMProvider,
        sandbox: SandboxRunner | None = None,
        workspace: WorkspaceFS | None = None,
        coding: CodingProvider | None = None,
        candidates: WorkspaceFS | None = None,
        orchestrated_states: frozenset[RunState] = ORCHESTRATED_STATES,
    ) -> None:
        self._settings = settings
        self._sessions = session_factory
        self._registry = registry
        self._executor = executor
        self._events = events
        self._catalog = catalog
        self._redactor = redactor
        self._llm_provider = llm_provider
        self._sandbox = sandbox
        self._workspace = workspace
        self._coding = coding
        self._candidates = candidates
        self._orchestrated = orchestrated_states
        self._guard = RunGuard(settings)
        self._stop = threading.Event()

    def request_stop(self) -> None:
        """Ask a running `advance` to pause at its next checkpoint (backend shutdown)."""
        self._stop.set()

    # --- Entry point -------------------------------------------------------------

    def advance(self, run_id: str) -> None:
        session_start = time.monotonic()
        try:
            if not self._prepare(run_id):
                return
            while True:
                if self._stop.is_set():
                    self._pause(run_id, "backend_shutdown", "The backend stopped; start the run again to continue.")
                    return
                if not self._process_queue(run_id, session_start):
                    return
                # A tool can run for minutes; a shutdown requested meanwhile must not be followed by a GPT call.
                if self._stop.is_set():
                    self._pause(run_id, "backend_shutdown", "The backend stopped; start the run again to continue.")
                    return
                prepared = self._next_request(run_id, session_start)
                if prepared is None:
                    return
                request, delivered_ids = prepared
                try:
                    response = self._llm_provider().create(request)
                except LLMError as exc:
                    self._on_llm_error(run_id, exc)
                    return
                if not self._record_response(run_id, response, delivered_ids):
                    return
        except Exception:
            # A bug must not kill the worker or leave the run looking active.
            logger.exception("orchestrator failed on run %s", run_id)
            self._pause(run_id, "internal_error", "An internal error stopped the run; see the backend log.")
        finally:
            self._add_active_time(run_id, time.monotonic() - session_start)

    # --- Steps -------------------------------------------------------------------

    def _prepare(self, run_id: str) -> bool:
        with self._sessions.begin() as s:
            run = s.get(Run, run_id)
            if run is None or run.is_terminal or run.state is RunState.AWAITING_APPROVAL:
                return False
            if run.state is RunState.CREATED:
                run.started_at = utcnow()
                run.cost_known = self._settings.cost_known
                self._record(s, run, "run.started", {"cost_known": run.cost_known})
                change_state(s, run, RunState.ANALYZING, self._events, reason="run started")
            elif run.pause_reason:
                self._record(s, run, "run.resumed", {"state": run.state, "after": run.pause_reason})
            if not self._settings.cost_known:
                run.cost_known = False
            run.pause_reason = None
            run.status_detail = None
            self._charge_reservation(run)  # the backend stopped during a GPT call: it may have been billed
            self._recover_interrupted(s, run)
            return True

    def _recover_interrupted(self, s: Session, run: Run) -> None:
        stale = s.scalars(
            select(ToolInvocation).where(
                ToolInvocation.run_id == run.id, ToolInvocation.status == InvocationStatus.STARTED
            )
        ).all()
        for inv in stale:
            summary = "The backend stopped while this call was running. Its changes were rolled back; call the tool again if it is still needed."
            spec = self._registry.get(inv.tool)
            if spec is not None and spec.external is not None and spec.external.recover is not None:
                info = RecoveryInfo(settings=self._settings, run_id=run.id, project_id=run.project_id, call_id=inv.call_id)
                try:
                    note = spec.external.recover(info)
                except Exception:
                    logger.exception("recovery of %s (%s) failed", inv.tool, inv.call_id)
                    note = "Its side effects could not be undone automatically; check the workspace."
                if note:
                    summary = f"The backend stopped while this call was running. {note}"
                self._record(s, run, "tool.recovered", {"call_id": inv.call_id, "tool": inv.tool, "note": note})
            result = ToolResult.failure(inv.tool, ErrorClass.INTERRUPTED, summary)
            self._finish(s, run, inv, result, status=InvocationStatus.INTERRUPTED)

    def _process_queue(self, run_id: str, session_start: float) -> bool:
        """Run queued calls in order. False means the loop must stop (paused or ended)."""
        while True:
            with self._sessions.begin() as s:
                run = s.get(Run, run_id)
                assert run is not None
                if run.is_terminal or run.state is RunState.AWAITING_APPROVAL:
                    return False
                inv = s.scalars(
                    select(ToolInvocation)
                    .where(
                        ToolInvocation.run_id == run_id,
                        ToolInvocation.status.in_((InvocationStatus.QUEUED, InvocationStatus.APPROVED)),
                    )
                    .order_by(ToolInvocation.seq)
                    .limit(1)
                ).first()
                if inv is None:
                    return True
                hit = self._guard.before_tool(run, self._active(run, session_start))
                if hit is not None:
                    self._stop_run(s, run, hit)
                    return False
                approved = inv.status is InvocationStatus.APPROVED
                prepared = self._executor.prepare(run.state, inv.tool, inv.arguments_json, approved=approved)
                if isinstance(prepared, ToolResult):
                    self._finish(s, run, inv, prepared)
                    continue
                if prepared.decision in (Decision.REQUIRE_APPROVAL, Decision.ASK_HUMAN):
                    self._request_approval(s, run, inv, prepared)
                    return False
                # Claim the call. Only one row can move from QUEUED/APPROVED to STARTED.
                claimed = s.execute(
                    update(ToolInvocation)
                    .where(
                        ToolInvocation.id == inv.id,
                        ToolInvocation.status.in_((InvocationStatus.QUEUED, InvocationStatus.APPROVED)),
                    )
                    .values(status=InvocationStatus.STARTED)
                    .execution_options(synchronize_session=False)
                )
                if claimed.rowcount != 1:
                    continue
                inv_id = inv.id
                self._record(s, run, "tool.started", {"call_id": inv.call_id, "tool": inv.tool})

            with self._sessions.begin() as s:
                run = s.get(Run, run_id)
                inv = s.get(ToolInvocation, inv_id)
                assert run is not None and inv is not None
                if run.is_terminal or inv.status is not InvocationStatus.STARTED:
                    return False
                prepared = self._executor.prepare(run.state, inv.tool, inv.arguments_json, approved=True)
                if isinstance(prepared, ToolResult):
                    self._finish(s, run, inv, prepared)
                    continue
                ctx = self._context(s, run, inv.call_id)
                if prepared.spec.external is None:
                    before = run.state
                    result = self._executor.invoke(ctx, prepared)
                    if run.state is not before:
                        run.nudges = 0
                    self._finish(s, run, inv, result)
                    continue
                started = time.perf_counter()
                plan = self._executor.begin_external(ctx, prepared)
                if isinstance(plan, ToolResult):
                    self._finish(s, run, inv, plan)
                    continue

            # The slow step runs with no transaction open, so the API can cancel the run or
            # decide approvals meanwhile. A cancelled run or a backend shutdown stops it.
            with _CancelWatch(self._sessions, run_id, self._stop) as cancel:
                outcome, error = self._executor.run_external(
                    prepared, plan, cancel, lambda event_type, payload: self._progress(run_id, event_type, payload)
                )

            with self._sessions.begin() as s:
                run = s.get(Run, run_id)
                inv = s.get(ToolInvocation, inv_id)
                assert run is not None and inv is not None
                ctx = self._context(s, run, inv.call_id)
                ended_meanwhile = run.is_terminal
                before = run.state
                # The tool still records its own outcome (and undoes its side effects if needed).
                result = self._executor.finish_external(ctx, prepared, plan, outcome, error, started=started)
                if ended_meanwhile:
                    self._finish(s, run, inv, result, status=InvocationStatus.DISCARDED)
                    self._record(s, run, "tool.result_discarded", {"call_id": inv.call_id, "tool": inv.tool, "state": run.state})
                    return False
                if run.state is not before:
                    run.nudges = 0
                self._finish(s, run, inv, result)

    def _context(self, s: Session, run: Run, call_id: str) -> ToolContext:
        project = s.get(Project, run.project_id)
        assert project is not None
        return ToolContext(
            session=s,
            run=run,
            project=project,
            settings=self._settings,
            events=self._events,
            catalog=self._catalog,
            sandbox=self._sandbox,
            workspace=self._workspace,
            cancel=self._stop,  # backend shutdown stops any container still running
            coding=self._coding,
            candidates=self._candidates,
            call_id=call_id,
        )

    def _progress(self, run_id: str, event_type: str, payload: dict[str, Any]) -> None:
        """Progress from a running external tool, each in its own short transaction."""
        try:
            with self._sessions.begin() as s:
                run = s.get(Run, run_id)
                if run is not None:
                    self._record(s, run, event_type, payload)
        except Exception:
            logger.exception("could not record %s for run %s", event_type, run_id)

    def _next_request(self, run_id: str, session_start: float) -> tuple[LLMRequest, list[int]] | None:
        with self._sessions.begin() as s:
            run = s.get(Run, run_id)
            assert run is not None
            if run.is_terminal or run.state is RunState.AWAITING_APPROVAL:
                return None
            if run.pause_reason:  # a tool paused the run (wait_for_workflow): no further GPT call until it is resumed
                return None
            if run.state not in self._orchestrated:
                needed = REQUIRED_PHASE.get(run.state, "a later phase")
                pause_run(
                    s,
                    run,
                    self._events,
                    reason="phase_unavailable",
                    detail=f"State {run.state} needs {needed}, which is not implemented yet. "
                    "Nothing was generated or executed. Start the run again once that phase exists.",
                )
                return None
            hit = self._guard.before_llm(run, self._active(run, session_start))
            if hit is not None:
                self._stop_run(s, run, hit)
                return None

            outputs = s.scalars(
                select(ToolInvocation)
                .where(
                    ToolInvocation.run_id == run_id,
                    ToolInvocation.delivered.is_(False),
                    ToolInvocation.status.in_(FINAL_INVOCATION_STATUSES),
                )
                .order_by(ToolInvocation.seq)
            ).all()
            items: list[dict[str, Any]] = [
                {"type": "function_call_output", "call_id": inv.call_id, "output": inv.output_json or "{}"} for inv in outputs
            ]
            messages: list[str] = json.loads(run.pending_messages_json or "[]")
            if run.last_response_id is None:
                project = s.get(Project, run.project_id)
                assert project is not None
                checked = latest_check(s, project.id)
                resources = for_gpt(checked["resources"]) if checked and checked.get("resources") else None
                messages.insert(0, prompts.initial_message(project.name, project.requirement_text, resources))
            if not items and not messages:
                messages.append(prompts.continue_message(run.state))
            items.extend({"role": "user", "content": m} for m in messages)

            requirements = [
                (r.code, r.text)
                for r in s.scalars(select(Requirement).where(Requirement.run_id == run_id).order_by(Requirement.code))
            ]
            tasks = [(t.order, t.title, t.status) for t in s.scalars(select(PlanTask).where(PlanTask.run_id == run_id).order_by(PlanTask.order))]
            project_stack = s.get(Project, run.project_id).stack  # type: ignore[union-attr]
            request = LLMRequest(
                instructions=prompts.instructions_for(
                    run.state,
                    catalog=self._catalog,
                    requirements=requirements,
                    stack=self._catalog.get(project_stack or ""),
                    tasks=tasks,
                ),
                input=items,
                tools=self._registry.schemas_for(run.state),
                previous_response_id=run.last_response_id,
            )
            ceiling = turn_ceiling_usd(request, run, self._settings)
            hit = self._guard.before_send(run, ceiling, turn_input_tokens(request, run))
            if hit is not None:
                self._stop_run(s, run, hit)
                return None
            run.reserved_usd = ceiling  # saved before the send, so a crash cannot hide this call
            self._record(
                s,
                run,
                "llm.request",
                {
                    "turn": run.turns + 1,
                    "state": run.state,
                    "tools": [t["name"] for t in request.tools],
                    "function_outputs": len(outputs),
                    "messages": len(messages),
                },
            )
            return request, [inv.id for inv in outputs]

    def _record_response(self, run_id: str, response: LLMResponse, delivered_ids: list[int]) -> bool:
        with self._sessions.begin() as s:
            run = s.get(Run, run_id)
            assert run is not None
            cost = turn_cost_usd(response.usage, self._settings) if run.cost_known else None
            if cost is not None:
                run.cost_usd += cost
            run.reserved_usd = 0.0
            run.tokens_in += response.usage.input_tokens
            run.tokens_out += response.usage.output_tokens
            if run.is_terminal or run.state is RunState.AWAITING_APPROVAL:
                self._record(s, run, "llm.response_discarded", {"response_id": response.id, "state": run.state})
                return False

            run.turns += 1
            run.context_tokens = response.usage.input_tokens + response.usage.output_tokens
            run.last_response_id = response.id
            run.pending_messages_json = None
            if delivered_ids:
                s.execute(
                    update(ToolInvocation)
                    .where(ToolInvocation.id.in_(delivered_ids))
                    .values(delivered=True)
                    .execution_options(synchronize_session=False)
                )
            self._record(
                s,
                run,
                "llm.response",
                {
                    "response_id": response.id,
                    "status": response.status,
                    "function_calls": [c.name for c in response.function_calls],
                    "input_tokens": response.usage.input_tokens,
                    "output_tokens": response.usage.output_tokens,
                    "cost_usd": cost,
                    "text": response.text[:_EVENT_TEXT_LIMIT] or None,
                },
            )

            if not response.function_calls:
                run.nudges += 1
                if run.nudges > self._settings.max_nudges:
                    self._stop_run(
                        s, run, LimitHit("no_progress", f"GPT replied {run.nudges} times without calling a tool")
                    )
                    return False
                names = [spec.name for spec in self._registry.specs_for(run.state)]
                run.pending_messages_json = json.dumps([prompts.nudge_message(run.state, names)])
                return True

            next_seq = (s.scalar(select(func.max(ToolInvocation.seq)).where(ToolInvocation.run_id == run_id)) or 0) + 1
            for fc in response.function_calls:
                exists = s.scalar(
                    select(ToolInvocation.id).where(ToolInvocation.run_id == run_id, ToolInvocation.call_id == fc.call_id)
                )
                if exists is not None:
                    self._record(s, run, "tool.duplicate_ignored", {"call_id": fc.call_id, "tool": fc.name})
                    continue
                arguments = self._redacted_arguments(fc.arguments)
                fingerprint = hashlib.sha256(f"{fc.name}\n{arguments}".encode()).hexdigest()
                if self._repeats(s, run_id, fingerprint) >= self._settings.max_repeated_tool_calls:
                    self._stop_run(
                        s,
                        run,
                        LimitHit(
                            "repeated_tool_call",
                            f"{fc.name} was called with identical arguments "
                            f"{self._settings.max_repeated_tool_calls + 1} times in a row",
                        ),
                    )
                    return False
                s.add(
                    ToolInvocation(
                        run_id=run_id,
                        seq=next_seq,
                        call_id=fc.call_id,
                        response_id=response.id,
                        tool=fc.name,
                        arguments_json=arguments,
                        fingerprint=fingerprint,
                        state_at_call=run.state.value,
                        status=InvocationStatus.QUEUED,
                    )
                )
                s.flush()
                next_seq += 1
                self._record(s, run, "tool.called", {"call_id": fc.call_id, "tool": fc.name, "arguments": arguments})
            return True

    # --- Helpers -------------------------------------------------------------------

    def _request_approval(self, s: Session, run: Run, inv: ToolInvocation, prepared: PreparedCall) -> None:
        kind = ApprovalKind.HUMAN_INPUT if prepared.decision is Decision.ASK_HUMAN else ApprovalKind.TOOL
        inv.status = InvocationStatus.AWAITING_APPROVAL
        details = None
        if prepared.spec.approval_details is not None:  # what the person should see; it never fetches anything
            try:
                details = self._redactor.redact(prepared.spec.approval_details(s, run, run.project, prepared.args))
            except Exception:  # details help a person decide; they never block the approval itself
                logger.exception("approval details for %s could not be prepared", inv.tool)
                details = {"note": "details could not be prepared; the arguments are shown"}
        approval = Approval(
            run_id=run.id,
            invocation_id=inv.id,
            kind=kind,
            tool=inv.tool,
            risk=prepared.spec.risk.value,
            payload_json=inv.arguments_json,
            details_json=json.dumps(details, sort_keys=True, ensure_ascii=False) if details is not None else None,
        )
        s.add(approval)
        s.flush()
        change_state(s, run, RunState.AWAITING_APPROVAL, self._events, reason=prepared.reason)
        self._record(
            s,
            run,
            "approval.requested",
            {
                "approval_id": approval.id,
                "kind": kind,
                "tool": inv.tool,
                "risk": approval.risk,
                "arguments": json.loads(inv.arguments_json) if _is_json(inv.arguments_json) else inv.arguments_json,
                **({"details": details} if details is not None else {}),
            },
        )

    def _finish(
        self,
        s: Session,
        run: Run,
        inv: ToolInvocation,
        result: ToolResult,
        *,
        status: InvocationStatus | None = None,
    ) -> None:
        if result.state is None:
            result.state = run.state.value
        inv.status = status or (InvocationStatus.SUCCEEDED if result.ok else InvocationStatus.FAILED)
        inv.ok = result.ok
        inv.error_class = result.error.error_class if result.error else None
        inv.output_json = self._executor.render(result)
        inv.latency_ms = result.meta.get("latency_ms")
        inv.finished_at = utcnow()
        run.tool_calls += 1
        payload: dict[str, Any] = {"call_id": inv.call_id, "tool": inv.tool, "ok": result.ok, "state": run.state}
        if result.error:
            payload["error_class"] = result.error.error_class
            payload["summary"] = result.error.summary
        self._record(s, run, "tool.result", payload)

    def _stop_run(self, s: Session, run: Run, hit: LimitHit) -> None:
        end_run(s, run, RunState.STOPPED_BY_POLICY, self._events, outcome=hit.reason, detail=hit.detail, reason=hit.reason)

    def _pause(self, run_id: str, reason: str, detail: str) -> None:
        with self._sessions.begin() as s:
            run = s.get(Run, run_id)
            if run is not None and not run.is_terminal and run.state is not RunState.AWAITING_APPROVAL:
                pause_run(s, run, self._events, reason=reason, detail=detail)

    def _on_llm_error(self, run_id: str, exc: LLMError) -> None:
        message = self._redactor.redact_text(exc.message)
        with self._sessions.begin() as s:
            run = s.get(Run, run_id)
            assert run is not None
            if exc.kind in _NOT_BILLED:
                run.reserved_usd = 0.0
            else:
                self._charge_reservation(run)
            if exc.kind is LLMErrorKind.STOPPED:
                self._record(s, run, "llm.stopped", {"message": message})
                if not run.is_terminal and run.state is not RunState.AWAITING_APPROVAL:
                    pause_run(s, run, self._events, reason="llm_stopped", detail=message)
                return
            self._record(s, run, "llm.error", {"kind": exc.kind, "message": message, "resumable": exc.resumable, **exc.facts()})
            if run.is_terminal or run.state is RunState.AWAITING_APPROVAL:
                return
            if exc.resumable:
                pause_run(s, run, self._events, reason=f"llm_error:{exc.kind.lower()}", detail=message)
            else:
                end_run(s, run, RunState.FAILED, self._events, outcome="llm_error", detail=message, reason=exc.kind)

    def _charge_reservation(self, run: Run) -> None:
        """The answer never came, so the call counts at its ceiling. Spending cannot go unseen."""
        run.cost_usd += run.reserved_usd
        run.reserved_usd = 0.0

    def _add_active_time(self, run_id: str, seconds: float) -> None:
        try:
            with self._sessions.begin() as s:
                run = s.get(Run, run_id)
                if run is not None:
                    run.active_seconds += seconds
        except Exception:
            logger.exception("could not record active time for run %s", run_id)

    def _repeats(self, s: Session, run_id: str, fingerprint: str) -> int:
        recent = s.scalars(
            select(ToolInvocation.fingerprint)
            .where(ToolInvocation.run_id == run_id)
            .order_by(ToolInvocation.seq.desc())
            .limit(self._settings.max_repeated_tool_calls)
        ).all()
        count = 0
        for fp in recent:
            if fp != fingerprint:
                break
            count += 1
        return count

    def _redacted_arguments(self, raw: str) -> str:
        try:
            return json.dumps(self._redactor.redact(json.loads(raw)), sort_keys=True, ensure_ascii=False)
        except (json.JSONDecodeError, TypeError):
            return self._redactor.redact_text(raw)

    def _active(self, run: Run, session_start: float) -> float:
        return run.active_seconds + (time.monotonic() - session_start)

    def _record(self, s: Session, run: Run, type: str, payload: dict[str, Any]) -> None:
        self._events.record(s, project_id=run.project_id, run_id=run.id, type=type, payload=payload)


def _is_json(text: str) -> bool:
    try:
        json.loads(text)
    except (json.JSONDecodeError, TypeError):
        return False
    return True


class _CancelWatch:
    """A cancel signal for one external step: set on backend shutdown or when the run ends.

    The API cancels a run by changing its row. This watcher polls that row so a running
    container or coding session is stopped, instead of running on for a run nobody wants.
    """

    POLL_SECONDS = 1.0

    def __init__(self, sessions: sessionmaker[Session], run_id: str, shutdown: threading.Event) -> None:
        self._sessions = sessions
        self._run_id = run_id
        self._shutdown = shutdown
        self._cancel = threading.Event()
        self._done = threading.Event()
        self._thread = threading.Thread(target=self._watch, name=f"cancel-watch-{run_id[:8]}", daemon=True)

    def __enter__(self) -> threading.Event:
        self._thread.start()
        return self._cancel

    def __exit__(self, *exc: object) -> None:
        self._done.set()
        self._thread.join(timeout=5)

    def _watch(self) -> None:
        while not self._done.is_set():
            if self._shutdown.is_set() or self._run_ended():
                self._cancel.set()
                return
            self._done.wait(self.POLL_SECONDS)

    def _run_ended(self) -> bool:
        try:
            with self._sessions() as s:
                run = s.get(Run, self._run_id)
                return run is None or run.is_terminal
        except Exception:
            logger.exception("cancel watch could not read run %s", self._run_id)
            return False
