"""OpenCode adapter: drives `opencode serve` over its HTTP API (ARCHITECTURE §6, SECURITY T3).

The contract is the pinned 1.19.5 server: its own OpenAPI document (`GET /doc`), checked at run
time, and the real schemas of its sessions, messages and events. The decisions live in
`OpenCodeProvider`, which speaks to the `OpenCodeApi` Protocol and is tested with in-memory fakes.
`OpenCodeClient` is the thin HTTP side: every answer goes through the pure checks in
opencode_contract.py before anything uses it.

One task:
1. Refuse to start unless the API filter answers. A bare OpenCode server never does, so this
   adapter cannot drive one.
2. Check health and version, the API operations the adapter needs, the *effective* policy
   (`GET /config` with the enforced rules of `GET /agent`, see opencode_policy.py) and the tool
   inventory (opencode_tools.py). Any problem: nothing is sent.
3. Continue the task's earlier session if it still exists, otherwise create one. A busy
   session is never prompted a second time.
4. Open the event stream, then send the brief with `prompt_async`.
5. Follow events for this session: progress is recorded, token use is checked against the task's
   budget, and each tool call is judged against the allowlist. Every permission request and
   question is rejected and stops the task: the policy allows or denies everything up front, so
   an "ask" means the rules that were verified are not the rules in force.
6. Completion is confirmed from the session's messages, by their relationships
   (session_control.turn_finished), never from a single event or a count. A dropped stream is
   reopened; a timeout, a cancelled run or a budget overrun stops the attempt.

The prompt goes through `AttemptGuard.submit`, which sends it at most once. Every way of stopping
goes through `AttemptGuard.secure`, which never treats an acknowledged abort as a stopped writer
(session_control.py). The caller keeps the guard and restores the workspace only through it.

The prompt carries no `tools` field. On 1.19.5 that field does not add to the session's rules: it
replaces the whole list (`SessionPrompt.prompt`, read from the binary), which would drop the
protected-path denials.

The adapter never calls the endpoints that run commands (`/session/{id}/shell`,
`/session/{id}/command`). It has no method for them.
"""

from __future__ import annotations

import re
import threading
import time
from collections.abc import Callable, Iterator, Sequence
from contextlib import contextmanager
from typing import Any, ContextManager, Protocol, TypeVar
from urllib.parse import quote

import httpx

from app.coding.opencode_contract import (
    FILTER_HEALTH_PATH,
    Contract,
    ContractError,
    SSEParser,
    check_confirmed,
    check_event_stream,
    check_filter,
    check_health,
    check_spec,
    json_answer,
    parse_messages,
    parse_session,
    parse_statuses,
    parse_tool_ids,
    spec_from_doc,
)
from app.coding.opencode_policy import PROMPTED_AGENT, is_protected, session_rules, verify_effective_config
from app.coding.opencode_tools import relative_path, tool_inventory_problems, tool_problems
from app.coding.session_control import AttemptGuard, ControlError, SubmitVerdict, new_entries, turn_finished
from app.coding.types import AttemptPhase, CodingOutcome, CodingStatus, CodingTask, Progress

PROVIDER_NAME = "opencode_server"
PERMISSION_EVENTS = frozenset({"permission.asked", "permission.v2.asked"})
QUESTION_EVENTS = frozenset({"question.asked", "question.v2.asked"})
TOOL_CALL_EVENT = "session.next.tool.called"
# Events DevPilot's own use can never cause: an interactive terminal or an MCP server.
FORBIDDEN_EVENT_PREFIXES = ("pty.", "mcp.")
# What SSEParser reports instead of an event it could not read. It could have been a tool call.
UNREADABLE_EVENTS = frozenset({"devpilot.oversized_event", "devpilot.invalid_event"})
MAX_EVENT_BYTES = 8 * 1024 * 1024  # a tool event carries a file's text and its diff; files stop at 2 MB
MAX_PROGRESS_EVENTS = 200
MAX_REPORTED_FILES = 500
_ID = re.compile(r"^[A-Za-z0-9_-]{1,128}$")
_DETAIL_CHARS = 200
T = TypeVar("T")


class OpenCodeError(Exception):
    def __init__(self, status: CodingStatus, message: str) -> None:
        super().__init__(message)
        self.status = status
        self.message = message


class OpenCodeNotFound(OpenCodeError):
    def __init__(self, message: str) -> None:
        super().__init__(CodingStatus.PROTOCOL, message)


class _Quiet(Exception):
    """No event arrived within the read timeout. Not an error: the session is polled instead."""


_CONTRACT_STATUS = {
    "incompatible": CodingStatus.INCOMPATIBLE,
    "unavailable": CodingStatus.UNAVAILABLE,
    "protocol": CodingStatus.PROTOCOL,
}


def _checked(check: Callable[..., T], *args: Any, **kwargs: Any) -> T:
    try:
        return check(*args, **kwargs)
    except ContractError as exc:
        raise OpenCodeError(_CONTRACT_STATUS[exc.kind], exc.message) from exc


class OpenCodeApi(Protocol):
    """What the provider needs from a server. Every answer is already checked; failures raise OpenCodeError."""

    @property
    def directory(self) -> str: ...

    def filter_health(self) -> None: ...

    def health(self) -> str:
        """The server's version."""
        ...

    def spec(self) -> dict[str, Any]: ...

    def config(self) -> Any: ...

    def agents(self) -> Any: ...

    def tool_ids(self) -> list[str]: ...

    def create_session(self, title: str) -> dict[str, Any]: ...

    def get_session(self, session_id: str) -> dict[str, Any] | None:
        """None when the session does not exist."""
        ...

    def statuses(self) -> dict[str, Any]: ...

    def messages(self, session_id: str) -> list[Any]: ...

    def prompt_async(self, session_id: str, body: dict[str, Any]) -> int | None:
        """One send, never retried. The HTTP status, or None when no answer arrived."""
        ...

    def abort(self, session_id: str) -> None: ...

    def reject_permission(self, contract: Contract, session_id: str, request_id: str) -> None: ...

    def reject_question(self, contract: Contract, request_id: str) -> None: ...

    def events(self, *, read_timeout: float) -> ContextManager[Iterator[dict[str, Any]]]: ...


# --- HTTP client -------------------------------------------------------------------


class OpenCodeClient:
    """Thin client for the operations DevPilot uses. Every session call is pinned to one folder."""

    def __init__(
        self,
        base_url: str,
        *,
        token: str | None = None,
        username: str | None = None,
        password: str | None = None,
        directory: str,
        timeout: float = 30.0,
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        """`token` is the filter's per-run credential, and every real use goes through it. A username
        and password reach a bare server, which only the read-only compatibility check does."""
        self._base_url = base_url.rstrip("/")
        self._directory = directory
        self._http = httpx.Client(
            base_url=self._base_url,
            auth=httpx.BasicAuth(username, password) if username and password else None,
            headers={"Authorization": f"Bearer {token}"} if token else None,
            timeout=timeout,
            transport=transport,
        )

    @property
    def directory(self) -> str:
        return self._directory

    def close(self) -> None:
        self._http.close()

    def filter_health(self) -> None:
        _checked(check_filter, self._json("GET", FILTER_HEALTH_PATH, scoped=False))

    def health(self) -> str:
        return _checked(check_health, self._json("GET", "/global/health", scoped=False))

    def spec(self) -> dict[str, Any]:
        response = self._request("GET", "/doc", scoped=False)
        return _checked(spec_from_doc, response.headers.get("content-type", ""), response.content)

    def config(self) -> Any:
        return self._json("GET", "/config")

    def agents(self) -> Any:
        return self._json("GET", "/agent")

    def tool_ids(self) -> list[str]:
        return _checked(parse_tool_ids, self._json("GET", "/experimental/tool/ids"))

    def create_session(self, title: str) -> dict[str, Any]:
        return self._session(self._json("POST", "/session", body={"title": title, "permission": session_rules()}))

    def get_session(self, session_id: str) -> dict[str, Any] | None:
        try:
            return self._session(self._json("GET", f"/session/{_segment(session_id)}"))
        except OpenCodeNotFound:
            return None

    def statuses(self) -> dict[str, Any]:
        return _checked(parse_statuses, self._json("GET", "/session/status"))

    def messages(self, session_id: str) -> list[Any]:
        payload = self._json("GET", f"/session/{_segment(session_id)}/message")
        return _checked(parse_messages, payload, session_id=session_id)

    def prompt_async(self, session_id: str, body: dict[str, Any]) -> int | None:
        """One send, never retried. The HTTP status, or None when no answer arrived, in which case
        the server may still have accepted it (session_control.classify_submission decides)."""
        path = f"/session/{_segment(session_id)}/prompt_async"
        try:
            return self._http.request("POST", path, json=body, params=self._params(True)).status_code
        except httpx.TransportError:  # timeouts included
            return None

    def abort(self, session_id: str) -> None:
        _checked(check_confirmed, self._json("POST", f"/session/{_segment(session_id)}/abort"), "the abort")

    def reject_permission(self, contract: Contract, session_id: str, request_id: str) -> None:
        if contract.permission_reply == "/permission/{}/reply":
            body: dict[str, str] = {"reply": "reject", "message": "Refused by DevPilot policy."}
            answer = self._json("POST", f"/permission/{_segment(request_id)}/reply", body=body)
        else:
            path = f"/session/{_segment(session_id)}/permissions/{_segment(request_id)}"
            answer = self._json("POST", path, body={"response": "reject"})
        _checked(check_confirmed, answer, "the permission rejection")

    def reject_question(self, contract: Contract, request_id: str) -> None:
        if contract.question_reject is None:
            raise OpenCodeError(CodingStatus.PROTOCOL, "the server has no operation to reject a question")
        _checked(check_confirmed, self._json("POST", f"/question/{_segment(request_id)}/reject"), "the question rejection")

    @contextmanager
    def events(self, *, read_timeout: float) -> Iterator[Iterator[dict[str, Any]]]:
        timeout = httpx.Timeout(10.0, read=read_timeout)
        try:
            with self._http.stream("GET", "/event", params=self._params(True), timeout=timeout) as response:
                if response.status_code >= 300:
                    response.read()
                    _raise_for_status(response, "GET /event")
                _checked(check_event_stream, response.status_code, response.headers.get("content-type", ""))
                yield _sse_events(response)
        except httpx.ReadTimeout as exc:
            raise _Quiet() from exc
        except httpx.TimeoutException as exc:
            raise OpenCodeError(CodingStatus.UNAVAILABLE, "the OpenCode event stream timed out") from exc
        except httpx.TransportError as exc:
            raise OpenCodeError(CodingStatus.UNAVAILABLE, f"the OpenCode event stream failed ({type(exc).__name__})") from exc

    # --- helpers ---------------------------------------------------------------

    def _session(self, payload: Any) -> dict[str, Any]:
        return _checked(parse_session, payload, directory=self._directory, permission=session_rules())

    def _params(self, scoped: bool) -> dict[str, str]:
        return {"directory": self._directory} if scoped else {}

    def _request(self, method: str, path: str, *, body: Any = None, scoped: bool = True) -> httpx.Response:
        try:
            response = self._http.request(method, path, json=body, params=self._params(scoped))
        except httpx.TimeoutException as exc:
            raise OpenCodeError(CodingStatus.UNAVAILABLE, f"OpenCode did not answer {method} {path} in time") from exc
        except httpx.TransportError as exc:
            raise OpenCodeError(
                CodingStatus.UNAVAILABLE, f"cannot reach OpenCode at {self._base_url} ({type(exc).__name__})"
            ) from exc
        _raise_for_status(response, f"{method} {path}")
        return response

    def _json(self, method: str, path: str, *, body: Any = None, scoped: bool = True) -> Any:
        response = self._request(method, path, body=body, scoped=scoped)
        what = f"{method} {path}"
        return _checked(json_answer, response.status_code, response.headers.get("content-type", ""), response.content, what)


def _raise_for_status(response: httpx.Response, what: str) -> None:
    code = response.status_code
    if code < 300:
        return
    detail = response.text.strip().replace("\n", " ")[:_DETAIL_CHARS]
    if code == 401:
        raise OpenCodeError(CodingStatus.AUTH, f"OpenCode refused DevPilot's credentials ({code})")
    if code == 403:
        raise OpenCodeError(CodingStatus.PROTOCOL, f"{what} was refused by the API filter")
    if code == 404:
        raise OpenCodeNotFound(f"{what}: not found")
    if code >= 500:
        raise OpenCodeError(CodingStatus.UNAVAILABLE, f"{what}: OpenCode server error ({code})")
    raise OpenCodeError(CodingStatus.PROTOCOL, f"{what} was refused ({code}): {detail}")


def _segment(value: str) -> str:
    # IDs come from the server; they are checked before being placed in a URL path.
    if not isinstance(value, str) or not _ID.fullmatch(value):
        raise OpenCodeError(CodingStatus.PROTOCOL, "the server returned an ID that is not safe to use in a URL")
    return quote(value, safe="")


def _sse_events(response: httpx.Response) -> Iterator[dict[str, Any]]:
    parser = SSEParser(max_event_bytes=MAX_EVENT_BYTES)
    for chunk in response.iter_raw():
        yield from parser.feed(chunk)
    yield from parser.finish()


# --- Contract ----------------------------------------------------------------------


def verify_contract(api: OpenCodeApi) -> Contract:
    version = api.health()
    return _checked(check_spec, api.spec(), version)


# --- Provider ----------------------------------------------------------------------


class _Control:
    """The API as session_control.py sees it: its errors become ControlError."""

    def __init__(self, api: OpenCodeApi) -> None:
        self._api = api

    def submit(self, session_id: str, body: dict[str, Any]) -> int | None:
        return self._api.prompt_async(session_id, body)

    def statuses(self) -> dict[str, Any]:
        try:
            return self._api.statuses()
        except OpenCodeError as exc:
            raise ControlError(exc.message) from exc

    def messages(self, session_id: str) -> Sequence[Any]:
        try:
            return self._api.messages(session_id)
        except OpenCodeError as exc:
            raise ControlError(exc.message) from exc

    def abort(self, session_id: str) -> None:
        try:
            self._api.abort(session_id)
        except OpenCodeError as exc:
            raise ControlError(exc.message) from exc


class OpenCodeProvider:
    """The CodingProvider that drives a real OpenCode server, through the API filter."""

    name = PROVIDER_NAME

    def __init__(
        self,
        api: OpenCodeApi,
        *,
        model: str,
        poll_seconds: float = 5.0,
        max_reconnects: int = 5,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        provider_id, _, model_id = model.partition("/")
        self._api = api
        self._control = _Control(api)
        self._model = {"providerID": provider_id, "modelID": model_id}
        self._poll = poll_seconds
        self._max_reconnects = max_reconnects
        self._clock = clock

    def run_task(
        self, task: CodingTask, *, cancel: threading.Event | None, progress: Progress, guard: AttemptGuard
    ) -> CodingOutcome:
        if guard.phase is not AttemptPhase.PREPARED:  # an old or cancelled attempt never touches the server again
            detail = "this attempt already ended, and a retry is a new attempt"
            return CodingOutcome(status=CodingStatus.CANCELLED, detail=detail, stop=guard.report())
        try:
            outcome = self._run(task, cancel, progress, guard)
        except Exception:
            # A bug must not leave a prompt running with nobody stopping it.
            if guard.stop_needed:
                guard.secure(self._control, "an unexpected error stopped the adapter")
            raise
        reason = outcome.detail or f"the task ended with {outcome.status}"
        guard.abandon(reason)
        if guard.stop_needed:  # any exit that left a prompt running and did not stop it
            outcome.detail = f"{reason}; {guard.secure(self._control, reason).detail}"
        outcome.stop = guard.report()
        return outcome

    def _run(self, task: CodingTask, cancel: threading.Event | None, progress: Progress, guard: AttemptGuard) -> CodingOutcome:
        outcome = CodingOutcome(status=CodingStatus.COMPLETED)
        try:
            refusal = self._filter_refusal()
            if refusal is not None:
                outcome.status = CodingStatus.INCOMPATIBLE
                outcome.detail = refusal
                return outcome
            contract = verify_contract(self._api)
            outcome.engine_version = contract.version
            agents = self._api.agents()
            problems = verify_effective_config(self._api.config(), agents)
            problems += tool_inventory_problems(self._api.tool_ids(), contract.version, agents)
            if problems:
                outcome.status = CodingStatus.INCOMPATIBLE
                outcome.detail = "the engine's effective configuration is not safe: " + "; ".join(problems[:5])
                return outcome
            session_id = self._session(task, progress)
            outcome.session_id = session_id
            if task.abort_first:
                self._api.abort(session_id)
                progress("coding.session_aborted", {"session_id": session_id, "reason": "the previous attempt was interrupted"})
                settled = guard.observe(self._control, session_id)
                if not settled.quiet:
                    outcome.status = CodingStatus.BUSY
                    outcome.detail = f"the earlier attempt was not seen to stop (last state: {settled.last_state}); nothing was sent"
                    return outcome
            status = self._api.statuses().get(session_id)
            if isinstance(status, dict) and status.get("type") in ("busy", "retry"):
                outcome.status = CodingStatus.BUSY
                outcome.detail = "the session is still working on an earlier prompt; nothing was sent"
                return outcome
            return self._drive(task, contract, session_id, outcome, cancel, progress, guard)
        except OpenCodeError as exc:
            outcome.status = exc.status
            outcome.detail = exc.message
            return outcome

    def _filter_refusal(self) -> str | None:
        """Why no task may start. The filter is what keeps the server's other operations out of reach."""
        try:
            self._api.filter_health()
        except OpenCodeError as exc:
            return f"no API filter answers in front of OpenCode, and a bare server is never driven: {exc.message}"
        return None

    def _session(self, task: CodingTask, progress: Progress) -> str:
        if task.session_id is not None:
            if self._api.get_session(task.session_id) is not None:
                return task.session_id
            progress("coding.session_replaced", {"previous": task.session_id, "reason": "the session no longer exists"})
        created = self._api.create_session(f"DevPilot {task.key}: {task.title}"[:120])
        return str(created["id"])

    def _drive(
        self,
        task: CodingTask,
        contract: Contract,
        session_id: str,
        outcome: CodingOutcome,
        cancel: threading.Event | None,
        progress: Progress,
        guard: AttemptGuard,
    ) -> CodingOutcome:
        tracker = _Tracker(
            session_id,
            max_tokens=task.max_tokens,
            workspace_dir=self._api.directory,
            progress=progress,
            reject_permission=lambda request_id: self._api.reject_permission(contract, session_id, request_id),
            reject_question=lambda request_id: self._api.reject_question(contract, request_id),
        )
        deadline = self._clock() + task.timeout_seconds
        prompt_sent = False
        quiet_rounds = 0
        while True:
            stop = self._stop_reason(cancel, deadline)
            if stop is not None:
                return self._abort(outcome, stop, tracker, task, guard)
            got_events = False
            try:
                with self._api.events(read_timeout=self._poll) as stream:
                    if not prompt_sent:
                        verdict = guard.submit(
                            self._control, session_id, self._body(task), use_message_id=contract.client_message_id, cancel=cancel
                        )
                        if verdict is not SubmitVerdict.ACCEPTED:
                            return self._not_accepted(outcome, verdict, tracker, task, guard)
                        prompt_sent = True
                        tracker.ignore(guard.record.known_messages)
                        progress("coding.prompt_sent", {"session_id": session_id, "brief_chars": len(task.brief)})
                    for event in stream:
                        got_events = True
                        signal = tracker.handle(event)
                        if tracker.violation is not None:
                            return self._abort(outcome, CodingStatus.POLICY_VIOLATION, tracker, task, guard)
                        if tracker.over_limit:
                            return self._abort(outcome, CodingStatus.LIMIT, tracker, task, guard)
                        if signal is not None and self._finished(session_id, tracker, guard):
                            return self._complete(outcome, tracker, task, guard)
                        stop = self._stop_reason(cancel, deadline)
                        if stop is not None:
                            return self._abort(outcome, stop, tracker, task, guard)
            except _Quiet:
                pass
            except OpenCodeError as exc:
                if not prompt_sent:
                    raise
                if exc.status is not CodingStatus.UNAVAILABLE:
                    return self._abort(outcome, exc.status, tracker, task, guard, detail=exc.message)
                outcome.reconnects += 1
                if outcome.reconnects > self._max_reconnects:
                    detail = f"the event stream failed {outcome.reconnects} times: {exc.message}"
                    return self._abort(outcome, CodingStatus.UNAVAILABLE, tracker, task, guard, detail=detail)
                progress("coding.reconnecting", {"session_id": session_id, "attempt": outcome.reconnects})
            if prompt_sent and self._finished(session_id, tracker, guard):
                return self._complete(outcome, tracker, task, guard)
            if got_events:
                quiet_rounds = 0
            else:
                # The stream closed with nothing in it; wait a little before reopening it.
                delay = min(0.05 * 2**quiet_rounds, 1.0)
                quiet_rounds += 1
                if cancel is not None:
                    cancel.wait(delay)
                else:
                    time.sleep(delay)

    def _finished(self, session_id: str, tracker: _Tracker, guard: AttemptGuard) -> bool:
        status = self._api.statuses().get(session_id)
        if isinstance(status, dict) and status.get("type") in ("busy", "retry"):
            return False
        messages = self._api.messages(session_id)
        record = guard.record
        if not turn_finished(messages, known=record.known_messages, message_id=record.message_id):
            return False
        tracker.absorb(new_entries(messages, record.known_messages))
        return True

    def _body(self, task: CodingTask) -> dict[str, Any]:
        # No `tools` field: on 1.19.5 it would replace the session's whole rule list.
        return {"agent": PROMPTED_AGENT, "model": self._model, "parts": [{"type": "text", "text": task.brief}]}

    def _stop_reason(self, cancel: threading.Event | None, deadline: float) -> CodingStatus | None:
        if cancel is not None and cancel.is_set():
            return CodingStatus.CANCELLED
        if self._clock() >= deadline:
            return CodingStatus.TIMEOUT
        return None

    def _complete(self, outcome: CodingOutcome, tracker: _Tracker, task: CodingTask, guard: AttemptGuard) -> CodingOutcome:
        # The session's own record can reveal what the events missed.
        if tracker.violation is not None:
            return self._abort(outcome, CodingStatus.POLICY_VIOLATION, tracker, task, guard)
        if tracker.over_limit:
            return self._abort(outcome, CodingStatus.LIMIT, tracker, task, guard)
        tracker.fill(outcome)
        if not guard.mark_finished():  # decided already: a late finish changes nothing
            outcome.status = CodingStatus.CANCELLED
            outcome.detail = guard.record.failure_reason or "the attempt was already stopped"
            return outcome
        if tracker.error is not None:
            outcome.status = CodingStatus.FAILED
            outcome.detail = tracker.error
        return outcome

    def _not_accepted(
        self, outcome: CodingOutcome, verdict: SubmitVerdict, tracker: _Tracker, task: CodingTask, guard: AttemptGuard
    ) -> CodingOutcome:
        reason = guard.record.failure_reason or "the prompt was not sent"
        if verdict is SubmitVerdict.AMBIGUOUS:  # never resent: it is stopped, and any retry is a new attempt
            return self._abort(outcome, CodingStatus.UNAVAILABLE, tracker, task, guard, detail=reason)
        tracker.fill(outcome)
        outcome.status = CodingStatus.PROTOCOL if verdict is SubmitVerdict.REJECTED else CodingStatus.UNAVAILABLE
        outcome.detail = reason
        return outcome

    def _abort(
        self,
        outcome: CodingOutcome,
        status: CodingStatus,
        tracker: _Tracker,
        task: CodingTask,
        guard: AttemptGuard,
        *,
        detail: str | None = None,
    ) -> CodingOutcome:
        """Every way of stopping a task that may be running. Only the reason differs."""
        default = {
            CodingStatus.TIMEOUT: f"no result within {task.timeout_seconds:g} seconds",
            CodingStatus.CANCELLED: "the run was cancelled or the backend stopped",
            CodingStatus.LIMIT: f"the task used more than its budget of {task.max_tokens} tokens",
            CodingStatus.POLICY_VIOLATION: tracker.violation or "the engine broke DevPilot's policy",
        }.get(status, "the coding task did not finish")
        reason = detail or default
        report = guard.secure(self._control, reason)
        tracker.fill(outcome)
        outcome.status = status
        outcome.detail = f"{reason}; {report.detail}"
        return outcome


class _Tracker:
    """Follows one session's events: progress, permissions, tokens, and policy breaches."""

    def __init__(
        self,
        session_id: str,
        *,
        max_tokens: int,
        workspace_dir: str,
        progress: Progress,
        reject_permission: Callable[[str], None],
        reject_question: Callable[[str], None],
    ) -> None:
        self._session = session_id
        self._max_tokens = max_tokens
        self._workspace = workspace_dir.rstrip("/")
        self._progress = progress
        self._reject_permission = reject_permission
        self._reject_question = reject_question
        self._ignored: frozenset[str] = frozenset()
        self._usage: dict[str, tuple[int, int, float | None]] = {}
        self._tool_states: set[tuple[str, str]] = set()
        self._progress_count = 0
        self.tools_used: list[str] = []
        self.files: list[str] = []
        self.rejections: list[str] = []
        self.error: str | None = None
        self.violation: str | None = None

    @property
    def over_limit(self) -> bool:
        return sum(i + o for i, o, _ in self._usage.values()) > self._max_tokens

    def ignore(self, message_ids: Sequence[str]) -> None:
        """Messages from before this attempt: a delayed event about one of them is not this task's."""
        self._ignored = frozenset(message_ids)

    def handle(self, event: dict[str, Any]) -> str | None:
        """Process one event. Returns "idle" or "error" when the session may have finished."""
        kind = event.get("type")
        if kind in UNREADABLE_EVENTS:
            self.violation = "an engine event could not be read, so what it reported is unknown"
            return None
        if isinstance(kind, str) and kind.startswith(FORBIDDEN_EVENT_PREFIXES):
            self.violation = f"the server reported {kind!r}, which DevPilot's own use can never cause"
            return None
        props = event.get("properties")
        if not isinstance(props, dict):
            return None
        owner = _session_of(props)
        if owner is not None and owner != self._session:
            return None
        if kind in PERMISSION_EVENTS:
            self._permission(props)
        elif kind in QUESTION_EVENTS:
            self._question(props)
        elif kind == "message.part.updated":
            self._part(props.get("part"))
        elif kind == TOOL_CALL_EVENT:
            self._tool(str(props.get("tool") or "unknown"), {"status": "running", "input": props.get("input")}, str(props.get("callID")))
        elif kind == "message.updated":
            self._message(props.get("info"))
        elif kind == "file.edited":
            self._file(props.get("file"))
        elif kind == "session.error":
            self.error = _error_text(props.get("error"))
            return "error"
        elif kind == "session.idle":
            return "idle"
        elif kind == "session.status":
            status = props.get("status")
            if isinstance(status, dict) and status.get("type") == "idle":
                return "idle"
        return None

    def absorb(self, entries: list[dict[str, Any]]) -> None:
        """The session's own record of its messages wins over what the events said."""
        for entry in entries:
            info = entry["info"]
            if info.get("role") != "assistant":
                continue
            self._message(info)
            if info.get("error"):
                self.error = _error_text(info.get("error"))
            for part in entry.get("parts") or []:
                self._part(part)

    def fill(self, outcome: CodingOutcome) -> None:
        outcome.tokens_in = sum(i for i, _, _ in self._usage.values())
        outcome.tokens_out = sum(o for _, o, _ in self._usage.values())
        costs = [c for _, _, c in self._usage.values() if c is not None]
        outcome.cost_usd = round(sum(costs), 6) if costs else None
        outcome.tools_used = list(self.tools_used)
        outcome.files_reported = list(self.files)
        outcome.permission_rejections = list(self.rejections)

    def _emit(self, event_type: str, payload: dict[str, Any]) -> None:
        if self._progress_count < MAX_PROGRESS_EVENTS:
            self._progress_count += 1
            self._progress(event_type, payload)

    def _permission(self, props: dict[str, Any]) -> None:
        name = str(props.get("permission") or props.get("action") or "unknown")
        patterns = props.get("patterns") if isinstance(props.get("patterns"), list) else []
        try:
            self._reject_permission(str(props.get("id")))
        except OpenCodeError as exc:
            self.violation = f"a {name} permission request could not be rejected: {exc.message}"
            return
        self.rejections.append(name)
        self._emit("coding.permission_rejected", {"permission": name, "patterns": [str(p)[:100] for p in patterns[:5]]})
        self.violation = f"the engine asked for the {name} permission, which the verified rules should already decide"

    def _question(self, props: dict[str, Any]) -> None:
        try:
            self._reject_question(str(props.get("id")))
        except OpenCodeError as exc:
            self.violation = f"a question from the engine could not be rejected: {exc.message}"
            return
        self.rejections.append("question")
        self._emit("coding.question_rejected", {})
        self.violation = "the engine asked a question, which the verified rules deny"

    def _part(self, part: Any) -> None:
        if isinstance(part, dict) and part.get("type") == "tool":
            state = part.get("state") if isinstance(part.get("state"), dict) else {}
            self._tool(str(part.get("tool") or "unknown"), state, str(part.get("callID") or part.get("id")))

    def _tool(self, tool: str, state: dict[str, Any], call_id: str) -> None:
        status = str(state.get("status") or "unknown")
        if tool not in self.tools_used:
            self.tools_used.append(tool)
        key = (call_id, status)
        if key not in self._tool_states:
            self._tool_states.add(key)
            self._emit("coding.tool", {"tool": tool, "status": status})
        problems = tool_problems(tool, state, self._workspace)
        if problems and self.violation is None:
            self.violation = problems[0]

    def _message(self, info: Any) -> None:
        if not isinstance(info, dict) or info.get("role") != "assistant" or not isinstance(info.get("id"), str):
            return
        if info["id"] in self._ignored:
            return
        tokens = info.get("tokens") if isinstance(info.get("tokens"), dict) else {}
        cache = tokens.get("cache") if isinstance(tokens.get("cache"), dict) else {}
        tokens_in = _int(tokens.get("input")) + _int(cache.get("read")) + _int(cache.get("write"))
        tokens_out = _int(tokens.get("output")) + _int(tokens.get("reasoning"))
        cost = info.get("cost")
        self._usage[info["id"]] = (tokens_in, tokens_out, float(cost) if isinstance(cost, int | float) else None)

    def _file(self, path: Any) -> None:
        if not isinstance(path, str) or not path:
            return
        relative = relative_path(path, self._workspace)
        if relative is None:
            self.violation = f"the engine reported editing {path[:100]!r}, outside the project folder"
            return
        if is_protected(relative):
            self.violation = f"the engine reported editing the protected path {relative[:100]!r}"
            return
        if relative not in self.files and len(self.files) < MAX_REPORTED_FILES:
            self.files.append(relative)
            self._emit("coding.file_edited", {"path": relative[:300]})


def _session_of(props: dict[str, Any]) -> str | None:
    if isinstance(props.get("sessionID"), str):
        return props["sessionID"]
    for key in ("info", "part"):
        nested = props.get(key)
        if isinstance(nested, dict) and isinstance(nested.get("sessionID"), str):
            return nested["sessionID"]
    return None


def _error_text(error: Any) -> str:
    if not isinstance(error, dict):
        return "the engine reported an error"
    name = str(error.get("name") or "Error")
    data = error.get("data") if isinstance(error.get("data"), dict) else {}
    message = str(data.get("message") or "").strip()
    return f"{name}: {message}"[:500] if message else name


def _int(value: Any) -> int:
    return int(value) if isinstance(value, int | float) and value > 0 else 0
