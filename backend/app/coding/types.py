"""The coding-engine boundary (ARCHITECTURE §6): what DevPilot asks for and what comes back.

A CodingProvider turns one task brief into file edits in the project workspace. It never
decides whether the edits are kept: the generate_code tool audits the workspace itself
and reverts the task on any violation (app/coding/audit.py).
"""

from __future__ import annotations

import threading
from collections.abc import Callable
from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path
from typing import TYPE_CHECKING, Any, Protocol

if TYPE_CHECKING:
    from app.coding.session_control import AttemptGuard

# (event type, payload): records one progress event in its own short transaction.
Progress = Callable[[str, dict[str, Any]], None]


class CodingStatus(StrEnum):
    COMPLETED = "COMPLETED"  # the engine finished its turn without an error
    FAILED = "FAILED"  # the engine reported an error (model, provider or tool failure)
    TIMEOUT = "TIMEOUT"  # OPENCODE_TIMEOUT_SECONDS passed; the session was aborted
    CANCELLED = "CANCELLED"  # the run was cancelled or the backend stopped; the session was aborted
    LIMIT = "LIMIT"  # the task's token budget was used up; the session was aborted
    POLICY_VIOLATION = "POLICY_VIOLATION"  # the engine did something its policy forbids; the session was aborted
    BUSY = "BUSY"  # the session is already working on something; nothing was sent
    UNAVAILABLE = "UNAVAILABLE"  # the engine is not reachable or not healthy
    AUTH = "AUTH"  # the engine refused DevPilot's credentials
    INCOMPATIBLE = "INCOMPATIBLE"  # wrong version, missing API operations, or an unsafe effective configuration
    PROTOCOL = "PROTOCOL"  # the engine answered with something DevPilot cannot interpret


class AttemptPhase(StrEnum):
    """One coding attempt's safety lifecycle (session_control.py). Saved before each step that matters."""

    PREPARED = "PREPARED"  # claimed; no prompt has left DevPilot
    ABANDONED = "ABANDONED"  # ended before a prompt was sent; nothing to stop
    SUBMITTED = "SUBMITTED"  # saved just before the send: from here a crash counts as ambiguous
    RUNNING = "RUNNING"  # the server accepted the prompt
    AMBIGUOUS = "AMBIGUOUS"  # the prompt may or may not have been accepted
    REJECTED = "REJECTED"  # confirmed refused and nothing running; nothing to stop
    FINISHED = "FINISHED"  # the engine finished by itself; the workspace still has to be audited
    KEPT = "KEPT"  # finished and accepted: the attempt is over
    ABORT_REQUESTED = "ABORT_REQUESTED"  # stopping: the abort is being sent
    QUIESCENCE_CHECK = "QUIESCENCE_CHECK"  # stopping: watching the session, then ending the container
    SAFE_TO_RESTORE = "SAFE_TO_RESTORE"  # termination confirmed and the workspace is still
    QUARANTINED = "QUARANTINED"  # a writer may still be active: never restored automatically
    RESTORED = "RESTORED"  # restored and compared with the checkpoint


@dataclass(frozen=True)
class Termination:
    """The answer to "is the coding container gone?". `confirmed` only from an independent read."""

    confirmed: bool
    evidence: str = ""  # what each step saw, kept with the attempt; safe to log


@dataclass(frozen=True)
class StopReport:
    """What is known about a stopped attempt. The three middle facts are never interchangeable."""

    phase: AttemptPhase
    abort_acknowledged: bool = False  # logical cancellation: the server said yes; writes may continue
    observed_quiet: bool = False  # session and workspace looked still; an observation, not proof
    process_terminated: bool = False  # confirmed termination: docker reports the container gone
    workspace_stable: bool = False  # unchanged after termination
    restore_verified: bool = False  # verified restoration: the workspace equals the checkpoint
    detail: str = ""
    stop_evidence: str = ""  # what the stop saw, step by step

    @property
    def safe_to_restore(self) -> bool:
        return self.phase is AttemptPhase.SAFE_TO_RESTORE

    @property
    def quarantined(self) -> bool:
        return self.phase is AttemptPhase.QUARANTINED


@dataclass(frozen=True)
class CodingTask:
    key: str  # "<run_id>:<task_order>", one per plan task
    title: str
    brief: str  # built by Python from the saved plan (app/coding/brief.py)
    session_id: str | None  # an earlier session for the same task, continued when it still exists
    abort_first: bool  # the earlier attempt was interrupted: stop that session before prompting
    max_tokens: int
    timeout_seconds: float
    workspace: Path | None = None  # the disposable copy on the host; a provider in a container sees its own mount


@dataclass
class CodingOutcome:
    status: CodingStatus
    detail: str | None = None
    session_id: str | None = None
    engine_version: str | None = None
    tokens_in: int = 0
    tokens_out: int = 0
    cost_usd: float | None = None
    tools_used: list[str] = field(default_factory=list)
    files_reported: list[str] = field(default_factory=list)  # what the engine said it edited; audited separately
    permission_rejections: list[str] = field(default_factory=list)
    reconnects: int = 0
    stop: StopReport | None = None  # how the attempt ended; the caller restores only through its guard


class CodingProvider(Protocol):
    name: str

    def run_task(
        self, task: CodingTask, *, cancel: threading.Event | None, progress: Progress, guard: AttemptGuard
    ) -> CodingOutcome:
        """Run one task. Expected failures come back as a status; only bugs raise.

        Every prompt goes through `guard`, and every way of stopping uses `guard.secure`. The caller
        keeps the guard: it decides, after the audit, whether to keep the edits or restore them."""
        ...
