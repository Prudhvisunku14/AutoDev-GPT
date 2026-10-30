"""In-memory stand-ins for the coding engine, its container and the attempt store.

No HTTP, no Docker, no database. `Scene` ties them together around one shared `log` of what
happened and in what order, and a fake clock that only moves when the code under test waits. Each
wait is also when a writer that is still running changes the workspace.
"""

from __future__ import annotations

import copy
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from app.coding.session_control import (
    BLOCKING_PHASES,
    UNRESOLVED_PHASES,
    AttemptGuard,
    AttemptRecord,
    AttemptRefused,
    ControlError,
    StopPolicy,
)
from app.coding.types import AttemptPhase

SESSION = "ses_1"


@dataclass
class FakeEngine:
    """One session on an imaginary server: what it answers, and whether it is really working."""

    log: list[str]
    submit_status: int | None = 204
    delivered: bool = True  # whether the prompt reached the server, whatever the answer said
    start_delay: int = 0  # waits between delivery and the session starting to work
    abort_stops: bool = True  # False: the abort is acknowledged and the work carries on
    abort_failures: int = 0
    unreadable: bool = False
    state: str | None = None  # what /session/status lists for the session; None means not listed
    entries: list[dict[str, Any]] = field(default_factory=list)
    prompts: list[dict[str, Any]] = field(default_factory=list)
    aborts: int = 0
    working: bool = False  # a real writer exists right now
    _starts_in: int | None = None

    def submit(self, session_id: str, body: dict[str, Any]) -> int | None:
        self.log.append("submit")
        self.prompts.append(body)
        if self.delivered:
            self._starts_in = self.start_delay
            if self.start_delay == 0:
                self._start()
        return self.submit_status

    def statuses(self) -> Mapping[str, Any]:
        self._read()
        return {} if self.state is None else {SESSION: {"type": self.state}}

    def messages(self, session_id: str) -> Sequence[Any]:
        self._read()
        return copy.deepcopy(self.entries)

    def abort(self, session_id: str) -> None:
        self.log.append("abort")
        self.aborts += 1
        if self.abort_failures > 0:
            self.abort_failures -= 1
            raise ControlError("the abort got no answer")
        if self.abort_stops and self.working:
            self.finish_work(error=True)

    def advance(self) -> None:
        """One wait passes. A delayed prompt may start."""
        if self._starts_in is not None and self._starts_in > 0:
            self._starts_in -= 1
            if self._starts_in == 0:
                self._start()

    def finish_work(self, *, error: bool = False) -> None:
        self.working = False
        self.state = None
        info = self.entries[-1]["info"]
        info["time"] = {"completed": 2}
        if error:
            info["error"] = {"name": "MessageAbortedError"}

    def killed(self) -> None:
        self.working = False
        self._starts_in = None

    def _start(self) -> None:
        self._starts_in = None
        self.working = True
        self.state = "busy"
        user_id = self.prompts[-1].get("messageID", "msg_server_chosen")
        self.entries.append({"info": {"id": user_id, "role": "user", "time": {"created": 1}}, "parts": [{"type": "text"}]})
        self.entries.append({"info": {"id": f"{user_id}_reply", "role": "assistant", "time": {"created": 1}}, "parts": []})

    def _read(self) -> None:
        if self.unreadable:
            raise ControlError("the server did not answer")


@dataclass
class FakeWorkspace:
    """Changes on every wait while the engine is working, or when told to keep changing."""

    engine: FakeEngine
    version: int = 0
    keeps_changing: bool = False
    unreadable: bool = False

    def fingerprint(self) -> str | None:
        return None if self.unreadable else f"v{self.version}"

    def tick(self) -> None:
        if self.engine.working or self.keeps_changing:
            self.version += 1


@dataclass
class FakeWriter:
    """The launcher's stop. `confirms` False: docker could not confirm the container is gone."""

    log: list[str]
    engine: FakeEngine
    confirms: bool = True
    really_stops: bool = True  # False: it says gone, and the writer goes on (a broken promise)
    raises: bool = False
    calls: int = 0

    def terminate(self) -> bool:
        self.log.append("terminate")
        self.calls += 1
        if self.raises:
            raise RuntimeError("docker failed")
        if self.confirms and self.really_stops:
            self.engine.killed()
        return self.confirms


@dataclass
class FakeCheckpoint:
    log: list[str]
    differs: list[str] = field(default_factory=list)
    fails: bool = False

    def restore(self, root: Path) -> int:
        self.log.append("restore")
        if self.fails:
            raise OSError("disk error")
        return 3

    def differences(self, root: Path) -> list[str]:
        self.log.append("compare")
        return list(self.differs)


class StoreCrash(Exception):
    """The backend stopped: this save and everything after it did not happen."""


@dataclass
class MemoryStore:
    """A store that keeps copies, as a database would. `crash_after` makes a later save fail."""

    rows: dict[tuple[str, str], AttemptRecord] = field(default_factory=dict)
    saves: int = 0
    crash_after: int | None = None

    def claim(self, record: AttemptRecord) -> None:
        if (record.run_id, record.call_id) in self.rows:
            raise AttemptRefused("this call already ran a coding attempt")
        if any(r.project_id == record.project_id and r.phase in BLOCKING_PHASES for r in self.rows.values()):
            raise AttemptRefused("the project is held")
        self.rows[(record.run_id, record.call_id)] = copy.deepcopy(record)

    def save(self, record: AttemptRecord) -> None:
        if self.crash_after is not None and self.saves >= self.crash_after:
            raise StoreCrash()
        self.saves += 1
        self.rows[(record.run_id, record.call_id)] = copy.deepcopy(record)

    def unresolved(self) -> list[AttemptRecord]:
        return [copy.deepcopy(r) for r in self.rows.values() if r.phase in UNRESOLVED_PHASES]

    def only(self) -> AttemptRecord:
        assert len(self.rows) == 1
        return copy.deepcopy(next(iter(self.rows.values())))


class Scene:
    """One attempt with all its fakes, sharing a clock and a log."""

    def __init__(self, *, policy: StopPolicy | None = None, **engine: Any) -> None:
        self.log: list[str] = []
        self.engine = FakeEngine(self.log, **engine)
        self.workspace = FakeWorkspace(self.engine)
        self.writer = FakeWriter(self.log, self.engine)
        self.checkpoint = FakeCheckpoint(self.log)
        self.store = MemoryStore()
        self.policy = policy or StopPolicy(settle_timeout_seconds=5.0, poll_seconds=0.5, stable_polls=2)
        self.now = 0.0
        self.events: list[tuple[str, dict[str, Any]]] = []
        self.on_wait: list[Any] = []

    def clock(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.now += seconds
        self.workspace.tick()
        self.engine.advance()
        for action in self.on_wait:
            action()

    def deps(self) -> dict[str, Any]:
        return {
            "store": self.store, "writer": self.writer, "fingerprint": self.workspace.fingerprint, "policy": self.policy,
            "clock": self.clock, "sleep": self.sleep, "wall_ms": lambda: 1_793_347_447_981,
            "progress": lambda event, payload: self.events.append((event, payload)),
        }  # fmt: skip

    def guard(self, *, call_id: str = "call_1", run_id: str = "run_1", project_id: str = "proj_1") -> AttemptGuard:
        return AttemptGuard.open(project_id=project_id, run_id=run_id, call_id=call_id, task_key=f"{run_id}:1", **self.deps())

    def reopened(self, record: AttemptRecord | None = None) -> AttemptGuard:
        """The guard a restarted backend would build from what the store kept."""
        return AttemptGuard(record or self.store.only(), **self.deps())

    def phases(self) -> list[str]:
        return [str(p["to"]) for e, p in self.events if e == "coding.attempt_phase"]

    def submit(self, guard: AttemptGuard, *, use_message_id: bool = True, **kwargs: Any) -> Any:
        return guard.submit(self.engine, SESSION, {"parts": [{"type": "text", "text": "do it"}]}, use_message_id=use_message_id, **kwargs)


PHASES = tuple(AttemptPhase)
