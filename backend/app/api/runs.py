"""Run creation, inspection, cancellation and event listing.

Phase 1 only records runs in the CREATED state. Executing them is the run
worker's job in Phase 2.
"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query, status

from app.api.deps import EventsDep, SessionDep, load_project, load_run
from app.core.auth import require_api_token
from app.core.events import event_payload, list_events
from app.db import repositories
from app.schemas.api import EventOut, RunOut

router = APIRouter(tags=["runs"], dependencies=[Depends(require_api_token)])


@router.post("/projects/{project_id}/runs", response_model=RunOut, status_code=status.HTTP_201_CREATED)
def create_run(project_id: str, session: SessionDep, events: EventsDep) -> RunOut:
    project = load_project(session, project_id)
    active = repositories.get_active_run(session, project.id)
    if active is not None:
        raise HTTPException(status.HTTP_409_CONFLICT, f"Project already has an active run ({active.id})")
    run = repositories.create_run(session, project)
    events.record(session, project_id=project.id, run_id=run.id, type="run.created", payload={"state": run.state})
    return RunOut.model_validate(run)


@router.get("/projects/{project_id}/runs", response_model=list[RunOut])
def list_project_runs(project_id: str, session: SessionDep) -> list[RunOut]:
    project = load_project(session, project_id)
    return [RunOut.model_validate(r) for r in repositories.list_runs(session, project.id)]


@router.get("/runs/{run_id}", response_model=RunOut)
def get_run(run_id: str, session: SessionDep) -> RunOut:
    return RunOut.model_validate(load_run(session, run_id))


@router.post("/runs/{run_id}/cancel", response_model=RunOut)
def cancel_run(run_id: str, session: SessionDep, events: EventsDep) -> RunOut:
    run = load_run(session, run_id)
    if run.is_terminal:
        raise HTTPException(status.HTTP_409_CONFLICT, f"Run is already {run.state}")
    previous = run.state
    repositories.cancel_run(run)
    events.record(
        session,
        project_id=run.project_id,
        run_id=run.id,
        type="run.cancelled",
        payload={"previous_state": previous},
    )
    return RunOut.model_validate(run)


@router.get("/runs/{run_id}/events", response_model=list[EventOut])
def get_run_events(
    run_id: str,
    session: SessionDep,
    after_seq: Annotated[int, Query(ge=0)] = 0,
    limit: Annotated[int, Query(ge=1, le=500)] = 100,
) -> list[EventOut]:
    """JSON event list. Phase 2 adds SSE streaming on this route (`Accept: text/event-stream`)."""
    run = load_run(session, run_id)
    return [
        EventOut(
            seq=e.seq,
            type=e.type,
            project_id=e.project_id,
            run_id=e.run_id,
            payload=event_payload(e),
            ts=e.ts,
        )
        for e in list_events(session, run_id=run.id, after_seq=after_seq, limit=limit)
    ]
