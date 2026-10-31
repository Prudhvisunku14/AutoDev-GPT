"""Ending the isolated coding container, and proving it ended (ARCHITECTURE_OPTIMIZATION A2).

Restoring a workspace is safe only once no writer can be alive, so "stopped" must come from an
independent read, never from the stop request. `docker rm -f` exits 0 for a container that does not
exist, so its exit code proves nothing: this class asks Docker again, separately, by the container's
full ID, because a name can be reused. Only a read that says the container is gone or has no process
confirms. A daemon that cannot be reached, a timeout, an odd answer or a different container all leave
termination unconfirmed, and session_control.py then quarantines the workspace.
"""

from __future__ import annotations

import time
from collections.abc import Callable

from app.coding.types import Termination
from app.sandbox.docker_cli import FULL_ID, ContainerPresence, DockerCli, Inspection

STOP_ATTEMPTS = 5
POLL_SECONDS = 1.0
_NO_PROCESS = (ContainerPresence.NOT_FOUND, ContainerPresence.STOPPED)


class ContainerTerminator:
    """The session_control.WriterControl for one container, known by its full ID."""

    def __init__(
        self,
        cli: DockerCli,
        container_id: str,
        *,
        attempts: int = STOP_ATTEMPTS,
        poll_seconds: float = POLL_SECONDS,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self._cli = cli
        self._id = container_id
        self._attempts = attempts
        self._poll = poll_seconds
        self._sleep = sleep

    def terminate(self) -> Termination:
        if not FULL_ID.fullmatch(self._id or ""):
            return Termination(False, "no full container ID: nothing can be confirmed by a name alone")
        steps: list[str] = []
        before = self._read(steps, "before")
        if before.presence is ContainerPresence.NOT_FOUND:
            return Termination(True, _joined(steps))
        for attempt in range(self._attempts):
            if attempt:
                self._sleep(self._poll)
            _, said = self._cli.remove_detail(self._id)
            steps.append(f"stop requested: {said[:160]}")
            seen = self._read(steps, "after")
            if seen.presence in _NO_PROCESS:
                return Termination(True, _joined(steps))
        return Termination(False, _joined(steps))

    def _read(self, steps: list[str], label: str) -> Inspection:
        seen = self._cli.inspect_container(self._id, expected_id=self._id)
        steps.append(f"{label}: {seen.presence} ({seen.detail[:160]})")
        return seen


def _joined(steps: list[str]) -> str:
    return "; ".join(steps)[-1900:]
