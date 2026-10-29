"""FakeSandbox: a scripted SandboxRunner for unit tests. It never starts a process."""

from __future__ import annotations

import threading
from collections.abc import Callable

from app.sandbox.docker_cli import SandboxUnavailable
from app.sandbox.spec import ExecResult, ExecSpec, ExecStatus


class FakeSandbox:
    def __init__(
        self,
        respond: Callable[[ExecSpec], ExecResult] | None = None,
        *,
        unavailable: bool = False,
    ) -> None:
        self._respond = respond
        self._unavailable = unavailable
        self.specs: list[ExecSpec] = []

    def run(self, spec: ExecSpec, *, cancel: threading.Event | None = None) -> ExecResult:
        self.specs.append(spec)
        if self._unavailable:
            raise SandboxUnavailable("Docker is not reachable (fake)")
        if self._respond is not None:
            return self._respond(spec)
        return ExecResult(
            status=ExecStatus.SUCCEEDED,
            command=spec.command,
            container=spec.container_name,
            exit_code=0,
            duration_ms=1,
            stdout="ok\n",
            cleanup="removed",
        )

    def sweep_orphans(self) -> int:
        return 0
