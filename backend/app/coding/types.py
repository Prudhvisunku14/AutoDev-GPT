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
from typing import Any, Protocol

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


@dataclass(frozen=True)
class CodingTask:
    key: str  # "<run_id>:<task_order>", one per plan task
    title: str
    brief: str  # built by Python from the saved plan (app/coding/brief.py)
    session_id: str | None  # an earlier session for the same task, continued when it still exists
    abort_first: bool  # the earlier attempt was interrupted: stop that session before prompting
    max_tokens: int
    timeout_seconds: float


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


class CodingProvider(Protocol):
    name: str

    def run_task(self, task: CodingTask, *, cancel: threading.Event | None, progress: Progress) -> CodingOutcome:
        """Run one task. Expected failures come back as a status; only bugs raise."""
        ...
