"""Container job and result types, shared by the policy, the Docker adapter and the tool."""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import Any

# Where the workspace appears inside every container. Read-only unless a command says otherwise.
WORKSPACE_TARGET = "/workspace"


class ExecStatus(StrEnum):
    SUCCEEDED = "SUCCEEDED"  # exited with code 0
    FAILED = "FAILED"  # exited with a non-zero code
    TIMEOUT = "TIMEOUT"  # wall-clock limit reached; the container was removed
    OUTPUT_LIMIT = "OUTPUT_LIMIT"  # printed far more than allowed; the container was removed
    MEMORY_LIMIT = "MEMORY_LIMIT"  # the kernel killed the container for exceeding its memory limit
    CANCELLED = "CANCELLED"  # the backend stopped; the container was removed
    ERROR = "ERROR"  # the container could not be created, or did not run
    UNAVAILABLE = "UNAVAILABLE"  # Docker is not installed, not running or not reachable


@dataclass(frozen=True)
class ExecSpec:
    """One approved execution. Built only by `sandbox.policy.build_spec`."""

    command: str  # approved command name
    image: str  # image reference, exactly as approved
    argv: tuple[str, ...]  # passed to the container as an argument list; never through a shell
    workspace_dir: Path  # host directory mounted at WORKSPACE_TARGET
    timeout_seconds: float
    container_name: str


@dataclass(frozen=True)
class ExecResult:
    status: ExecStatus
    command: str
    container: str
    exit_code: int | None = None
    duration_ms: int = 0
    stdout: str = ""
    stderr: str = ""
    stdout_truncated: bool = False
    stderr_truncated: bool = False
    oom_killed: bool = False
    cleanup: str = "not_needed"  # "removed", "failed" or "not_needed"
    detail: str | None = None  # plain-language reason for ERROR, UNAVAILABLE or CANCELLED

    @classmethod
    def unavailable(cls, spec: ExecSpec, detail: str) -> ExecResult:
        return cls(status=ExecStatus.UNAVAILABLE, command=spec.command, container=spec.container_name, detail=detail)

    def to_dict(self) -> dict[str, Any]:
        return {
            "status": self.status.value,
            "command": self.command,
            "container": self.container,
            "exit_code": self.exit_code,
            "duration_ms": self.duration_ms,
            "stdout": self.stdout,
            "stderr": self.stderr,
            "stdout_truncated": self.stdout_truncated,
            "stderr_truncated": self.stderr_truncated,
            "oom_killed": self.oom_killed,
            "cleanup": self.cleanup,
            "detail": self.detail,
        }
