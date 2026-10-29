"""Deciding approvals and answering human-input requests (ARCHITECTURE §5.4).

The decision is recorded here; an approved tool call is executed later by the run
worker, never inside the HTTP request. A conditional UPDATE guarantees that a pending
approval is decided exactly once, so a double click cannot run an action twice.
"""

from __future__ import annotations

from typing import Literal

from sqlalchemy import update
from sqlalchemy.orm import Session

from app.core.events import EventRecorder
from app.core.redaction import Redactor
from app.db.models import (
    Approval,
    ApprovalKind,
    ApprovalStatus,
    InvocationStatus,
    Run,
    RunState,
    ToolInvocation,
    utcnow,
)
from app.orchestrator.run_control import change_state
from app.tools.base import ErrorClass, ToolResult
from app.tools.executor import ToolExecutor


class ApprovalConflict(Exception):
    """The approval was already decided, or its run is no longer waiting."""


class ApprovalInvalid(Exception):
    """The decision is malformed (for example, an answer without text)."""


def decide(
    session: Session,
    approval: Approval,
    *,
    decision: Literal["approve", "deny"],
    response: str | None,
    executor: ToolExecutor,
    events: EventRecorder,
    redactor: Redactor,
) -> Approval:
    run = session.get(Run, approval.run_id)
    inv = session.get(ToolInvocation, approval.invocation_id)
    assert run is not None and inv is not None
    if approval.status is not ApprovalStatus.PENDING:
        raise ApprovalConflict(f"approval is already {approval.status}")
    if run.state is not RunState.AWAITING_APPROVAL or inv.status is not InvocationStatus.AWAITING_APPROVAL:
        raise ApprovalConflict(f"run is {run.state}, not awaiting this approval")
    answer = redactor.redact_text(response.strip()) if response and response.strip() else None
    if approval.kind is ApprovalKind.HUMAN_INPUT and decision == "approve" and answer is None:
        raise ApprovalInvalid("an answer is required to approve a human-input request")

    new_status = ApprovalStatus.APPROVED if decision == "approve" else ApprovalStatus.DENIED
    claimed = session.execute(
        update(Approval)
        .where(Approval.id == approval.id, Approval.status == ApprovalStatus.PENDING)
        .values(status=new_status, decided_at=utcnow(), response_text=answer)
        .execution_options(synchronize_session=False)
    )
    if claimed.rowcount != 1:
        raise ApprovalConflict("approval was decided concurrently")
    session.refresh(approval)

    if approval.kind is ApprovalKind.TOOL:
        if decision == "approve":
            inv.status = InvocationStatus.APPROVED  # the worker executes it exactly once
        else:
            _complete(run, inv, executor, ToolResult.failure(inv.tool, ErrorClass.APPROVAL_DENIED, "The user denied this action."), InvocationStatus.DENIED)
    elif decision == "approve":
        _complete(run, inv, executor, ToolResult(ok=True, tool=inv.tool, data={"answer": answer}), InvocationStatus.SUCCEEDED)
    else:
        result = ToolResult.failure(
            inv.tool,
            ErrorClass.HUMAN_DECLINED,
            "The user declined to answer. Continue with a reasonable assumption and record it.",
        )
        _complete(run, inv, executor, result, InvocationStatus.FAILED)

    events.record(
        session,
        project_id=run.project_id,
        run_id=run.id,
        type="approval.decided",
        payload={"approval_id": approval.id, "kind": approval.kind, "tool": approval.tool, "decision": new_status},
    )
    assert run.resume_state is not None
    change_state(session, run, run.resume_state, events, reason=f"approval {new_status.lower()}")
    return approval


def _complete(run: Run, inv: ToolInvocation, executor: ToolExecutor, result: ToolResult, status: InvocationStatus) -> None:
    result.state = (run.resume_state or run.state).value
    inv.status = status
    inv.ok = result.ok
    inv.error_class = result.error.error_class if result.error else None
    inv.output_json = executor.render(result)
    inv.finished_at = utcnow()
    run.tool_calls += 1
