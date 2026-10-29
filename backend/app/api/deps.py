"""Shared FastAPI dependencies."""

from __future__ import annotations

from collections.abc import Iterator
from typing import Annotated

from fastapi import Depends, HTTPException, Request, status
from sqlalchemy.orm import Session

from app.core.events import EventRecorder
from app.core.redaction import Redactor
from app.db import repositories
from app.db.models import Project, Run
from app.tools.adapters.workspace_fs import PROJECT_ID_PATTERN, WorkspaceFS


def get_session(request: Request) -> Iterator[Session]:
    """One transaction per request: commit on success, roll back on any error."""
    session: Session = request.app.state.session_factory()
    try:
        yield session
        session.commit()
    except BaseException:
        session.rollback()
        raise
    finally:
        session.close()


def get_workspace_fs(request: Request) -> WorkspaceFS:
    return request.app.state.workspace_fs


def get_events(request: Request) -> EventRecorder:
    return request.app.state.events


def get_redactor(request: Request) -> Redactor:
    return request.app.state.redactor


# scope="function": commit before the response is sent, so a failed commit is a 500, not a false 201.
SessionDep = Annotated[Session, Depends(get_session, scope="function")]
WorkspaceDep = Annotated[WorkspaceFS, Depends(get_workspace_fs)]
EventsDep = Annotated[EventRecorder, Depends(get_events)]
RedactorDep = Annotated[Redactor, Depends(get_redactor)]


# Project and run ids share one format: 32 lowercase hex characters (uuid4().hex).
ID_PATTERN = PROJECT_ID_PATTERN


def load_project(session: Session, project_id: str) -> Project:
    project = repositories.get_project(session, project_id) if ID_PATTERN.fullmatch(project_id) else None
    if project is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Project not found")
    return project


def load_run(session: Session, run_id: str) -> Run:
    run = repositories.get_run(session, run_id) if ID_PATTERN.fullmatch(run_id) else None
    if run is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Run not found")
    return run
