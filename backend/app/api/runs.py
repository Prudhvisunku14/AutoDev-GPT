"""Runs: creation, start/resume, inspection, cancellation, events and the live stream.

Runs never start by themselves: `POST /runs/{id}/start` is the only way to make the
orchestrator call GPT (and spend money) for a run.
"""

from __future__ import annotations

import asyncio
import json
import time
from collections.abc import AsyncIterator
from typing import Annotated, Any

from fastapi import APIRouter, BackgroundTasks, Depends, Header, HTTPException, Query, Request, status
from fastapi.responses import StreamingResponse
from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from app.api.deps import EventsDep, SessionDep, load_project, load_run
from app.core.auth import require_api_token
from app.core.events import event_payload, list_events
from app.db import repositories
from app.db.models import AcceptanceCriterion, Event, PlanTask, Requirement, Run, RunState, ToolInvocation
from app.orchestrator.run_control import cancel_run as cancel_run_and_close
from app.verification.report import build_report
from app.schemas.api import (
    AnalysisOut,
    CriterionOut,
    EventOut,
    InvocationOut,
    PlanOut,
    RequirementOut,
    RunOut,
    TaskOut,
)

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


@router.post("/runs/{run_id}/start", response_model=RunOut, status_code=status.HTTP_202_ACCEPTED)
def start_run(run_id: str, request: Request, session: SessionDep, events: EventsDep, background: BackgroundTasks) -> RunOut:
    """Start a CREATED run, or resume a paused one. Calls GPT; may cost money."""
    run = load_run(session, run_id)
    if run.is_terminal:
        raise HTTPException(status.HTTP_409_CONFLICT, f"Run is already {run.state}")
    if run.state is RunState.AWAITING_APPROVAL:
        raise HTTPException(status.HTTP_409_CONFLICT, "Run is waiting for an approval decision")
    problem = request.app.state.llm_problem()
    if problem is not None:
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, f"GPT orchestrator is not configured: {problem}")
    events.record(session, project_id=run.project_id, run_id=run.id, type="run.start_requested", payload={"state": run.state})
    # Queued after the response, i.e. after this transaction has committed.
    background.add_task(request.app.state.run_manager.submit, run.id)
    return RunOut.model_validate(run)


@router.post("/runs/{run_id}/cancel", response_model=RunOut)
def cancel_run(run_id: str, session: SessionDep, events: EventsDep) -> RunOut:
    run = load_run(session, run_id)
    if run.is_terminal:
        raise HTTPException(status.HTTP_409_CONFLICT, f"Run is already {run.state}")
    cancel_run_and_close(session, run, events)
    return RunOut.model_validate(run)


@router.get("/runs/{run_id}/analysis", response_model=AnalysisOut)
def get_analysis(run_id: str, session: SessionDep) -> AnalysisOut:
    run = load_run(session, run_id)
    if run.analysis_json is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "No accepted analysis yet")
    analysis = json.loads(run.analysis_json)
    requirements = session.scalars(select(Requirement).where(Requirement.run_id == run.id).order_by(Requirement.code))
    return AnalysisOut(
        summary=analysis["summary"],
        requirements=[RequirementOut(code=r.code, text=r.text, kind=r.kind) for r in requirements],
        assumptions=analysis["assumptions"],
        ambiguities=analysis["ambiguities"],
    )


@router.get("/runs/{run_id}/plan", response_model=PlanOut)
def get_plan(run_id: str, session: SessionDep) -> PlanOut:
    run = load_run(session, run_id)
    if run.plan_json is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "No accepted plan yet")
    plan = json.loads(run.plan_json)
    tasks = session.scalars(select(PlanTask).where(PlanTask.run_id == run.id).order_by(PlanTask.order))
    return PlanOut(
        stack=plan["stack"],
        stack_rationale=plan["stack_rationale"],
        summary=plan["summary"],
        tasks=[
            TaskOut(
                order=t.order,
                title=t.title,
                description=t.description,
                capability=t.capability,
                requirement_refs=json.loads(t.requirement_refs_json),
                features=json.loads(t.features_json),
                status=t.status,
            )
            for t in tasks
        ],
    )


@router.get("/runs/{run_id}/criteria", response_model=list[CriterionOut])
def get_criteria(run_id: str, session: SessionDep) -> list[CriterionOut]:
    run = load_run(session, run_id)
    rows = session.scalars(
        select(AcceptanceCriterion).where(AcceptanceCriterion.run_id == run.id).order_by(AcceptanceCriterion.code)
    )
    return [
        CriterionOut(
            code=c.code,
            given=c.given,
            when=c.when,
            then=c.then,
            priority=c.priority,
            method=c.method,
            requirement_refs=json.loads(c.requirement_refs_json),
            status=c.status,
        )
        for c in rows
    ]


@router.get("/runs/{run_id}/invocations", response_model=list[InvocationOut])
def get_invocations(run_id: str, session: SessionDep) -> list[InvocationOut]:
    run = load_run(session, run_id)
    rows = session.scalars(select(ToolInvocation).where(ToolInvocation.run_id == run.id).order_by(ToolInvocation.seq))
    return [
        InvocationOut(
            seq=i.seq,
            call_id=i.call_id,
            tool=i.tool,
            state_at_call=i.state_at_call,
            status=i.status,
            ok=i.ok,
            error_class=i.error_class,
            arguments=_json_or_text(i.arguments_json),
            output=_json_or_text(i.output_json),
            latency_ms=i.latency_ms,
            created_at=i.created_at,
            finished_at=i.finished_at,
        )
        for i in rows
    ]


@router.get("/runs/{run_id}/report")
def get_report(run_id: str, request: Request, session: SessionDep) -> dict[str, Any]:
    """The final execution report, once the run has ended."""
    run = load_run(session, run_id)
    if run.report_json is not None:
        report: dict[str, Any] = json.loads(run.report_json)
        return report
    if not run.is_terminal:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "No final report yet")
    # A run that ended outside the coding tools, such as on a limit or a GPT error, wrote no report.
    state = request.app.state
    return build_report(session, run, run.project, gpt=state.gpt_label, sandbox=type(state.sandbox).__name__)


@router.get("/runs/{run_id}/events", response_model=list[EventOut])
def get_run_events(
    run_id: str,
    session: SessionDep,
    after_seq: Annotated[int, Query(ge=0)] = 0,
    limit: Annotated[int, Query(ge=1, le=500)] = 100,
) -> list[EventOut]:
    run = load_run(session, run_id)
    return [_event_out(e) for e in list_events(session, run_id=run.id, after_seq=after_seq, limit=limit)]


@router.get("/runs/{run_id}/events/stream")
async def stream_run_events(
    run_id: str,
    request: Request,
    after_seq: Annotated[int, Query(ge=0)] = 0,
    last_event_id: Annotated[str | None, Header(alias="Last-Event-ID")] = None,
) -> StreamingResponse:
    """Server-sent events for one run. Authenticated with the Authorization header only.

    Each event's `id` is its seq. A reconnecting client sends `Last-Event-ID` and gets
    only newer events. The stream ends when the run is terminal and fully delivered,
    or after SSE_MAX_STREAM_SECONDS (the client then reconnects).
    """
    factory: sessionmaker[Session] = request.app.state.session_factory
    exists = await asyncio.to_thread(_run_exists, factory, run_id)
    if not exists:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Run not found")
    start_seq = int(last_event_id) if last_event_id and last_event_id.strip().isdigit() else after_seq
    settings = request.app.state.settings
    return StreamingResponse(
        _sse(request, factory, run_id, start_seq, settings.sse_poll_seconds, settings.sse_heartbeat_seconds, settings.sse_max_stream_seconds),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


async def _sse(
    request: Request,
    factory: sessionmaker[Session],
    run_id: str,
    seq: int,
    poll: float,
    heartbeat: float,
    max_seconds: float,
) -> AsyncIterator[str]:
    yield "retry: 3000\n\n"
    started = last_beat = time.monotonic()
    while True:
        if await request.is_disconnected():
            return
        batch, terminal = await asyncio.to_thread(_fetch_events, factory, run_id, seq)
        for event in batch:
            seq = event.seq
            yield f"id: {event.seq}\nevent: {event.type}\ndata: {event.model_dump_json()}\n\n"
        if terminal and not batch:
            yield "event: stream.end\ndata: {}\n\n"
            return
        now = time.monotonic()
        if now - started >= max_seconds:
            yield ": stream time limit reached; reconnect with Last-Event-ID\n\n"
            return
        if now - last_beat >= heartbeat:
            last_beat = now
            yield ": keepalive\n\n"
        if not batch:
            await asyncio.sleep(poll)


def _run_exists(factory: sessionmaker[Session], run_id: str) -> bool:
    with factory() as s:
        return s.get(Run, run_id) is not None


def _fetch_events(factory: sessionmaker[Session], run_id: str, after_seq: int) -> tuple[list[EventOut], bool]:
    with factory() as s:
        # Read the run state first: if it is terminal now, every event of the run is already stored.
        run = s.get(Run, run_id)
        terminal = run is not None and run.is_terminal
        rows = list_events(s, run_id=run_id, after_seq=after_seq, limit=200)
        return [_event_out(e) for e in rows], terminal


def _event_out(e: Event) -> EventOut:
    return EventOut(seq=e.seq, type=e.type, project_id=e.project_id, run_id=e.run_id, payload=event_payload(e), ts=e.ts)


def _json_or_text(value: str | None) -> Any:
    if value is None:
        return None
    try:
        return json.loads(value)
    except json.JSONDecodeError:
        return value
