"""Approval queue: list pending decisions and decide them (ARCHITECTURE §5.4)."""

from __future__ import annotations

import json
from typing import Annotated, Literal

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Query, Request, status
from sqlalchemy import select

from app.api.deps import ID_PATTERN, EventsDep, RedactorDep, SessionDep
from app.core.auth import require_api_token
from app.db.models import Approval, ApprovalStatus
from app.orchestrator.approvals import ApprovalConflict, ApprovalInvalid, decide
from app.schemas.api import ApprovalDecisionIn, ApprovalOut

router = APIRouter(prefix="/approvals", tags=["approvals"], dependencies=[Depends(require_api_token)])


@router.get("", response_model=list[ApprovalOut])
def list_approvals(
    session: SessionDep,
    status_filter: Annotated[
        Literal["pending", "approved", "denied", "cancelled", "all"], Query(alias="status")
    ] = "pending",
    run_id: str | None = None,
) -> list[ApprovalOut]:
    stmt = select(Approval).order_by(Approval.created_at)
    if status_filter != "all":
        stmt = stmt.where(Approval.status == ApprovalStatus(status_filter.upper()))
    if run_id is not None:
        stmt = stmt.where(Approval.run_id == run_id)
    return [_out(a) for a in session.scalars(stmt)]


@router.post("/{approval_id}/decision", response_model=ApprovalOut)
def decide_approval(
    approval_id: str,
    body: ApprovalDecisionIn,
    request: Request,
    session: SessionDep,
    events: EventsDep,
    redactor: RedactorDep,
    background: BackgroundTasks,
) -> ApprovalOut:
    approval = session.get(Approval, approval_id) if ID_PATTERN.fullmatch(approval_id) else None
    if approval is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Approval not found")
    try:
        decided = decide(
            session,
            approval,
            decision=body.decision,
            response=body.response,
            executor=request.app.state.executor,
            events=events,
            redactor=redactor,
        )
    except ApprovalConflict as exc:
        raise HTTPException(status.HTTP_409_CONFLICT, str(exc)) from exc
    except ApprovalInvalid as exc:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, str(exc)) from exc
    # The run continues automatically: deciding is the user's go-ahead.
    background.add_task(request.app.state.run_manager.submit, decided.run_id)
    return _out(decided)


def _out(a: Approval) -> ApprovalOut:
    try:
        payload = json.loads(a.payload_json)
    except json.JSONDecodeError:
        payload = a.payload_json
    return ApprovalOut(
        id=a.id,
        run_id=a.run_id,
        kind=a.kind,
        tool=a.tool,
        risk=a.risk,
        payload=payload,
        details=json.loads(a.details_json) if a.details_json else None,
        status=a.status,
        response_text=a.response_text,
        created_at=a.created_at,
        decided_at=a.decided_at,
    )
