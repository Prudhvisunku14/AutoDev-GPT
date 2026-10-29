"""Data access for projects and runs. Callers own the transaction."""

from __future__ import annotations

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.db.models import TERMINAL_RUN_STATES, Project, Run, RunState, utcnow


def create_project(
    session: Session, *, project_id: str, name: str, requirement_text: str, workspace_path: str
) -> Project:
    project = Project(
        id=project_id, name=name, requirement_text=requirement_text, workspace_path=workspace_path
    )
    session.add(project)
    session.flush()
    return project


def get_project(session: Session, project_id: str) -> Project | None:
    return session.get(Project, project_id)


def list_projects(session: Session, *, limit: int, offset: int) -> list[Project]:
    stmt = select(Project).order_by(Project.created_at.desc(), Project.id).limit(limit).offset(offset)
    return list(session.scalars(stmt))


def count_projects(session: Session) -> int:
    return session.scalar(select(func.count()).select_from(Project)) or 0


def get_active_run(session: Session, project_id: str) -> Run | None:
    stmt = select(Run).where(Run.project_id == project_id, Run.state.not_in(TERMINAL_RUN_STATES)).limit(1)
    return session.scalars(stmt).first()


def list_runs(session: Session, project_id: str) -> list[Run]:
    stmt = select(Run).where(Run.project_id == project_id).order_by(Run.created_at.desc(), Run.id)
    return list(session.scalars(stmt))


def create_run(session: Session, project: Project) -> Run:
    run = Run(project_id=project.id, state=RunState.CREATED)
    session.add(run)
    session.flush()
    return run


def get_run(session: Session, run_id: str) -> Run | None:
    return session.get(Run, run_id)


def count_runs(session: Session) -> int:
    return session.scalar(select(func.count()).select_from(Run)) or 0


def cancel_run(run: Run) -> None:
    run.resume_state = None
    run.state = RunState.CANCELLED
    run.outcome = "cancelled"
    run.ended_at = utcnow()
