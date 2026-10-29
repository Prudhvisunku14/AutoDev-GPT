"""Shared tool types: risk levels, the ToolResult contract (ARCHITECTURE §5.3), tool specs."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from enum import StrEnum
from typing import TYPE_CHECKING, Any

from pydantic import BaseModel, ConfigDict, Field

from app.db.models import RunState

if TYPE_CHECKING:
    from sqlalchemy.orm import Session

    from app.config import Settings
    from app.core.events import EventRecorder
    from app.db.models import Project, Run
    from app.stacks.catalog import StackCatalog


class RiskLevel(StrEnum):
    """SECURITY.md §3."""

    READ = "READ"
    WORKSPACE_WRITE = "WORKSPACE_WRITE"
    SANDBOX_EXEC = "SANDBOX_EXEC"
    EXTERNAL_NETWORK = "EXTERNAL_NETWORK"
    GIT_PUSH = "GIT_PUSH"
    DESTRUCTIVE = "DESTRUCTIVE"


class ErrorClass(StrEnum):
    """Executor-level error classes returned to GPT. The Phase 6 failure taxonomy is separate."""

    INVALID_ARGS = "INVALID_ARGS"  # not JSON, or fails the Pydantic model
    UNKNOWN_TOOL = "UNKNOWN_TOOL"
    POLICY_REFUSED = "POLICY_REFUSED"  # tool not allowed in this state, or refused by policy
    VALIDATION_FAILED = "VALIDATION_FAILED"  # a gate's code-level checks failed
    APPROVAL_DENIED = "APPROVAL_DENIED"
    HUMAN_DECLINED = "HUMAN_DECLINED"
    INTERRUPTED = "INTERRUPTED"  # the backend stopped while the tool was running
    TOOL_ERROR = "TOOL_ERROR"  # unexpected exception inside the tool


class ToolArgs(BaseModel):
    """Base for tool-argument models. Extra keys are refused, matching the strict schema."""

    model_config = ConfigDict(extra="forbid")


class ToolErrorInfo(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    error_class: str = Field(serialization_alias="class")
    summary: str
    details: list[str] | None = None


class ToolResult(BaseModel):
    """What GPT receives as `function_call_output` (ARCHITECTURE §5.3, Phase 2 subset)."""

    ok: bool
    tool: str
    state: str | None = None  # run state after the call
    data: dict[str, Any] | None = None
    error: ToolErrorInfo | None = None
    meta: dict[str, Any] = Field(default_factory=dict)

    @classmethod
    def failure(
        cls, tool: str, error_class: ErrorClass, summary: str, details: list[str] | None = None, *, state: str | None = None
    ) -> ToolResult:
        return cls(ok=False, tool=tool, state=state, error=ToolErrorInfo(error_class=error_class, summary=summary, details=details))


class ToolError(Exception):
    """Raised by a tool handler to return a structured failure. Its DB changes are rolled back."""

    def __init__(self, error_class: ErrorClass, summary: str, details: list[str] | None = None) -> None:
        super().__init__(summary)
        self.error_class = error_class
        self.summary = summary
        self.details = details


@dataclass
class ToolContext:
    session: Session
    run: Run
    project: Project
    settings: Settings
    events: EventRecorder
    catalog: StackCatalog


Handler = Callable[[ToolContext, Any], dict[str, Any]]


@dataclass(frozen=True)
class ToolSpec:
    name: str
    description: str
    args_model: type[ToolArgs]
    risk: RiskLevel
    # States in which GPT may call the tool; None means every non-terminal working state.
    states: frozenset[RunState] | None
    handler: Handler | None
    # True: the call pauses the run until a person answers (request_human_input).
    asks_human: bool = False
