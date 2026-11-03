"""Pydantic v2 DTOs for the REST API (ARCHITECTURE §11.1)."""

from __future__ import annotations

from datetime import datetime
from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field, StringConstraints

from app.db.models import ProjectStatus, RunState

NonBlank = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1)]


class StrictInput(BaseModel):
    model_config = ConfigDict(extra="forbid")


class AcceptanceCriterionIn(StrictInput):
    code: Annotated[str, StringConstraints(pattern=r"^AT-\d{3}$")]
    description: Annotated[NonBlank, StringConstraints(max_length=500)]
    priority: Literal["MUST", "SHOULD"] = "MUST"
    # A component of the plan, such as "frontend": the criterion is then proved only by that component's front-end files.
    component: Annotated[str, StringConstraints(pattern=r"^[a-z][a-z0-9_-]{0,31}$")] | None = None


class AcceptanceSuiteIn(StrictInput):
    """The evaluator's tests, given before any code exists. The coding provider never sees them."""

    criteria: Annotated[list[AcceptanceCriterionIn], Field(min_length=1, max_length=30)]
    files: Annotated[dict[str, Annotated[str, StringConstraints(max_length=64_000)]], Field(min_length=1, max_length=5)]


class ProjectCreate(StrictInput):
    name: Annotated[NonBlank, StringConstraints(max_length=200)]
    requirement_text: Annotated[NonBlank, StringConstraints(max_length=20_000)]
    stack: Annotated[str, StringConstraints(min_length=2, max_length=64)] | None = None
    acceptance: AcceptanceSuiteIn | None = None  # fixed at creation: there is no way to change it later


class ProjectOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    name: str
    requirement_text: str
    stack: str | None
    status: ProjectStatus
    created_at: datetime
    has_acceptance_suite: bool = False


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
    acceptance_sha256: str | None = None  # the frozen acceptance suite this run is judged by


class RunListItem(RunOut):
    project_name: str


class StartRunIn(StrictInput):
    """A person's spending approval: the most this run may spend, which must be the limit the backend enforces."""

    approved_max_usd: Annotated[float, Field(gt=0, le=1_000)]


class ArtifactFileOut(BaseModel):
    path: str
    bytes: int


class ArtifactLeftOut(BaseModel):
    path: str
    reason: str


class ArtifactOut(BaseModel):
    """What a download of this run would hold. `available` only for a COMPLETED run whose independent acceptance PASSED."""

    available: bool
    reason: str | None
    verification: str | None = None  # "independent" or "generated-tests" when available
    filename: str | None
    files: list[ArtifactFileOut]
    excluded: list[ArtifactLeftOut]
    total_bytes: int
    simulated: list[str]


class SpendingOut(BaseModel):
    total_usd: float  # what runs with a known cost have spent, on this backend's database
    runs: int
    unknown_cost_runs: int
    active_runs: int
    active_committed_usd: float  # runs not yet ended, each at its whole limit or what it has spent if more


class ReferenceCostOut(BaseModel):
    """What earlier finished runs on this backend cost. Earlier runs, not an estimate of the next one."""

    finished_runs: int
    min_usd: float | None
    median_usd: float | None
    max_usd: float | None


class BudgetOut(BaseModel):
    cost_known: bool
    run_limit_usd: float
    coding_attempt_limit_usd: float | None
    model: str | None
    price_input_per_mtok: float | None
    price_output_per_mtok: float | None
    max_turns: int
    max_tool_calls: int
    max_repair_attempts: int
    run_wall_clock_seconds: float
    approval_required: bool
    session: dict[str, Any] | None  # a session-wide cap, when the server has one (scripts/live_server.py)
    recorded: SpendingOut
    reference: ReferenceCostOut
    estimate: str


class PartOut(BaseModel):
    ready: bool
    label: str
    detail: str | None
    simulated: bool


class StackOut(BaseModel):
    id: str
    name: str
    language: str
    frameworks: list[str]
    description: str
    verifiable: bool
    reason: str


class CapabilitiesOut(BaseModel):
    gpt: PartOut
    coding: PartOut
    sandbox: PartOut
    prices: PartOut
    stacks: list[StackOut]
    can_build: bool
    problems: list[str]
    simulated: list[str]
    acceptance_suites: bool  # projects may carry an evaluator's frozen suite (POST /projects with `acceptance`)
    history: bool  # earlier terminal builds can be shown (GET /history/runs)


class HistoryRunOut(BaseModel):
    """A run recorded by scripts/live_demo.py or scripts/live_build.py, read from its folder's summary.json. Historical:
    it ran in the terminal with its own database, not on this backend."""

    id: str  # the run folder's name, a UTC timestamp
    recorded_at: datetime
    source: str
    project_name: str | None
    requirement: str | None
    run_id: str | None
    state: str | None
    outcome: str | None
    detail: str | None
    cost_usd: float | None
    tokens: int | None
    seconds: float | None
    turns: int | None
    delivered: list[str]
    report: dict[str, Any] | None


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
    details: Any = None  # names, types, locations, operation and reason, for a person deciding; never content
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
