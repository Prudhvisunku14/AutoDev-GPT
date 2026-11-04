"""Downloads: a verified run's project files with its verification report, as one ZIP (app/verification/artifact.py).

`GET /runs/{id}/artifact` says whether there is something to download and what it would hold; `.zip` sends it. Both
need the bearer token like every other route, and the files are read only through WorkspaceFS, inside the run's own
project folder.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request, Response, status

from app.api.deps import RedactorDep, SessionDep, WorkspaceDep, load_run
from app.api.runs import final_report
from app.core.auth import require_api_token
from app.core.redaction import Redactor
from app.schemas.api import ArtifactFileOut, ArtifactLeftOut, ArtifactOut
from app.tools.adapters.workspace_fs import WorkspaceError
from app.verification import artifact

router = APIRouter(tags=["artifacts"], dependencies=[Depends(require_api_token)])


@router.get("/runs/{run_id}/artifact", response_model=ArtifactOut)
def describe_artifact(run_id: str, request: Request, session: SessionDep, fs: WorkspaceDep, redactor: RedactorDep) -> ArtifactOut:
    run = load_run(session, run_id)
    report = final_report(request, session, run)
    return describe(report, *_files(fs, run.project_id), redactor)


@router.get("/runs/{run_id}/artifact.zip")
def download_artifact(run_id: str, request: Request, session: SessionDep, fs: WorkspaceDep, redactor: RedactorDep) -> Response:
    run = load_run(session, run_id)
    report = final_report(request, session, run)
    return send(report, *_files(fs, run.project_id), redactor, when=run.ended_at)


def _files(fs: Any, project_id: str) -> tuple[list[str], Callable[[str], bytes]]:
    try:
        paths = fs.list_files(project_id)
    except WorkspaceError:
        paths = []
    return paths, lambda path: fs.read_bytes(project_id, path)


def describe(report: dict[str, Any] | None, paths: list[str], read: Callable[[str], bytes], redactor: Redactor, *, note: str | None = None) -> ArtifactOut:
    delivery = artifact.decide(report, paths, read, redactor)
    reason, selection = delivery.reason, delivery.selection
    simulated = list(((report or {}).get("provenance") or {}).get("simulated") or [])
    return ArtifactOut(
        available=reason is None,
        reason=reason,
        verification=artifact.verification_level(report) if reason is None else None,
        filename=f"{artifact.archive_name(report)}.zip" if reason is None and report else None,
        files=[ArtifactFileOut(path=e.path, bytes=e.size) for e in selection.included],
        excluded=[ArtifactLeftOut(path=e.path, reason=e.reason) for e in selection.excluded],
        total_bytes=selection.total_bytes,
        simulated=simulated + ([note] if note else []),
    )


def send(report: dict[str, Any] | None, paths: list[str], read: Callable[[str], bytes], redactor: Redactor, *, when: Any = None, note: str | None = None) -> Response:
    delivery = artifact.decide(report, paths, read, redactor)
    if delivery.reason is not None or report is None:
        raise HTTPException(status.HTTP_409_CONFLICT, f"Nothing to download: {delivery.reason}")
    selection, data = delivery.selection, delivery.data
    body = artifact.build_zip(report, selection, data, redactor, when=when, provenance_note=note)
    name = f"{artifact.archive_name(report)}.zip"
    return Response(body, media_type="application/zip", headers={"Content-Disposition": f'attachment; filename="{name}"', "Cache-Control": "no-store"})
