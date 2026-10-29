"""SQLAlchemy 2.0 typed ORM models (ARCHITECTURE §10).

Phase 1 creates `projects`, `runs` and `events`. The other tables arrive with the
phases that use them.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from enum import StrEnum

from sqlalchemy import DateTime, Enum, Float, ForeignKey, Integer, String, Text, UniqueConstraint
from sqlalchemy.engine import Dialect
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship
from sqlalchemy.types import TypeDecorator


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

    project: Mapped[Project] = relationship(back_populates="runs")

    @property
    def is_terminal(self) -> bool:
        return self.state in TERMINAL_RUN_STATES


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
