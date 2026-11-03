"""Earlier terminal builds, read-only: runs that scripts/live_demo.py and scripts/live_build.py recorded in their own folders.

Each of those runs had its own database and workspace under HISTORY_DIR (`<UTC timestamp>/demo.db`, `workspaces/`). They
are shown from the folder's `summary.json`, which the script wrote when the run ended, and from the delivered files.
**Their databases are never opened**: SQLite touches a database's `-wal` and `-shm` files even to read it, and these
folders are evidence. Nothing here writes anywhere: a folder without a workspace is not given one.

Every record says it is historical: it ran in the terminal, not on this backend, and it is not started again here.
"""

from __future__ import annotations

import json
import re
from datetime import UTC, datetime
from pathlib import Path
from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response, status

from app.api.artifacts import describe, send
from app.api.deps import RedactorDep
from app.core.auth import require_api_token
from app.core.redaction import Redactor
from app.schemas.api import ArtifactOut, FileContent, FileList, HistoryRunOut
from app.tools.adapters.workspace_fs import PROJECT_ID_PATTERN, PathRejected, WorkspaceError, WorkspaceFS, WorkspaceLimitExceeded, WorkspaceNotFound

router = APIRouter(prefix="/history", tags=["history"], dependencies=[Depends(require_api_token)])

RECORD_ID = re.compile(r"^\d{8}T\d{6}Z$")
SOURCE = "terminal build (scripts/live_demo.py or scripts/live_build.py), with its own database"
NOTE = "Historical: recorded by a terminal build, not run on this backend."
_MAX_SUMMARY_BYTES = 5_000_000


@router.get("/runs", response_model=list[HistoryRunOut])
def list_history(request: Request, redactor: RedactorDep) -> list[HistoryRunOut]:
    root = _root(request)
    if root is None:
        return []
    records = [_record(folder, redactor, with_report=False) for folder in sorted(root.iterdir(), reverse=True) if RECORD_ID.fullmatch(folder.name)]
    return [r for r in records if r is not None]


@router.get("/runs/{record_id}", response_model=HistoryRunOut)
def get_history(record_id: str, request: Request, redactor: RedactorDep) -> HistoryRunOut:
    record = _record(_folder(request, record_id), redactor, with_report=True)
    if record is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "This run folder has no summary")
    return record


@router.get("/runs/{record_id}/files", response_model=FileList)
def list_history_files(record_id: str, request: Request, redactor: RedactorDep) -> FileList:
    fs, project_id, _ = _workspace(request, record_id, redactor)
    return FileList(files=fs.list_files(project_id) if fs is not None else [])


@router.get("/runs/{record_id}/files/content", response_model=FileContent)
def read_history_file(record_id: str, request: Request, redactor: RedactorDep, path: Annotated[str, Query(min_length=1, max_length=1024)]) -> FileContent:
    fs, project_id, _ = _workspace(request, record_id, redactor)
    if fs is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "This run left no files")
    try:
        return FileContent(path=path, content=fs.read_text(project_id, path))
    except WorkspaceError as exc:
        raise _http_error(exc) from exc


@router.get("/runs/{record_id}/artifact", response_model=ArtifactOut)
def describe_history_artifact(record_id: str, request: Request, redactor: RedactorDep) -> ArtifactOut:
    fs, project_id, report = _workspace(request, record_id, redactor)
    paths = fs.list_files(project_id) if fs is not None else []
    return describe(report, paths, lambda p: _read(fs, project_id, p), redactor, note=NOTE)


@router.get("/runs/{record_id}/artifact.zip")
def download_history_artifact(record_id: str, request: Request, redactor: RedactorDep) -> Response:
    fs, project_id, report = _workspace(request, record_id, redactor)
    paths = fs.list_files(project_id) if fs is not None else []
    return send(report, paths, lambda p: _read(fs, project_id, p), redactor, when=_recorded_at(record_id), note=NOTE)


def _root(request: Request) -> Path | None:
    configured: Path | None = request.app.state.settings.history_dir
    return configured if configured is not None and configured.is_dir() and not configured.is_symlink() else None


def _folder(request: Request, record_id: str) -> Path:
    root = _root(request)
    if root is None or not RECORD_ID.fullmatch(record_id) or not (root / record_id).is_dir():
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Historical run not found")
    return root / record_id


def _recorded_at(record_id: str) -> datetime:
    return datetime.strptime(record_id, "%Y%m%dT%H%M%SZ").replace(tzinfo=UTC)


def _summary(folder: Path) -> dict[str, Any] | None:
    path = folder / "summary.json"
    if not path.is_file() or path.is_symlink() or path.stat().st_size > _MAX_SUMMARY_BYTES:
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def _record(folder: Path, redactor: Redactor, *, with_report: bool) -> HistoryRunOut | None:
    summary = _summary(folder)
    if summary is None:
        return None
    report = summary.get("report") if isinstance(summary.get("report"), dict) and summary.get("report") else None
    project = (report or {}).get("project") or {}
    run = (report or {}).get("run") or {}
    delivered = summary.get("delivered")
    return HistoryRunOut(
        id=folder.name,
        recorded_at=_recorded_at(folder.name),
        source=SOURCE,
        project_name=project.get("name"),
        requirement=project.get("requirement"),
        run_id=run.get("id"),
        state=summary.get("state"),
        outcome=summary.get("outcome"),
        detail=summary.get("detail"),
        cost_usd=summary.get("cost_usd") if isinstance(summary.get("cost_usd"), int | float) else None,
        tokens=_number(summary.get("tokens_in"), summary.get("tokens_out")),
        seconds=summary.get("seconds") if isinstance(summary.get("seconds"), int | float) else None,
        turns=summary.get("turns") if isinstance(summary.get("turns"), int) else None,
        delivered=[str(d) for d in delivered] if isinstance(delivered, list) else [],
        report=redactor.redact(report) if with_report and report is not None else None,
    )


def _number(*values: Any) -> int | None:
    numbers = [v for v in values if isinstance(v, int)]
    return sum(numbers) if numbers else None


def _workspace(request: Request, record_id: str, redactor: Redactor) -> tuple[WorkspaceFS | None, str, dict[str, Any] | None]:
    """The run's project folder, jailed, and its report. None when the run left no workspace: none is created."""
    folder = _folder(request, record_id)
    summary = _summary(folder) or {}
    report = summary.get("report") if isinstance(summary.get("report"), dict) and summary.get("report") else None
    project_id = str(((report or {}).get("project") or {}).get("id") or "")
    workspaces = folder / "workspaces"
    if not PROJECT_ID_PATTERN.fullmatch(project_id) or not workspaces.is_dir() or not (workspaces / project_id).is_dir():
        return None, project_id, redactor.redact(report) if report is not None else None
    settings = request.app.state.settings
    fs = WorkspaceFS(workspaces, max_file_bytes=settings.workspace_max_file_bytes, max_files=settings.workspace_max_files)  # the folder exists: nothing is created
    return fs, project_id, redactor.redact(report) if report is not None else None


def _read(fs: WorkspaceFS | None, project_id: str, path: str) -> bytes:
    if fs is None:
        raise WorkspaceNotFound("this run left no files")
    return fs.read_bytes(project_id, path)


def _http_error(exc: WorkspaceError) -> HTTPException:
    if isinstance(exc, WorkspaceNotFound):
        return HTTPException(status.HTTP_404_NOT_FOUND, str(exc))
    if isinstance(exc, WorkspaceLimitExceeded):
        return HTTPException(status.HTTP_413_CONTENT_TOO_LARGE, str(exc))
    if isinstance(exc, PathRejected):
        return HTTPException(status.HTTP_400_BAD_REQUEST, str(exc))
    return HTTPException(status.HTTP_400_BAD_REQUEST, str(exc))
