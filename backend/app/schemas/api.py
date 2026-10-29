"""Pydantic v2 DTOs for the REST API (ARCHITECTURE §11.1)."""

from __future__ import annotations

from datetime import datetime
from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field, StringConstraints

from app.db.models import ProjectStatus, RunState

NonBlank = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1)]


class StrictInput(BaseModel):
    model_config = ConfigDict(extra="forbid")


class ProjectCreate(StrictInput):
    name: Annotated[NonBlank, StringConstraints(max_length=200)]
    requirement_text: Annotated[NonBlank, StringConstraints(max_length=20_000)]


class ProjectOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    name: str
    requirement_text: str
    stack: str | None
    status: ProjectStatus
    created_at: datetime


class RunOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    project_id: str
    state: RunState
    resume_state: RunState | None
    turns: int
    tokens_in: int
    tokens_out: int
    cost_usd: float
    created_at: datetime
    started_at: datetime | None
    ended_at: datetime | None
    outcome: str | None


class EventOut(BaseModel):
    seq: int
    type: str
    project_id: str
    run_id: str | None
    payload: dict[str, Any]
    ts: datetime


class FileList(BaseModel):
    files: list[str]


class FileContent(BaseModel):
    path: str
    content: str


class HealthOut(BaseModel):
    status: Literal["ok"]


class StatusOut(BaseModel):
    status: Literal["ok", "degraded"]
    version: str
    phase: str
    database: Literal["ok", "error"]
    workspaces_writable: bool
    egress_components: list[str]
    projects: int
    runs: int


class ErrorOut(BaseModel):
    detail: str = Field(description="Human-readable error message")
