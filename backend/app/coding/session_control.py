"""Stopping a coding attempt safely (ARCHITECTURE_OPTIMIZATION A1, A2).

The rule: a prompt is sent at most once, and the workspace is restored only after the process
that could write to it is confirmed gone. Three facts are recorded apart and never swapped:

- Logical cancellation: the server acknowledged an abort. Writes already in flight may land, and
  a prompt the server has not started yet would start after it.
- Confirmed termination: docker reports the isolated coding container gone.
- Verified restoration: the checkpoint was restored and the workspace compared with it.

The lifecycle (`_NEXT` is the complete list of moves):

    PREPARED -> SUBMITTED -> RUNNING or AMBIGUOUS -> ABORT_REQUESTED -> QUIESCENCE_CHECK
             -> SAFE_TO_RESTORE or QUARANTINED

Why observation is not enough. `/session/status` lists only working sessions, so a session that has
not started yet looks the same as one that has finished, and a still folder says nothing about a
writer that has not woken up. So the session and the workspace are watched, and what was seen is
recorded, but only confirmed termination permits a restore. Every stop takes that path, whatever
caused it: cancellation, timeout, a policy breach, a budget overrun or a backend crash. If
termination cannot be confirmed the attempt is QUARANTINED, and nothing leaves that state by itself.

Each phase is saved before the step it describes, so a crash at any point leaves a record that
`recover` can finish from. A crash during the send leaves SUBMITTED, which counts as ambiguous.

Tested with in-memory fakes: nothing here touches HTTP, Docker or the database.
"""

from __future__ import annotations

import hashlib
import os
import secrets
import threading
import time
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path
from typing import Any, Protocol

from app.coding.types import AttemptPhase, Progress, StopReport, Termination

Phase = AttemptPhase
# A workspace fingerprint, or None when it could not be read. None never counts as still.
Fingerprint = Callable[[], str | None]

# Every allowed move. A phase missing from the keys has no way out, QUARANTINED among them.
_NEXT: dict[AttemptPhase, frozenset[AttemptPhase]] = {
    Phase.PREPARED: frozenset({Phase.SUBMITTED, Phase.ABANDONED}),
    Phase.SUBMITTED: frozenset({Phase.RUNNING, Phase.AMBIGUOUS, Phase.REJECTED}),
    Phase.RUNNING: frozenset({Phase.FINISHED, Phase.ABORT_REQUESTED}),
    Phase.AMBIGUOUS: frozenset({Phase.ABORT_REQUESTED}),
    Phase.FINISHED: frozenset({Phase.KEPT, Phase.ABORT_REQUESTED}),
    Phase.ABORT_REQUESTED: frozenset({Phase.QUIESCENCE_CHECK}),
    Phase.QUIESCENCE_CHECK: frozenset({Phase.SAFE_TO_RESTORE, Phase.QUARANTINED}),
    Phase.SAFE_TO_RESTORE: frozenset({Phase.RESTORED, Phase.QUARANTINED}),
}
# Phases that hold a project: no new attempt may start while one of these exists.
BLOCKING_PHASES = frozenset(
    {
        Phase.PREPARED, Phase.SUBMITTED, Phase.RUNNING, Phase.AMBIGUOUS, Phase.FINISHED,
        Phase.ABORT_REQUESTED, Phase.QUIESCENCE_CHECK, Phase.SAFE_TO_RESTORE, Phase.QUARANTINED,
    }
)  # fmt: skip
# Phases a restarted backend must finish. QUARANTINED is not one: only a person ends it.
UNRESOLVED_PHASES = BLOCKING_PHASES - {Phase.QUARANTINED}
_STOPPED_PHASES = frozenset({Phase.SAFE_TO_RESTORE, Phase.QUARANTINED, Phase.RESTORED})
_NOTHING_TO_STOP = frozenset({Phase.ABANDONED, Phase.REJECTED})
_BUSY_STATES = frozenset({"busy", "retry"})
_BASE62 = "0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz"
_ACCEPTED_STATUS = 204  # what prompt_async answers (GET /doc); see classify_submission


class ControlError(Exception):
    """The server's state could not be read or changed. The message is safe to log."""


class LifecycleError(Exception):
    """A move the lifecycle does not allow: a bug in the caller. Nothing was changed."""


class AttemptRefused(Exception):
    """A new attempt may not start: the same call already ran, or the project is still held."""


class RestoreForbidden(Exception):
    """The workspace may not be restored. Nothing was restored."""


class SubmitVerdict(StrEnum):
    ACCEPTED = "ACCEPTED"  # the server answered 204
    REJECTED = "REJECTED"  # the server refused it, and nothing is running
    AMBIGUOUS = "AMBIGUOUS"  # it may have been accepted: the attempt must be stopped
    NOT_SENT = "NOT_SENT"  # nothing left DevPilot


class Reconciliation(StrEnum):
    STARTED = "STARTED"  # the exact message is in the session, or the session is working
    NOT_SEEN = "NOT_SEEN"  # not there yet. It may still arrive, so this is never "did not start"
    UNKNOWN = "UNKNOWN"  # the server's state could not be read


class SessionControl(Protocol):
    """What this module needs from the engine. The HTTP client implements it; tests use a fake."""

    def submit(self, session_id: str, body: dict[str, Any]) -> int | None:
        """Send one prompt. The HTTP status, or None when no answer arrived. Never retries."""
        ...

    def statuses(self) -> Mapping[str, Any]:
        """Working sessions by id. Raises ControlError."""
        ...

    def messages(self, session_id: str) -> Sequence[Any]:
        """The session's messages, each {"info": {...}, "parts": [...]}. Raises ControlError."""
        ...

    def abort(self, session_id: str) -> None:
        """Ask the server to stop the session. Raises ControlError."""
        ...


class WriterControl(Protocol):
    """The thing that can really stop the writer: the launcher, for the isolated coding container."""

    def terminate(self) -> Termination:
        """End the coding container and wait. `confirmed` only when an independent read shows no process."""
        ...


class Restorable(Protocol):
    """A checkpoint (app/coding/audit.py)."""

    def restore(self, root: Path) -> int: ...

    def differences(self, root: Path) -> list[str]: ...


@dataclass
class AttemptRecord:
    """Everything needed to finish or explain an attempt after a crash."""

    project_id: str
    run_id: str
    call_id: str
    task_key: str
    phase: AttemptPhase = Phase.PREPARED
    session_id: str | None = None
    message_id: str | None = None  # the exact message sent, when the server takes client ids
    container_id: str | None = None  # the full ID of the coding container: a name can be reused
    known_messages: list[str] = field(default_factory=list)  # ids in the session before the send
    reconciliation: str | None = None
    failure_reason: str | None = None  # the first reason given; later ones never replace it
    abort_acknowledged: bool = False
    observed_quiet: bool = False
    process_terminated: bool = False
    workspace_stable: bool = False
    restore_verified: bool = False
    late_results: int = 0  # results that arrived after the outcome was decided, and were ignored
    detail: str | None = None
    stop_evidence: str | None = None  # what the stop saw, kept even when it could not be confirmed


class AttemptStore(Protocol):
    def claim(self, record: AttemptRecord) -> None:
        """Save a new attempt, atomically refusing with AttemptRefused if the same (run, call) exists
        or any attempt of the project is in BLOCKING_PHASES."""
        ...

    def save(self, record: AttemptRecord) -> None:
        """Durably replace the attempt's saved state. Returns only after it is committed."""
        ...

    def unresolved(self) -> list[AttemptRecord]:
        """Every attempt in UNRESOLVED_PHASES."""
        ...


@dataclass(frozen=True)
class StopPolicy:
    settle_timeout_seconds: float = 30.0
    poll_seconds: float = 0.5
    stable_polls: int = 2  # consecutive identical readings
    abort_attempts: int = 3
    unreachable_polls: int = 3  # give up watching a server that stopped answering


@dataclass(frozen=True)
class Quiescence:
    """What was seen. Evidence for the record, never permission to restore."""

    quiet: bool
    polls: int
    waited_seconds: float
    last_state: str


def classify_submission(http_status: int | None) -> SubmitVerdict:
    """What one prompt_async answer means. Only a 204 means accepted: unknown paths on this server
    answer 200 with its web page (C13), so a 200 proves nothing. A 4xx is a refusal, except 408 and
    429, which can follow a request the server already began. Anything else may have been accepted."""
    if http_status == _ACCEPTED_STATUS:
        return SubmitVerdict.ACCEPTED
    if http_status is not None and 400 <= http_status < 500 and http_status not in (408, 429):
        return SubmitVerdict.REJECTED
    return SubmitVerdict.AMBIGUOUS


def new_message_id(now_ms: int) -> str:
    """A client-chosen id in the shape OpenCode 1.19.5 creates (read from the binary): 12 hex characters
    of time, then 14 random ones, so it sorts among the session's own messages as they do."""
    stamp = ((now_ms * 4096) + 1) & 0xFFFFFFFFFFFF
    return f"msg_{stamp:012x}{''.join(secrets.choice(_BASE62) for _ in range(14))}"


def message_ids(messages: Iterable[Any]) -> list[str]:
    infos = (m.get("info") for m in messages if isinstance(m, dict))
    return [i["id"] for i in infos if isinstance(i, dict) and isinstance(i.get("id"), str)]


def workspace_fingerprint(root: Path) -> str | None:
    """Paths, kinds, sizes and modification times of everything under `root`. None when it cannot be read.
    Links are recorded, never followed, and no file is opened."""
    digest = hashlib.sha256()

    def refuse(error: OSError) -> None:
        raise error

    try:
        for folder, dirs, files in os.walk(root, followlinks=False, onerror=refuse):
            dirs.sort()
            for name in sorted(dirs + files):
                path = os.path.join(folder, name)
                info = os.lstat(path)
                line = f"{os.path.relpath(path, root)}\0{info.st_mode}\0{info.st_size}\0{info.st_mtime_ns}\n"
                digest.update(line.encode("utf-8", "surrogateescape"))
    except OSError:
        return None
    return digest.hexdigest()


def _session_state(statuses: Mapping[str, Any], session_id: str) -> str:
    """idle, absent, busy, retry or the unknown type. Absent is not idle: it is also what a session
    looks like before a queued prompt starts."""
    entry = statuses.get(session_id)
    if entry is None:
        return "absent"
    return str(entry.get("type")) if isinstance(entry, dict) else "unknown"


def _settled(messages: Sequence[Any], known: Iterable[str]) -> bool:
    """False while the newest message is a new user prompt or an unfinished assistant turn."""
    entries = [m.get("info") for m in messages if isinstance(m, dict)]
    infos = [i for i in entries if isinstance(i, dict)]
    if not infos:
        return True
    last = infos[-1]
    if last.get("role") == "assistant":
        times = last.get("time") if isinstance(last.get("time"), dict) else {}
        return bool(times.get("completed") or last.get("error"))
    return last.get("id") in set(known)


def new_entries(messages: Sequence[Any], known: Iterable[str]) -> list[dict[str, Any]]:
    """The entries added since `known` was read: nothing an earlier attempt left in the session counts."""
    seen = set(known)
    entries = [m for m in messages if isinstance(m, dict) and isinstance(m.get("info"), dict)]
    return [m for m in entries if m["info"].get("id") not in seen]


def turn_finished(messages: Sequence[Any], *, known: Iterable[str], message_id: str | None) -> bool:
    """True when this attempt's turn is over, by the rule the engine's own loop exits on (read from the
    1.19.5 binary): the newest assistant message answers the newest user message, its `finish` is not
    `tool-calls` or `unknown`, and none of its tools is still going. An error also ends the turn.

    Relationships, not counts. `parentID` ties a reply to its prompt, and the newest user message may be
    later than ours, because compaction adds one. When the exact prompt is known it must be present."""
    entries = new_entries(messages, known)
    users = [m["info"] for m in entries if m["info"].get("role") == "user"]
    replies = [m for m in entries if m["info"].get("role") == "assistant"]
    if not users or not replies or (message_id is not None and message_id not in {u.get("id") for u in users}):
        return False
    last = replies[-1]
    info = last["info"]
    if info.get("parentID") != users[-1].get("id"):
        return False
    if info.get("error"):
        return True
    times = info.get("time") if isinstance(info.get("time"), dict) else {}
    if not times.get("completed") or info.get("finish") in (None, "tool-calls", "unknown"):
        return False
    parts = last.get("parts") if isinstance(last.get("parts"), list) else []
    return not any(_tool_going(part) for part in parts)


def _tool_going(part: Any) -> bool:
    if not isinstance(part, dict) or part.get("type") != "tool":
        return False
    state = part.get("state") if isinstance(part.get("state"), dict) else {}
    return state.get("status") in ("pending", "running")


def _signature(messages: Sequence[Any]) -> tuple[Any, ...]:
    """Changes when a message or a part is added or a message is updated."""
    rows: list[tuple[Any, ...]] = []
    for message in messages:
        if isinstance(message, dict):
            info = message.get("info") if isinstance(message.get("info"), dict) else {}
            times = info.get("time") if isinstance(info.get("time"), dict) else {}
            parts = message.get("parts")
            rows.append((info.get("id"), times.get("completed"), len(parts) if isinstance(parts, list) else 0))
    return tuple(rows)


def observe_quiescence(
    control: SessionControl,
    session_id: str,
    *,
    fingerprint: Fingerprint,
    known_messages: Iterable[str] = (),
    policy: StopPolicy = StopPolicy(),
    clock: Callable[[], float] = time.monotonic,
    sleep: Callable[[float], None] = time.sleep,
) -> Quiescence:
    """Watch until the session and the workspace hold still, or the time limit passes.

    Quiet means: on `stable_polls` consecutive readings the session was not working, its newest
    message was finished, and the message list and the workspace fingerprint did not change. A
    reading that cannot be taken breaks the run of identical readings. The result is an observation."""
    known = list(known_messages)
    started = clock()
    polls = 0
    streak = 0
    unreachable = 0
    previous: tuple[Any, ...] | None = None
    state = "unknown"
    while True:
        polls += 1
        try:
            state = _session_state(control.statuses(), session_id)
            messages = control.messages(session_id)
        except ControlError:
            state = "unknown"
            streak = 0
            previous = None
            unreachable += 1
            if unreachable >= policy.unreachable_polls:
                return Quiescence(False, polls, clock() - started, state)
        else:
            unreachable = 0
            workspace = fingerprint()
            reading = (_signature(messages), workspace)
            still = state in ("idle", "absent") and _settled(messages, known) and workspace is not None
            streak = streak + 1 if still and reading == previous else (1 if still else 0)
            previous = reading
            if streak >= policy.stable_polls:
                return Quiescence(True, polls, clock() - started, state)
        if clock() - started >= policy.settle_timeout_seconds:
            return Quiescence(False, polls, clock() - started, state)
        sleep(policy.poll_seconds)


class AttemptGuard:
    """One attempt's lifecycle. The only way to send a prompt, and the only way to allow a restore."""

    def __init__(
        self,
        record: AttemptRecord,
        *,
        store: AttemptStore,
        writer: WriterControl,
        fingerprint: Fingerprint,
        policy: StopPolicy = StopPolicy(),
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], None] = time.sleep,
        wall_ms: Callable[[], int] = lambda: time.time_ns() // 1_000_000,
        progress: Progress | None = None,
    ) -> None:
        self._record = record
        self._store = store
        self._writer = writer
        self._fingerprint = fingerprint
        self._policy = policy
        self._clock = clock
        self._sleep = sleep
        self._wall_ms = wall_ms
        self._progress = progress
        self._lock = threading.RLock()

    @classmethod
    def open(
        cls, *, store: AttemptStore, project_id: str, run_id: str, call_id: str, task_key: str, container_id: str | None = None, **deps: Any
    ) -> AttemptGuard:
        """Claim the attempt. Raises AttemptRefused for a repeated call or a held project."""
        record = AttemptRecord(project_id=project_id, run_id=run_id, call_id=call_id, task_key=task_key, container_id=container_id)
        store.claim(record)
        return cls(record, store=store, **deps)

    @property
    def phase(self) -> AttemptPhase:
        return self._record.phase

    @property
    def record(self) -> AttemptRecord:
        return self._record

    def report(self) -> StopReport:
        r = self._record
        return StopReport(
            phase=r.phase, abort_acknowledged=r.abort_acknowledged, observed_quiet=r.observed_quiet,
            process_terminated=r.process_terminated, workspace_stable=r.workspace_stable,
            restore_verified=r.restore_verified, detail=self._summary(), stop_evidence=r.stop_evidence or "",
        )  # fmt: skip

    @property
    def stop_needed(self) -> bool:
        """True while a prompt may be running and nothing has stopped it yet."""
        return self._record.phase in (Phase.SUBMITTED, Phase.RUNNING, Phase.AMBIGUOUS)

    # --- A1: one prompt, at most once ------------------------------------------------------------

    def submit(
        self,
        control: SessionControl,
        session_id: str,
        body: dict[str, Any],
        *,
        use_message_id: bool,
        cancel: threading.Event | None = None,
    ) -> SubmitVerdict:
        """Send the prompt at most once. There is no path back to here: a retry is a new attempt."""
        with self._lock:
            if self._record.phase is not Phase.PREPARED:
                raise LifecycleError(f"a prompt cannot be sent in phase {self._record.phase}: it is never sent twice")
            if cancel is not None and cancel.is_set():
                self.abandon("the run was cancelled before the prompt was sent")
                return SubmitVerdict.NOT_SENT
            try:
                known = message_ids(control.messages(session_id))
            except ControlError as exc:
                self.abandon(f"the session could not be read before sending: {exc}")
                return SubmitVerdict.NOT_SENT
            self._record.session_id = session_id
            self._record.known_messages = known
            payload = dict(body)
            if use_message_id:
                self._record.message_id = new_message_id(self._wall_ms())
                payload["messageID"] = self._record.message_id
            self._move(Phase.SUBMITTED)
        try:
            status = control.submit(session_id, payload)
        except Exception:  # whatever went wrong, it cannot show that the prompt was not delivered
            status = None
        with self._lock:
            verdict = classify_submission(status)
            if verdict is SubmitVerdict.ACCEPTED:
                self._move(Phase.RUNNING)
                return verdict
            seen = self._reconcile(control)
            self._record.reconciliation = seen
            if verdict is SubmitVerdict.REJECTED and seen is Reconciliation.NOT_SEEN:
                self._note_failure(f"the server refused the prompt ({status})")
                self._move(Phase.REJECTED)
                return verdict
            answer = "no answer" if status is None else f"status {status}"
            self._note_failure(f"the prompt's delivery is ambiguous ({answer}; {_RECONCILED[seen]})")
            self._move(Phase.AMBIGUOUS)
            return SubmitVerdict.AMBIGUOUS

    def _reconcile(self, control: SessionControl) -> Reconciliation:
        """Look for the exact message. Not finding it never means the prompt did not start."""
        record = self._record
        session_id = record.session_id or ""
        try:
            state = _session_state(control.statuses(), session_id)
            ids = message_ids(control.messages(session_id))
        except ControlError:
            return Reconciliation.UNKNOWN
        found = record.message_id in ids if record.message_id else bool(set(ids) - set(record.known_messages))
        if found or state in _BUSY_STATES:
            return Reconciliation.STARTED
        return Reconciliation.NOT_SEEN if state in ("idle", "absent") else Reconciliation.UNKNOWN

    def abandon(self, reason: str) -> None:
        """The attempt ended before anything was sent. Does nothing once a prompt has left."""
        with self._lock:
            if self._record.phase is Phase.PREPARED:
                self._note_failure(reason)
                self._move(Phase.ABANDONED)

    def mark_finished(self) -> bool:
        """The engine finished by itself. False when the outcome was already decided: a late result
        is counted and ignored, so it can never turn a stopped attempt back into a success."""
        with self._lock:
            if self._record.phase is Phase.RUNNING:
                self._move(Phase.FINISHED)
                return True
            self.note_late_result("the engine reported finishing")
            return False

    def note_late_result(self, what: str) -> None:
        with self._lock:
            self._record.late_results += 1
            self._store.save(self._record)
            self._emit("coding.late_result_ignored", {"what": what[:200], "phase": str(self._record.phase)})

    def keep(self) -> None:
        """The audit accepted a finished attempt."""
        with self._lock:
            self._move(Phase.KEPT)

    # --- A2: one way to stop ---------------------------------------------------------------------

    def secure(self, control: SessionControl, reason: str) -> StopReport:
        """Stop the attempt so the workspace can be trusted. Every cause of stopping comes here.

        Abort, watch, end the container, confirm the workspace is still. Nothing in here looks at the
        cancel flag, so a second cancellation cannot shorten it. Safe to call again. When a prompt
        never left, or was refused, there is nothing to stop and the report says so."""
        with self._lock:
            record = self._record
            if record.phase is Phase.KEPT:
                raise LifecycleError("a kept attempt cannot be stopped: its edits were accepted")
            if record.phase in _STOPPED_PHASES or record.phase in _NOTHING_TO_STOP:
                return self.report()
            if record.phase is Phase.PREPARED:
                self.abandon(reason)
                return self.report()
            self._note_failure(reason)
            if record.phase is Phase.SUBMITTED:  # only after a crash: the send's answer was lost
                self._move(Phase.AMBIGUOUS)
            if record.phase is not Phase.QUIESCENCE_CHECK:
                if record.phase is not Phase.ABORT_REQUESTED:
                    self._move(Phase.ABORT_REQUESTED)
                record.abort_acknowledged = self._request_abort(control)
                self._move(Phase.QUIESCENCE_CHECK)
        seen = self.observe(control)
        with self._lock:
            if record.phase is not Phase.QUIESCENCE_CHECK:  # another caller finished the stop meanwhile
                return self.report()
            record.observed_quiet = seen.quiet
            self._store.save(record)
            return self._finish_stop(seen)

    def observe(self, control: SessionControl, session_id: str | None = None) -> Quiescence:
        """Watch one session, with this attempt's limits. The attempt's own session by default."""
        target = session_id or self._record.session_id or ""
        return observe_quiescence(
            control, target, fingerprint=self._fingerprint, known_messages=self._record.known_messages,
            policy=self._policy, clock=self._clock, sleep=self._sleep,
        )  # fmt: skip

    def _request_abort(self, control: SessionControl) -> bool:
        session_id = self._record.session_id or ""
        for attempt in range(self._policy.abort_attempts):
            if attempt:
                self._sleep(self._policy.poll_seconds)
            try:
                control.abort(session_id)
                return True
            except ControlError:
                continue
        return False

    def _finish_stop(self, seen: Quiescence) -> StopReport:
        """Observation is recorded; only confirmed termination, then a still workspace, permits a restore."""
        record = self._record
        record.process_terminated = self._terminate()
        self._store.save(record)
        if not record.process_terminated:
            return self._quarantine(f"the coding container could not be confirmed stopped (last seen: {seen.last_state})")
        record.workspace_stable = self._still_after_stop()
        self._store.save(record)
        if not record.workspace_stable:
            return self._quarantine("the workspace was not still after the container stopped")
        self._move(Phase.SAFE_TO_RESTORE)
        return self.report()

    def _terminate(self) -> bool:
        try:
            result = self._writer.terminate()
            self._record.stop_evidence = result.evidence[:2000]
            return result.confirmed is True
        except Exception as exc:  # a stop that failed is an unconfirmed stop, whatever the reason
            self._record.stop_evidence = f"the stop raised {type(exc).__name__}"
            return False

    def _still_after_stop(self) -> bool:
        last: str | None = None
        for reading in range(self._policy.stable_polls + 1):
            if reading:
                self._sleep(self._policy.poll_seconds)
            current = self._fingerprint()
            if current is None or (reading and current != last):
                return False
            last = current
        return True

    def _quarantine(self, why: str) -> StopReport:
        self._record.detail = why
        self._move(Phase.QUARANTINED)
        return self.report()

    # --- Restoring --------------------------------------------------------------------------------

    def restore(self, checkpoint: Restorable, root: Path) -> int:
        """Restore the workspace and compare it with the checkpoint. Raises RestoreForbidden unless
        termination was confirmed. If it fails, or the result differs, the attempt is quarantined."""
        with self._lock:
            if self._record.phase is not Phase.SAFE_TO_RESTORE:
                raise RestoreForbidden(f"the workspace may not be restored in phase {self._record.phase}")
            try:
                changed = checkpoint.restore(root)
                differences = checkpoint.differences(root)
            except (OSError, ValueError, KeyError) as exc:
                self._quarantine(f"restoring failed part-way ({type(exc).__name__})")
                raise RestoreForbidden("restoring failed part-way; the workspace is quarantined") from exc
            if differences:
                self._quarantine(f"the restored workspace differs from the checkpoint at {len(differences)} paths")
                raise RestoreForbidden("the restored workspace differs from the checkpoint; it is quarantined")
            self._record.restore_verified = True
            self._move(Phase.RESTORED)
            return changed

    # --- After a crash ----------------------------------------------------------------------------

    def recover(self, control: SessionControl) -> StopReport:
        """Finish an attempt a stopped backend left behind. Nothing is assumed from the backend having
        been down: the writer may have carried on without it."""
        with self._lock:
            phase = self._record.phase
            if phase is Phase.PREPARED:
                self.abandon("the backend stopped before the prompt was sent")
                return self.report()
            if phase is Phase.SAFE_TO_RESTORE:  # the proof must still hold: nothing vouches for the time away
                if not self._terminate():
                    return self._quarantine("the coding container could not be confirmed stopped after a restart")
                if not self._still_after_stop():
                    return self._quarantine("the workspace was not still after a restart")
                return self.report()
        return self.secure(control, "the backend stopped during the attempt")

    # --- Internals --------------------------------------------------------------------------------

    def _note_failure(self, reason: str) -> None:
        if self._record.failure_reason is None:
            self._record.failure_reason = reason[:2000]

    def _move(self, phase: AttemptPhase) -> None:
        current = self._record.phase
        if phase not in _NEXT.get(current, frozenset()):
            raise LifecycleError(f"an attempt cannot move from {current} to {phase}")
        self._record.phase = phase
        self._store.save(self._record)
        self._emit("coding.attempt_phase", {"from": str(current), "to": str(phase)})

    def _emit(self, event: str, payload: dict[str, Any]) -> None:
        if self._progress is not None:
            self._progress(event, {"run_id": self._record.run_id, "task": self._record.task_key, **payload})

    def _summary(self) -> str:
        r = self._record
        if r.phase is Phase.ABANDONED:
            return "nothing was sent, so there was nothing to stop"
        if r.phase is Phase.REJECTED:
            return "the server refused the prompt and nothing is running, so there was nothing to stop"
        facts = [
            "the abort was acknowledged" if r.abort_acknowledged else "the abort was not acknowledged",
            "the session was seen quiet" if r.observed_quiet else "the session was not seen quiet",
            "the container is confirmed stopped" if r.process_terminated else "the container is not confirmed stopped",
        ]
        if r.phase is Phase.QUARANTINED:
            facts.append(f"the workspace is quarantined: {r.detail}")
        elif r.phase is Phase.RESTORED:
            facts.append("the workspace was restored and verified")
        elif r.phase is Phase.SAFE_TO_RESTORE:
            facts.append("the workspace is safe to restore")
        return "; ".join(facts)


_RECONCILED = {
    Reconciliation.STARTED: "the prompt had started",
    Reconciliation.NOT_SEEN: "the prompt was not seen, and may still start",
    Reconciliation.UNKNOWN: "the server's state could not be read",
}
