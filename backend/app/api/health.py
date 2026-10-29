"""Health (no auth) and status (auth) endpoints."""

from __future__ import annotations

import logging
import tempfile

from fastapi import APIRouter, Depends, Request
from sqlalchemy import text

from app import __version__
from app.api.deps import SessionDep
from app.core.auth import require_api_token
from app.db import repositories
from app.schemas.api import HealthOut, StatusOut

PHASE = "1 - backend foundation"

logger = logging.getLogger("devpilot.api")

public_router = APIRouter(tags=["health"])
router = APIRouter(tags=["health"], dependencies=[Depends(require_api_token)])


@public_router.get("/health", response_model=HealthOut)
def health() -> HealthOut:
    """Liveness only. Deliberately reveals nothing about the deployment."""
    return HealthOut(status="ok")


@router.get("/status", response_model=StatusOut)
def get_status(request: Request, session: SessionDep) -> StatusOut:
    database_ok = True
    projects = runs = 0
    try:
        session.execute(text("SELECT 1"))
        projects = repositories.count_projects(session)
        runs = repositories.count_runs(session)
    except Exception:
        logger.exception("status: database check failed")
        database_ok = False

    workspaces_writable = True
    try:
        with tempfile.TemporaryFile(dir=request.app.state.workspace_fs.root):
            pass
    except OSError:
        workspaces_writable = False

    healthy = database_ok and workspaces_writable
    return StatusOut(
        status="ok" if healthy else "degraded",
        version=__version__,
        phase=PHASE,
        database="ok" if database_ok else "error",
        workspaces_writable=workspaces_writable,
        egress_components=sorted(request.app.state.egress.components),
        projects=projects,
        runs=runs,
    )
