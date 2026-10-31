"""A simulated coding provider for offline runs. **It is not OpenCode and it uses no model.**

It writes files from a fixed script into the run's disposable copy, so everything around it can be
exercised for real: the attempt lifecycle, the audit, the checkpoint, the Docker tests, the verdict and
the recovery. A result produced with it shows the workflow works, not that any model can write code.

It goes through `AttemptGuard` exactly as a real provider does: the prompt is "sent" through
`guard.submit`, and a failure stops through `guard.secure`. There is no container, so its stop is
confirmed at once, and that is the one safety step it cannot exercise.
"""

from __future__ import annotations

import threading
from collections.abc import Iterable
from dataclasses import dataclass, field
from typing import Any

from app.coding.launcher import LaunchRefused, LaunchRequest
from app.coding.session_control import AttemptGuard, SessionControl, SubmitVerdict, WriterControl
from app.coding.types import CodingOutcome, CodingStatus, CodingTask, Progress, Termination
from app.tools.adapters.workspace_fs import check_relative_path

PROVIDER_NAME = "simulated"
_SESSION = "ses_simulated"


@dataclass(frozen=True)
class ScriptedAttempt:
    """What one coding attempt does. Files are written even when the attempt then fails."""

    files: dict[str, str] = field(default_factory=dict)
    status: CodingStatus = CodingStatus.COMPLETED
    detail: str | None = None


class _Control:
    """A session that accepts one prompt and is never busy."""

    def __init__(self) -> None:
        self.prompts: list[dict[str, Any]] = []

    def submit(self, session_id: str, body: dict[str, Any]) -> int | None:
        self.prompts.append(body)
        return 204

    def statuses(self) -> dict[str, Any]:
        return {}

    def messages(self, session_id: str) -> list[Any]:
        return []

    def abort(self, session_id: str) -> None:
        return None


class _NoContainer:
    def terminate(self) -> Termination:
        return Termination(True, "simulated: no container exists")


class SimulatedProvider:
    name = PROVIDER_NAME

    def __init__(self, attempt: ScriptedAttempt, control: _Control) -> None:
        self._attempt = attempt
        self._control = control

    def run_task(self, task: CodingTask, *, cancel: threading.Event | None, progress: Progress, guard: AttemptGuard) -> CodingOutcome:
        assert task.workspace is not None, "the simulated provider writes into the disposable copy"
        verdict = guard.submit(self._control, _SESSION, {"parts": [{"type": "text", "text": task.brief}]}, use_message_id=True, cancel=cancel)
        if verdict is not SubmitVerdict.ACCEPTED:
            return CodingOutcome(status=CodingStatus.CANCELLED, detail=guard.record.failure_reason, stop=guard.report())
        progress("coding.prompt_sent", {"session_id": _SESSION, "brief_chars": len(task.brief), "simulated": True})
        for rel, content in self._attempt.files.items():
            check_relative_path(rel)
            target = task.workspace / rel
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(content, encoding="utf-8", newline="\n")
        outcome = CodingOutcome(status=self._attempt.status, detail=self._attempt.detail, session_id=_SESSION, files_reported=sorted(self._attempt.files))
        if self._attempt.status is CodingStatus.COMPLETED:
            guard.mark_finished()
        else:
            guard.secure(self._control, self._attempt.detail or f"the simulated attempt ended {self._attempt.status}")
        outcome.stop = guard.report()
        return outcome


@dataclass
class _Session:
    provider: SimulatedProvider
    control: SessionControl
    writer: WriterControl
    container_id: str | None = None
    usage: dict[str, Any] = field(default_factory=dict)

    def close(self) -> None:
        return None


class SimulatedLauncher:
    """Hands out the scripted attempts in order. Running out of script refuses the launch."""

    name = PROVIDER_NAME

    def __init__(self, script: Iterable[ScriptedAttempt]) -> None:
        self._script = list(script)
        self.briefs: list[str] = []

    def unavailable_reason(self) -> str | None:
        return None

    def start(self, request: LaunchRequest) -> _Session:
        if not self._script:
            raise LaunchRefused("the simulated script has no more attempts")
        control = _Control()
        return _Session(provider=SimulatedProvider(self._script.pop(0), control), control=control, writer=_NoContainer())

    def writer_for(self, container_id: str | None) -> WriterControl:
        return _NoContainer()

    def sweep(self) -> None:
        return None
