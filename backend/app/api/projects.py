"""Project creation, listing and read-only workspace file access."""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query, status

from app.api.deps import EventsDep, RedactorDep, SessionDep, WorkspaceDep, load_project
from app.core.auth import require_api_token
from app.db import repositories
from app.db.models import new_id
from app.schemas.api import FileContent, FileList, ProjectCreate, ProjectOut
from app.tools.adapters.workspace_fs import (
    PathRejected,
    WorkspaceError,
    WorkspaceLimitExceeded,
    WorkspaceNotFound,
)
from app.verification.acceptance import AcceptanceSuite, SuiteCriterion, SuiteError, freeze, validate

router = APIRouter(prefix="/projects", tags=["projects"], dependencies=[Depends(require_api_token)])


@router.post("", response_model=ProjectOut, status_code=status.HTTP_201_CREATED)
def create_project(
    body: ProjectCreate, session: SessionDep, workspace: WorkspaceDep, events: EventsDep, redactor: RedactorDep
) -> ProjectOut:
    project_id = new_id()
    frozen = None
    if body.acceptance is not None:  # parsed and checked here, never executed
        suite = AcceptanceSuite(tuple(SuiteCriterion(c.code, c.description, c.priority, c.component) for c in body.acceptance.criteria), dict(body.acceptance.files))
        try:
            validate(suite)
        except SuiteError as exc:
            raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, f"acceptance suite: {exc}") from exc
        frozen, _ = freeze(suite)
    # A requirement may contain a pasted credential; secrets never reach the DB (SECURITY T6).
    project = repositories.create_project(
        session,
        project_id=project_id,
        name=redactor.redact_text(body.name),
        requirement_text=redactor.redact_text(body.requirement_text),
        workspace_path=str(workspace.root / project_id),
        acceptance_json=redactor.redact_text(frozen) if frozen is not None else None,
    )
    workspace.create_project(project_id)
    events.record(session, project_id=project.id, type="project.created", payload={"name": project.name, "acceptance_criteria": len(body.acceptance.criteria) if body.acceptance else 0})
    return ProjectOut.model_validate(project)


@router.get("", response_model=list[ProjectOut])
def list_projects(
    session: SessionDep,
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> list[ProjectOut]:
    return [ProjectOut.model_validate(p) for p in repositories.list_projects(session, limit=limit, offset=offset)]


@router.get("/{project_id}", response_model=ProjectOut)
def get_project(project_id: str, session: SessionDep) -> ProjectOut:
    return ProjectOut.model_validate(load_project(session, project_id))


@router.get("/{project_id}/files", response_model=FileList)
def list_files(project_id: str, session: SessionDep, workspace: WorkspaceDep) -> FileList:
    project = load_project(session, project_id)
    try:
        return FileList(files=workspace.list_files(project.id))
    except WorkspaceError as exc:
        raise _http_error(exc) from exc


@router.get("/{project_id}/files/content", response_model=FileContent)
def read_file(
    project_id: str,
    path: Annotated[str, Query(min_length=1, max_length=1024)],
    session: SessionDep,
    workspace: WorkspaceDep,
) -> FileContent:
    project = load_project(session, project_id)
    try:
        return FileContent(path=path, content=workspace.read_text(project.id, path))
    except WorkspaceError as exc:
        raise _http_error(exc) from exc


def _http_error(exc: WorkspaceError) -> HTTPException:
    if isinstance(exc, WorkspaceNotFound):
        return HTTPException(status.HTTP_404_NOT_FOUND, str(exc))
    if isinstance(exc, WorkspaceLimitExceeded):
        return HTTPException(status.HTTP_413_CONTENT_TOO_LARGE, str(exc))
    if isinstance(exc, PathRejected):
        return HTTPException(status.HTTP_400_BAD_REQUEST, str(exc))
    return HTTPException(status.HTTP_400_BAD_REQUEST, "workspace error")
