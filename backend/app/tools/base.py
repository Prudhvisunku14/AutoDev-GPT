"""Shared tool types: risk levels, the ToolResult contract (ARCHITECTURE §5.3), tool specs."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from enum import StrEnum
from typing import TYPE_CHECKING, Any

from pydantic import BaseModel, ConfigDict, Field

from app.db.models import RunState

if TYPE_CHECKING:
    import threading

    from sqlalchemy.orm import Session

    from app.coding.types import CodingProvider
    from app.config import Settings
    from app.core.events import EventRecorder
    from app.db.models import Project, Run
    from app.sandbox.runner import SandboxRunner
    from app.stacks.catalog import StackCatalog
    from app.tools.adapters.workspace_fs import WorkspaceFS


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
    # Phase 3: the Docker sandbox, the workspace jail, and a signal set when the backend stops.
    sandbox: SandboxRunner | None = None
    workspace: WorkspaceFS | None = None
    cancel: threading.Event | None = None
    # Phase 4: the coding engine.
    coding: CodingProvider | None = None
    # The jail holding each run's disposable copy. Sandbox commands run there once a run has one.
    candidates: WorkspaceFS | None = None
    # The GPT function call being executed. Tools key their side effects (such as a workspace
    # checkpoint) by it, so the `recover` hook, which receives the same call_id, can find them.
    call_id: str | None = None


Handler = Callable[[ToolContext, Any], dict[str, Any]]
# Records one progress event in its own short transaction: (event type, payload).
Progress = Callable[[str, dict[str, Any]], None]


@dataclass(frozen=True)
class RecoveryInfo:
    """What a tool's `recover` hook gets when the backend died while the tool was running."""

    settings: Settings
    run_id: str
    project_id: str
    call_id: str


@dataclass(frozen=True)
class ExternalStep:
    """A tool whose slow part (Docker, OpenCode) runs with no database transaction open.

    `begin` validates and records the start inside a transaction and returns a plan.
    `run` does the slow work with no transaction, so the API can still write meanwhile.
    `finish` records the outcome inside a new transaction and returns the tool's data.
    `recover` repairs side effects (such as workspace edits) left by a call that never finished.
    """

    begin: Callable[[ToolContext, Any], Any]
    run: Callable[[Any, threading.Event | None, Progress], Any]
    finish: Callable[[ToolContext, Any, Any], dict[str, Any]]
    recover: Callable[[RecoveryInfo], str | None] | None = None


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
    # Set for tools that call slow external systems; `handler` is then None.
    external: ExternalStep | None = None
