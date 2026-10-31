"""Run state changes with their audit events. Shared by the loop, the gates and the API."""

from __future__ import annotations

from typing import Any

from sqlalchemy import update
from sqlalchemy.orm import Session

from app.core.events import EventRecorder
from app.db import repositories
from app.db.models import (
    Approval,
    ApprovalStatus,
    InvocationStatus,
    Run,
    RunState,
    ToolInvocation,
    utcnow,
)
from app.orchestrator.state_machine import transition

_OPEN_INVOCATIONS = (InvocationStatus.QUEUED, InvocationStatus.AWAITING_APPROVAL, InvocationStatus.APPROVED)


def change_state(
    session: Session, run: Run, target: RunState, events: EventRecorder, *, reason: str | None = None
) -> None:
    previous = transition(run, target)
    payload: dict[str, Any] = {"from": previous, "to": target}
    if reason:
        payload["reason"] = reason
    events.record(session, project_id=run.project_id, run_id=run.id, type="run.state_changed", payload=payload)


def end_run(
    session: Session,
    run: Run,
    target: RunState,
    events: EventRecorder,
    *,
    outcome: str,
    detail: str | None = None,
    reason: str | None = None,
) -> None:
    """Move a run to FAILED, CANCELLED or STOPPED_BY_POLICY and close everything still open."""
    previous = run.state
    _close_open_work(session, run)
    transition(run, target)
    run.outcome = outcome
    run.status_detail = detail
    run.pause_reason = None
    run.ended_at = utcnow()
    payload: dict[str, Any] = {"previous_state": previous}
    if reason:
        payload["reason"] = reason
    if detail:
        payload["detail"] = detail
    event_type = {RunState.FAILED: "run.failed", RunState.CANCELLED: "run.cancelled"}.get(target, "run.stopped")
    events.record(session, project_id=run.project_id, run_id=run.id, type=event_type, payload=payload)


def complete_run(session: Session, run: Run, events: EventRecorder, *, detail: str) -> None:
    """The one way to COMPLETED: the delivery has happened."""
    change_state(session, run, RunState.COMPLETED, events, reason="delivered")
    run.outcome = "completed"
    run.status_detail = detail
    run.pause_reason = None
    run.ended_at = utcnow()
    events.record(session, project_id=run.project_id, run_id=run.id, type="run.completed", payload={"detail": detail})


def cancel_run(session: Session, run: Run, events: EventRecorder) -> None:
    previous = run.state
    _close_open_work(session, run)
    repositories.cancel_run(run)
    events.record(
        session, project_id=run.project_id, run_id=run.id, type="run.cancelled", payload={"previous_state": previous}
    )


def pause_run(session: Session, run: Run, events: EventRecorder, *, reason: str, detail: str) -> None:
    """A non-terminal run stops progressing until someone starts it again."""
    run.pause_reason = reason
    run.status_detail = detail
    events.record(
        session,
        project_id=run.project_id,
        run_id=run.id,
        type="run.paused",
        payload={"state": run.state, "reason": reason, "detail": detail},
    )


def _close_open_work(session: Session, run: Run) -> None:
    session.execute(
        update(ToolInvocation)
        .where(ToolInvocation.run_id == run.id, ToolInvocation.status.in_(_OPEN_INVOCATIONS))
        .values(status=InvocationStatus.SKIPPED, finished_at=utcnow())
    )
    session.execute(
        update(Approval)
        .where(Approval.run_id == run.id, Approval.status == ApprovalStatus.PENDING)
        .values(status=ApprovalStatus.CANCELLED, decided_at=utcnow())
    )
