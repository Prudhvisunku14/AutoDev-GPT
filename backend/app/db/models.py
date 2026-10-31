"""SQLAlchemy 2.0 typed ORM models (ARCHITECTURE §10).

Phase 1: `projects`, `runs`, `events`.
Phase 2: `requirements`, `tasks`, `acceptance_criteria`, `tool_invocations`, `approvals`,
plus orchestration columns on `runs`. Columns added after Phase 1 carry a server default
so `init_db` can add them to an existing database (see db/session.py).
Phase 4: `coding_attempts`.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from enum import StrEnum

from sqlalchemy import (
    Boolean,
    DateTime,
    Enum,
    Float,
    ForeignKey,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.engine import Dialect
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship
from sqlalchemy.types import TypeDecorator

from app.coding.types import AttemptPhase


def new_id() -> str:
    return uuid.uuid4().hex


def utcnow() -> datetime:
    return datetime.now(UTC)


class UTCDateTime(TypeDecorator[datetime]):
    """Stores UTC; always returns timezone-aware datetimes (SQLite drops tzinfo)."""

    impl = DateTime
    cache_ok = True

    def process_bind_param(self, value: datetime | None, dialect: Dialect) -> datetime | None:
        if value is None:
            return None
        if value.tzinfo is None:
            raise ValueError("naive datetimes are not allowed; use UTC-aware values")
        return value.astimezone(UTC).replace(tzinfo=None)

    def process_result_value(self, value: datetime | None, dialect: Dialect) -> datetime | None:
        return None if value is None else value.replace(tzinfo=UTC)


class RunState(StrEnum):
    """Run lifecycle states (ARCHITECTURE §4.1)."""

    CREATED = "CREATED"
    ANALYZING = "ANALYZING"
    PLANNING = "PLANNING"
    DEFINING_CRITERIA = "DEFINING_CRITERIA"
    IMPLEMENTING = "IMPLEMENTING"
    VERIFYING = "VERIFYING"
    REPAIRING = "REPAIRING"
    DOCUMENTING = "DOCUMENTING"
    COMPLETED = "COMPLETED"
    AWAITING_APPROVAL = "AWAITING_APPROVAL"
    FAILED = "FAILED"
    CANCELLED = "CANCELLED"
    STOPPED_BY_POLICY = "STOPPED_BY_POLICY"


TERMINAL_RUN_STATES = frozenset(
    {RunState.COMPLETED, RunState.FAILED, RunState.CANCELLED, RunState.STOPPED_BY_POLICY}
)


class ProjectStatus(StrEnum):
    ACTIVE = "ACTIVE"
    ARCHIVED = "ARCHIVED"


class InvocationStatus(StrEnum):
    """Lifecycle of one GPT function call (one `call_id`)."""

    QUEUED = "QUEUED"  # received from GPT, not yet checked
    AWAITING_APPROVAL = "AWAITING_APPROVAL"
    APPROVED = "APPROVED"  # approved, waiting for the worker to execute it
    STARTED = "STARTED"  # claimed by the worker; a crash leaves it here
    SUCCEEDED = "SUCCEEDED"
    FAILED = "FAILED"
    DENIED = "DENIED"
    INTERRUPTED = "INTERRUPTED"  # found STARTED after a crash; never re-executed
    SKIPPED = "SKIPPED"  # the run ended before the call ran
    DISCARDED = "DISCARDED"  # finished after the run ended: kept for the audit, never sent to GPT


# Statuses whose output must be delivered to GPT as a function_call_output.
FINAL_INVOCATION_STATUSES = frozenset(
    {InvocationStatus.SUCCEEDED, InvocationStatus.FAILED, InvocationStatus.DENIED, InvocationStatus.INTERRUPTED}
)


class ApprovalKind(StrEnum):
    TOOL = "TOOL"  # a risky tool call waits for approve/deny
    HUMAN_INPUT = "HUMAN_INPUT"  # GPT asked the user a question


class ApprovalStatus(StrEnum):
    PENDING = "PENDING"
    APPROVED = "APPROVED"
    DENIED = "DENIED"
    CANCELLED = "CANCELLED"


class Base(DeclarativeBase):
    pass


class Project(Base):
    __tablename__ = "projects"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_id)
    name: Mapped[str] = mapped_column(String(200))
    requirement_text: Mapped[str] = mapped_column(Text)
    stack: Mapped[str | None] = mapped_column(String(64), default=None)
    workspace_path: Mapped[str] = mapped_column(String(1024))
    status: Mapped[ProjectStatus] = mapped_column(
        Enum(ProjectStatus, native_enum=False, length=16), default=ProjectStatus.ACTIVE
    )
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)

    runs: Mapped[list[Run]] = relationship(back_populates="project", order_by="Run.created_at")


class Run(Base):
    __tablename__ = "runs"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_id)
    project_id: Mapped[str] = mapped_column(ForeignKey("projects.id"), index=True)
    state: Mapped[RunState] = mapped_column(
        Enum(RunState, native_enum=False, length=32), default=RunState.CREATED
    )
    resume_state: Mapped[RunState | None] = mapped_column(
        Enum(RunState, native_enum=False, length=32), default=None
    )
    last_response_id: Mapped[str | None] = mapped_column(String(128), default=None)
    turns: Mapped[int] = mapped_column(Integer, default=0)
    tokens_in: Mapped[int] = mapped_column(Integer, default=0)
    tokens_out: Mapped[int] = mapped_column(Integer, default=0)
    cost_usd: Mapped[float] = mapped_column(Float, default=0.0)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)
    started_at: Mapped[datetime | None] = mapped_column(UTCDateTime, default=None)
    ended_at: Mapped[datetime | None] = mapped_column(UTCDateTime, default=None)
    outcome: Mapped[str | None] = mapped_column(String(64), default=None)

    # --- Phase 2: orchestration ------------------------------------------------
    tool_calls: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    nudges: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    active_seconds: Mapped[float] = mapped_column(Float, default=0.0, server_default="0")
    cost_known: Mapped[bool] = mapped_column(Boolean, default=True, server_default="1")
    # Why a non-terminal run is not progressing (e.g. waiting for a later phase, LLM error).
    pause_reason: Mapped[str | None] = mapped_column(String(256), default=None)
    status_detail: Mapped[str | None] = mapped_column(Text, default=None)
    analysis_json: Mapped[str | None] = mapped_column(Text, default=None)
    plan_json: Mapped[str | None] = mapped_column(Text, default=None)
    # User messages (e.g. reminders) to send with the next GPT request.
    pending_messages_json: Mapped[str | None] = mapped_column(Text, default=None)

    # --- Phase 4 slice: verification, repair and the final report ------------------
    repairs: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    verification_json: Mapped[str | None] = mapped_column(Text, default=None)  # the last test run, judged
    report_json: Mapped[str | None] = mapped_column(Text, default=None)  # written when the run ends

    project: Mapped[Project] = relationship(back_populates="runs")

    @property
    def is_terminal(self) -> bool:
        return self.state in TERMINAL_RUN_STATES


class Requirement(Base):
    __tablename__ = "requirements"
    __table_args__ = (UniqueConstraint("run_id", "code", name="uq_requirements_run_code"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    run_id: Mapped[str] = mapped_column(ForeignKey("runs.id"), index=True)
    code: Mapped[str] = mapped_column(String(16))
    text: Mapped[str] = mapped_column(Text)
    kind: Mapped[str] = mapped_column(String(16))


class PlanTask(Base):
    __tablename__ = "tasks"
    __table_args__ = (UniqueConstraint("run_id", "order", name="uq_tasks_run_order"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    run_id: Mapped[str] = mapped_column(ForeignKey("runs.id"), index=True)
    order: Mapped[int] = mapped_column(Integer)
    title: Mapped[str] = mapped_column(String(200))
    description: Mapped[str] = mapped_column(Text)
    capability: Mapped[str] = mapped_column(String(32))
    features_json: Mapped[str] = mapped_column(Text)
    requirement_refs_json: Mapped[str] = mapped_column(Text)
    status: Mapped[str] = mapped_column(String(16), default="PENDING")


class AcceptanceCriterion(Base):
    __tablename__ = "acceptance_criteria"
    __table_args__ = (UniqueConstraint("run_id", "code", name="uq_criteria_run_code"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    run_id: Mapped[str] = mapped_column(ForeignKey("runs.id"), index=True)
    code: Mapped[str] = mapped_column(String(16))
    given: Mapped[str] = mapped_column(Text)
    when: Mapped[str] = mapped_column(Text)
    then: Mapped[str] = mapped_column(Text)
    priority: Mapped[str] = mapped_column(String(8))
    method: Mapped[str] = mapped_column(String(16))
    requirement_refs_json: Mapped[str] = mapped_column(Text)
    status: Mapped[str] = mapped_column(String(16), default="DEFINED")
    test_refs_json: Mapped[str] = mapped_column(Text, default="[]")
    frozen_hash: Mapped[str | None] = mapped_column(String(64), default=None)


class ToolInvocation(Base):
    """One GPT function call. `(run_id, call_id)` is unique, so a call never runs twice."""

    __tablename__ = "tool_invocations"
    __table_args__ = (
        UniqueConstraint("run_id", "call_id", name="uq_invocations_run_call"),
        UniqueConstraint("run_id", "seq", name="uq_invocations_run_seq"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    run_id: Mapped[str] = mapped_column(ForeignKey("runs.id"), index=True)
    seq: Mapped[int] = mapped_column(Integer)
    call_id: Mapped[str] = mapped_column(String(128))
    response_id: Mapped[str | None] = mapped_column(String(128), default=None)
    tool: Mapped[str] = mapped_column(String(64))
    arguments_json: Mapped[str] = mapped_column(Text)  # redacted before storage
    fingerprint: Mapped[str] = mapped_column(String(64))  # sha256 of tool + canonical arguments
    state_at_call: Mapped[str] = mapped_column(String(32))
    status: Mapped[InvocationStatus] = mapped_column(
        Enum(InvocationStatus, native_enum=False, length=24), default=InvocationStatus.QUEUED
    )
    ok: Mapped[bool | None] = mapped_column(Boolean, default=None)
    error_class: Mapped[str | None] = mapped_column(String(32), default=None)
    output_json: Mapped[str | None] = mapped_column(Text, default=None)  # exactly what GPT receives
    delivered: Mapped[bool] = mapped_column(Boolean, default=False, server_default="0")
    latency_ms: Mapped[int | None] = mapped_column(Integer, default=None)
    capability: Mapped[str | None] = mapped_column(String(32), default=None)
    provider: Mapped[str | None] = mapped_column(String(64), default=None)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)
    finished_at: Mapped[datetime | None] = mapped_column(UTCDateTime, default=None)


class Approval(Base):
    __tablename__ = "approvals"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_id)
    run_id: Mapped[str] = mapped_column(ForeignKey("runs.id"), index=True)
    invocation_id: Mapped[int] = mapped_column(ForeignKey("tool_invocations.id"), unique=True)
    kind: Mapped[ApprovalKind] = mapped_column(Enum(ApprovalKind, native_enum=False, length=16))
    tool: Mapped[str] = mapped_column(String(64))
    risk: Mapped[str] = mapped_column(String(32))
    payload_json: Mapped[str] = mapped_column(Text)  # redacted arguments or the question
    status: Mapped[ApprovalStatus] = mapped_column(
        Enum(ApprovalStatus, native_enum=False, length=16), default=ApprovalStatus.PENDING
    )
    response_text: Mapped[str | None] = mapped_column(Text, default=None)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)
    decided_at: Mapped[datetime | None] = mapped_column(UTCDateTime, default=None)


class CodingAttempt(Base):
    """One coding attempt's safety lifecycle (app/coding/session_control.py). `(run_id, call_id)` is
    unique, so a call never sends a second prompt. Saved before each step, so a crash can be finished."""

    __tablename__ = "coding_attempts"
    __table_args__ = (UniqueConstraint("run_id", "call_id", name="uq_coding_attempts_run_call"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    project_id: Mapped[str] = mapped_column(ForeignKey("projects.id"), index=True)
    run_id: Mapped[str] = mapped_column(ForeignKey("runs.id"), index=True)
    call_id: Mapped[str] = mapped_column(String(128))
    task_key: Mapped[str] = mapped_column(String(128))
    phase: Mapped[AttemptPhase] = mapped_column(Enum(AttemptPhase, native_enum=False, length=24), index=True)
    session_id: Mapped[str | None] = mapped_column(String(128), default=None)
    message_id: Mapped[str | None] = mapped_column(String(64), default=None)
    container_id: Mapped[str | None] = mapped_column(String(64), default=None)
    known_messages_json: Mapped[str] = mapped_column(Text, default="[]")
    reconciliation: Mapped[str | None] = mapped_column(String(16), default=None)
    failure_reason: Mapped[str | None] = mapped_column(Text, default=None)  # redacted before storage
    abort_acknowledged: Mapped[bool] = mapped_column(Boolean, default=False)
    observed_quiet: Mapped[bool] = mapped_column(Boolean, default=False)
    process_terminated: Mapped[bool] = mapped_column(Boolean, default=False)
    workspace_stable: Mapped[bool] = mapped_column(Boolean, default=False)
    restore_verified: Mapped[bool] = mapped_column(Boolean, default=False)
    late_results: Mapped[int] = mapped_column(Integer, default=0)
    detail: Mapped[str | None] = mapped_column(Text, default=None)  # redacted before storage
    stop_evidence: Mapped[str | None] = mapped_column(Text, default=None)  # redacted before storage
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow, onupdate=utcnow)


class Event(Base):
    """Append-only execution log. `seq` is monotonic per project."""

    __tablename__ = "events"
    __table_args__ = (UniqueConstraint("project_id", "seq", name="uq_events_project_seq"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    project_id: Mapped[str] = mapped_column(ForeignKey("projects.id"), index=True)
    run_id: Mapped[str | None] = mapped_column(ForeignKey("runs.id"), index=True, default=None)
    seq: Mapped[int] = mapped_column(Integer)
    type: Mapped[str] = mapped_column(String(64))
    payload_json: Mapped[str] = mapped_column(Text)
    ts: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)
