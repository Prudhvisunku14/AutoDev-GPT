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
    tool_calls: int
    cost_known: bool
    active_seconds: float
    pause_reason: str | None
    status_detail: str | None


class RequirementOut(BaseModel):
    code: str
    text: str
    kind: str


class AnalysisOut(BaseModel):
    summary: str
    requirements: list[RequirementOut]
    assumptions: list[str]
    ambiguities: list[str]


class TaskOut(BaseModel):
    order: int
    title: str
    description: str
    capability: str
    requirement_refs: list[str]
    features: dict[str, Any]
    status: str


class PlanOut(BaseModel):
    stack: str
    stack_rationale: str
    summary: str
    tasks: list[TaskOut]


class CriterionOut(BaseModel):
    code: str
    given: str
    when: str
    then: str
    priority: str
    method: str
    requirement_refs: list[str]
    status: str


class InvocationOut(BaseModel):
    seq: int
    call_id: str
    tool: str
    state_at_call: str
    status: str
    ok: bool | None
    error_class: str | None
    arguments: Any
    output: Any
    latency_ms: int | None
    created_at: datetime
    finished_at: datetime | None


class ApprovalOut(BaseModel):
    id: str
    run_id: str
    kind: str
    tool: str
    risk: str
    payload: Any
    status: str
    response_text: str | None
    created_at: datetime
    decided_at: datetime | None


class ApprovalDecisionIn(StrictInput):
    decision: Literal["approve", "deny"]
    response: Annotated[str, StringConstraints(max_length=5_000)] | None = None


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
