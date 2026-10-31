"""The OpenCode adapter's decisions, driven through an in-memory `OpenCodeApi`.

No HTTP and no server. `FakeApi` implements the Protocol and answers from tests/coding_fakes.py. No
prompt reaches anything real. The checks of raw answers (status, content type, HTML pages) are pure
functions, tested in test_opencode_contract.py; here the provider sees what the real client raises
for them. The HTTP side is exercised read-only on the real server by scripts/opencode_readonly_http.py.

Held back with the adapter: this file imports it, so the two are committed together.
"""

from __future__ import annotations

import threading
from collections.abc import Callable, Iterable, Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

import pytest

from app.coding.opencode_contract import PERMISSION_REPLY_OPERATIONS, QUESTION_REJECT_OPERATION, REQUIRED_OPERATIONS, json_answer
from app.coding.opencode_policy import managed_config, session_rules
from app.coding.session_control import AttemptRefused, ControlError, RestoreForbidden
from app.coding.types import AttemptPhase, CodingOutcome, CodingStatus, CodingTask
from app.tools.adapters import opencode
from app.tools.adapters.opencode import OpenCodeError, OpenCodeNotFound, OpenCodeProvider, _checked, _Control
from tests.coding_fakes import SESSION, Scene
from tests.helpers import OPENCODE_1_19_5_TOOLS, opencode_agents

P = AttemptPhase
TIMEOUT = 100.0
ROOT = Path("/not/used")
Stream = Callable[[], Iterable[dict[str, Any]]]


def _managed() -> dict[str, Any]:
    return managed_config(model="openai/m", gateway_base_url="http://gw.invalid/v1")


def _spec(*, message_id: bool = True) -> dict[str, Any]:
    paths: dict[str, Any] = {}
    for method, template in [*REQUIRED_OPERATIONS, PERMISSION_REPLY_OPERATIONS[0], QUESTION_REJECT_OPERATION]:
        paths.setdefault(template.replace("{}", "{id}"), {})[method] = {"responses": {}}
    schema = {"properties": {"parts": {}, **({"messageID": {}} if message_id else {})}}
    paths["/session/{id}/prompt_async"]["post"] = {"requestBody": {"content": {"application/json": {"schema": schema}}}}
    return {"openapi": "3.1.0", "paths": paths}


def _html_page(what: str) -> OpenCodeError:
    """What the real client raises for the server's 200 web page, produced by the real check."""
    try:
        _checked(json_answer, 200, "text/html; charset=utf-8", b"<!doctype html><html></html>", what)
    except OpenCodeError as exc:
        return exc
    raise AssertionError("the check accepted an HTML page")


class FakeApi:
    """OpenCodeApi over the in-memory engine. Each entry of `streams` is one /event connection."""

    directory = "/workspace"

    def __init__(self, scene: Scene, *, message_id: bool = True) -> None:
        self.scene = scene
        self.streams: list[Stream] = []
        self.spec_doc = _spec(message_id=message_id)
        self.config_doc: Any = _managed()
        self.agents_doc: Any = opencode_agents(_managed())
        self.tools = list(OPENCODE_1_19_5_TOOLS)
        self.version = "1.19.5"
        self.failures: dict[str, OpenCodeError] = {}
        self.sessions = {SESSION}
        self.created: list[str] = []
        self.calls: list[str] = []
        self.rejected: list[tuple[str, str]] = []

    def _call(self, name: str) -> None:
        self.calls.append(name)
        if name in self.failures:
            raise self.failures[name]

    def filter_health(self) -> None:
        self._call("filter_health")

    def health(self) -> str:
        self._call("health")
        return self.version

    def spec(self) -> dict[str, Any]:
        self._call("spec")
        return self.spec_doc

    def config(self) -> Any:
        self._call("config")
        return self.config_doc

    def agents(self) -> Any:
        self._call("agents")
        return self.agents_doc

    def tool_ids(self) -> list[str]:
        self._call("tool_ids")
        return self.tools

    def create_session(self, title: str) -> dict[str, Any]:
        self._call("create_session")
        self.created.append(title)
        self.sessions.add(SESSION)
        return {"id": SESSION, "directory": self.directory, "permission": session_rules()}

    def get_session(self, session_id: str) -> dict[str, Any] | None:
        self._call("get_session")
        return {"id": session_id} if session_id in self.sessions else None

    def prompt_async(self, session_id: str, body: dict[str, Any]) -> int | None:
        return self.scene.engine.submit(session_id, body)

    def statuses(self) -> dict[str, Any]:
        self._call("statuses")
        return dict(self._read(self.scene.engine.statuses))

    def messages(self, session_id: str) -> list[Any]:
        self._call("messages")
        return list(self._read(lambda: self.scene.engine.messages(session_id)))

    def abort(self, session_id: str) -> None:
        self._read(lambda: self.scene.engine.abort(session_id))

    def reject_permission(self, contract: Any, session_id: str, request_id: str) -> None:
        self._call("reject_permission")
        self.rejected.append(("permission", request_id))

    def reject_question(self, contract: Any, request_id: str) -> None:
        self._call("reject_question")
        self.rejected.append(("question", request_id))

    @contextmanager
    def events(self, *, read_timeout: float) -> Iterator[Iterator[dict[str, Any]]]:
        self._call("events")
        if not self.streams:
            self.scene.sleep(read_timeout)
            raise opencode._Quiet()
        stream = self.streams.pop(0)
        yield iter(_after_the_prompt(stream))

    def _read(self, call: Callable[[], Any]) -> Any:
        try:
            return call()
        except ControlError as exc:
            raise OpenCodeError(CodingStatus.UNAVAILABLE, str(exc)) from exc


def _after_the_prompt(stream: Stream) -> Iterator[dict[str, Any]]:
    # Generators run only when read, so what `stream` does happens after the adapter sent the prompt.
    yield from stream()


def _quiet() -> list[dict[str, Any]]:
    return []


def _idle_event() -> dict[str, Any]:
    return {"type": "session.idle", "properties": {"sessionID": SESSION}}


def _idle(scene: Scene) -> Stream:
    def stream() -> list[dict[str, Any]]:
        scene.engine.finish_work()
        return [_idle_event()]

    return stream


def _tool_event(name: str, status: str = "running", metadata: dict[str, Any] | None = None, **inputs: Any) -> dict[str, Any]:
    state: dict[str, Any] = {"status": status, "input": inputs}
    if metadata is not None:
        state["metadata"] = metadata
    part = {"id": "prt_1", "sessionID": SESSION, "messageID": "msg_a", "type": "tool", "callID": "call_1", "tool": name, "state": state}
    return {"type": "message.part.updated", "properties": {"sessionID": SESSION, "part": part, "time": 1}}


def _patch(*lines: str) -> str:
    return "\n".join(("*** Begin Patch", *lines, "*** End Patch"))


def _task(**changes: Any) -> CodingTask:
    values: dict[str, Any] = {"key": "run_1:1", "title": "t", "brief": "do it", "session_id": None, "abort_first": False,
                              "max_tokens": 1000, "timeout_seconds": TIMEOUT}  # fmt: skip
    return CodingTask(**{**values, **changes})


def _provider(scene: Scene, api: FakeApi) -> OpenCodeProvider:
    return OpenCodeProvider(api, model="openai/m", poll_seconds=40.0, clock=scene.clock)


def _run(
    scene: Scene, api: FakeApi, *, task: CodingTask | None = None, cancel: threading.Event | None = None, guard: Any = None
) -> tuple[CodingOutcome, Any]:
    guard = guard or scene.guard()
    outcome = _provider(scene, api).run_task(task or _task(), cancel=cancel, progress=lambda *_: None, guard=guard)
    return outcome, guard


def _stopped_for_policy(scene: Scene, *events: dict[str, Any]) -> tuple[CodingOutcome, Any]:
    api = FakeApi(scene)
    api.streams = [lambda: list(events)]
    return _run(scene, api)


def _assert_nothing_sent(scene: Scene, api: FakeApi) -> None:
    assert scene.engine.prompts == [] and scene.log == [] and api.created == []


# --- Before anything is sent: valid and invalid servers ------------------------------------------


def test_a_valid_server_gets_exactly_one_prompt_with_the_recorded_id() -> None:
    scene = Scene()
    api = FakeApi(scene)
    api.streams = [_idle(scene)]
    outcome, guard = _run(scene, api)
    assert outcome.status is CodingStatus.COMPLETED and guard.phase is P.FINISHED
    assert len(scene.engine.prompts) == 1 and scene.engine.prompts[0]["messageID"] == guard.record.message_id
    assert scene.log == ["submit"]  # nothing was stopped
    assert outcome.stop is not None and outcome.stop.phase is P.FINISHED and outcome.engine_version == "1.19.5"


def test_the_checks_run_in_order_and_all_before_a_session_exists() -> None:
    scene = Scene()
    api = FakeApi(scene)
    api.streams = [_idle(scene)]
    _run(scene, api)
    assert api.calls[:6] == ["filter_health", "health", "spec", "agents", "config", "tool_ids"]
    assert api.calls[6] == "create_session"


def test_the_prompt_carries_no_tools_field_and_names_the_build_agent() -> None:
    # On 1.19.5 `tools` replaces the session's whole rule list (SessionPrompt.prompt, read from the binary).
    scene = Scene()
    api = FakeApi(scene)
    api.streams = [_idle(scene)]
    _run(scene, api)
    body = scene.engine.prompts[0]
    assert set(body) == {"agent", "model", "parts", "messageID"}
    assert body["agent"] == "build" and body["model"] == {"providerID": "openai", "modelID": "m"}
    assert body["parts"] == [{"type": "text", "text": "do it"}]


def test_a_server_without_client_ids_gets_none_and_is_matched_by_what_is_new() -> None:
    scene = Scene()
    api = FakeApi(scene, message_id=False)
    api.streams = [_idle(scene)]
    outcome, guard = _run(scene, api)
    assert "messageID" not in scene.engine.prompts[0] and guard.record.message_id is None
    assert outcome.status is CodingStatus.COMPLETED


def test_a_bare_server_is_refused_before_anything_else() -> None:
    # The real server answers an unknown path with 200 and its web page: exactly this error.
    scene = Scene()
    api = FakeApi(scene)
    api.failures["filter_health"] = _html_page("GET /devpilot/health")
    outcome, guard = _run(scene, api)
    assert outcome.status is CodingStatus.INCOMPATIBLE and "no API filter" in (outcome.detail or "")
    assert api.calls == ["filter_health"] and guard.phase is P.ABANDONED
    _assert_nothing_sent(scene, api)


@pytest.mark.parametrize("step", ["config", "agents", "tool_ids", "health", "spec"])
def test_html_for_any_check_stops_the_task(step: str) -> None:
    scene = Scene()
    api = FakeApi(scene)
    api.failures[step] = _html_page(f"GET /{step}")
    outcome, guard = _run(scene, api)
    assert outcome.status is CodingStatus.PROTOCOL and "text/html" in (outcome.detail or "")
    assert guard.phase is P.ABANDONED
    _assert_nothing_sent(scene, api)


def test_an_unexpected_content_type_stops_the_task() -> None:
    scene = Scene()
    api = FakeApi(scene)
    try:
        _checked(json_answer, 200, "text/plain", b"{}", "GET /agent")
    except OpenCodeError as exc:
        api.failures["agents"] = exc
    outcome, _ = _run(scene, api)
    assert outcome.status is CodingStatus.PROTOCOL and "text/plain" in (outcome.detail or "")
    _assert_nothing_sent(scene, api)


def _agents_without_build() -> list[dict[str, Any]]:
    return [a for a in opencode_agents(_managed()) if a["name"] != "build"]


def _agents_with_hostile() -> list[dict[str, Any]]:
    hostile = {"name": "devpilot-hostile", "mode": "primary", "permission": [{"permission": "bash", "pattern": "*", "action": "allow"}]}
    return [*opencode_agents(_managed()), hostile]


def _agents_with_bash() -> list[dict[str, Any]]:
    return opencode_agents(_managed(), extra_rules=[{"permission": "bash", "pattern": "*", "action": "allow"}])


def _agents_with_ask() -> list[dict[str, Any]]:
    return opencode_agents(_managed(), extra_rules=[{"permission": "some_new_permission", "pattern": "*", "action": "ask"}])


@pytest.mark.parametrize(
    ("agents", "reason"),
    [
        (_agents_without_build, "build agent is missing"),
        (_agents_with_hostile, "devpilot-hostile"),
        (_agents_with_bash, "bash is not denied"),
        (_agents_with_ask, "some_new_permission"),
        (lambda: None, "were not supplied"),
        (lambda: {}, "list of named agents"),
        (lambda: [{"permission": []}], "list of named agents"),
    ],
)
def test_a_missing_or_malicious_agent_list_sends_nothing(agents: Callable[[], Any], reason: str) -> None:
    scene = Scene()
    api = FakeApi(scene)
    api.agents_doc = agents()
    outcome, guard = _run(scene, api)
    assert outcome.status is CodingStatus.INCOMPATIBLE and reason in (outcome.detail or "")
    assert guard.phase is P.ABANDONED
    _assert_nothing_sent(scene, api)


@pytest.mark.parametrize(
    ("change", "reason"),
    [
        ({"plugin": ["evil"]}, "plugins must be empty"),
        ({"lsp": True}, "lsp must be false"),
        ({"share": "auto"}, "share must be"),
        ({"provider": {"openai": {"npm": "evil-provider"}}}, "install from npm"),
    ],
)
def test_an_unsafe_configuration_sends_nothing(change: dict[str, Any], reason: str) -> None:
    scene = Scene()
    api = FakeApi(scene)
    api.config_doc = {**_managed(), **change}
    outcome, _ = _run(scene, api)
    assert outcome.status is CodingStatus.INCOMPATIBLE and reason in (outcome.detail or "")
    _assert_nothing_sent(scene, api)


def test_a_configuration_that_is_not_an_object_sends_nothing() -> None:
    scene = Scene()
    api = FakeApi(scene)
    api.config_doc = None
    outcome, _ = _run(scene, api)
    assert outcome.status is CodingStatus.INCOMPATIBLE
    _assert_nothing_sent(scene, api)


def test_an_unreviewed_tool_on_the_server_sends_nothing() -> None:
    scene = Scene()
    api = FakeApi(scene)
    api.tools = [*OPENCODE_1_19_5_TOOLS, "pty_exec"]
    outcome, _ = _run(scene, api)
    assert outcome.status is CodingStatus.INCOMPATIBLE and "pty_exec" in (outcome.detail or "")
    _assert_nothing_sent(scene, api)


@pytest.mark.parametrize("version", ["1.19.4", "1.19.6"])
def test_another_release_sends_nothing_even_inside_the_supported_range(version: str) -> None:
    scene = Scene()
    api = FakeApi(scene)
    api.version = version
    outcome, _ = _run(scene, api)
    assert outcome.status is CodingStatus.INCOMPATIBLE and "have not been reviewed" in (outcome.detail or "")
    _assert_nothing_sent(scene, api)


def test_a_spec_lacking_an_operation_sends_nothing() -> None:
    scene = Scene()
    api = FakeApi(scene)
    del api.spec_doc["paths"]["/agent"]
    outcome, _ = _run(scene, api)
    assert outcome.status is CodingStatus.INCOMPATIBLE and "GET /agent" in (outcome.detail or "")
    _assert_nothing_sent(scene, api)


def test_an_old_or_cancelled_attempt_never_touches_the_server_again() -> None:
    scene = Scene()
    api = FakeApi(scene)
    api.streams = [_idle(scene)]
    _, guard = _run(scene, api)
    api.calls.clear()
    log = list(scene.log)
    outcome, _ = _run(scene, api, guard=guard)
    assert outcome.status is CodingStatus.CANCELLED and api.calls == [] and scene.log == log


# --- Sessions ------------------------------------------------------------------------------------


def test_a_missing_session_is_replaced_and_says_so() -> None:
    scene = Scene()
    api = FakeApi(scene)
    api.sessions.clear()
    api.streams = [_idle(scene)]
    events: list[str] = []
    guard = scene.guard()
    outcome = _provider(scene, api).run_task(_task(session_id="ses_gone"), cancel=None, progress=lambda e, _: events.append(e), guard=guard)
    assert outcome.status is CodingStatus.COMPLETED and "coding.session_replaced" in events and len(api.created) == 1


def test_a_session_that_still_exists_is_continued() -> None:
    scene = Scene()
    api = FakeApi(scene)
    api.streams = [_idle(scene)]
    _run(scene, api, task=_task(session_id=SESSION))
    assert api.created == []


def test_a_session_that_cannot_be_created_closes_the_attempt() -> None:
    scene = Scene()
    api = FakeApi(scene)
    api.failures["create_session"] = OpenCodeError(CodingStatus.AUTH, "refused")
    outcome, guard = _run(scene, api)
    assert outcome.status is CodingStatus.AUTH and guard.phase is P.ABANDONED and guard.record.failure_reason == "refused"


def test_a_session_that_vanishes_after_the_prompt_is_stopped_not_left_running() -> None:
    scene = Scene()
    api = FakeApi(scene)
    original = api.messages

    def gone(session_id: str) -> list[Any]:
        if scene.engine.prompts:
            raise OpenCodeNotFound("GET /session/ses_1/message: not found")
        return original(session_id)

    def vanished() -> list[dict[str, Any]]:
        scene.engine.state = None  # no longer listed as working, so its messages are read: they are gone
        return []

    api.messages = gone  # type: ignore[method-assign]
    api.streams = [vanished]
    outcome, guard = _run(scene, api)
    assert outcome.status is CodingStatus.PROTOCOL and scene.log == ["submit", "abort", "terminate"]
    assert guard.phase is P.SAFE_TO_RESTORE


# --- Ambiguous submission and reconciliation -------------------------------------------------------


def test_a_timeout_on_the_send_is_stopped_and_never_resent() -> None:
    scene = Scene(submit_status=None, delivered=True)
    api = FakeApi(scene)
    api.streams = [_quiet, _quiet, _quiet]
    outcome, guard = _run(scene, api)
    assert len(scene.engine.prompts) == 1
    assert outcome.status is CodingStatus.UNAVAILABLE and guard.phase is P.SAFE_TO_RESTORE
    assert "ambiguous" in (outcome.detail or "") and "the container is confirmed stopped" in (outcome.detail or "")
    assert scene.log == ["submit", "abort", "terminate"]


@pytest.mark.parametrize("status", [None, 200, 202, 302, 408, 429, 500, 502, 503])
def test_every_uncertain_answer_takes_the_same_path(status: int | None) -> None:
    scene = Scene(submit_status=status, delivered=False)
    api = FakeApi(scene)
    api.streams = [_quiet]
    outcome, guard = _run(scene, api)
    assert outcome.status is CodingStatus.UNAVAILABLE and guard.phase is P.SAFE_TO_RESTORE and len(scene.engine.prompts) == 1


def test_the_exact_message_found_after_an_ambiguous_send_is_reconciled_as_started() -> None:
    scene = Scene(submit_status=None, delivered=True)
    api = FakeApi(scene)
    api.streams = [_quiet]
    _, guard = _run(scene, api)
    assert guard.record.reconciliation == "STARTED"
    assert guard.record.message_id == scene.engine.prompts[0]["messageID"]
    assert "the prompt had started" in (guard.record.failure_reason or "")


def test_a_message_that_may_still_arrive_is_not_called_absent() -> None:
    scene = Scene(submit_status=None, delivered=True, start_delay=1)
    api = FakeApi(scene)
    api.streams = [_quiet]
    outcome, guard = _run(scene, api)
    assert guard.record.reconciliation == "NOT_SEEN" and "may still start" in (outcome.detail or "")
    assert not scene.engine.working and guard.phase is P.SAFE_TO_RESTORE


def test_a_refused_prompt_stops_nothing() -> None:
    scene = Scene(submit_status=400, delivered=False)
    api = FakeApi(scene)
    api.streams = [_quiet]
    outcome, guard = _run(scene, api)
    assert outcome.status is CodingStatus.PROTOCOL and guard.phase is P.REJECTED
    assert scene.log == ["submit"] and "refused the prompt (400)" in (outcome.detail or "")


def test_an_event_stream_that_cannot_open_sends_nothing() -> None:
    scene = Scene()
    api = FakeApi(scene)
    api.failures["events"] = OpenCodeError(CodingStatus.UNAVAILABLE, "no stream")
    outcome, guard = _run(scene, api)
    assert outcome.status is CodingStatus.UNAVAILABLE and guard.phase is P.ABANDONED and scene.engine.prompts == []


def test_reconnecting_never_sends_a_second_prompt() -> None:
    scene = Scene()
    api = FakeApi(scene)

    def dropped() -> list[dict[str, Any]]:
        raise OpenCodeError(CodingStatus.UNAVAILABLE, "the stream dropped")

    api.streams = [dropped, dropped, _idle(scene)]
    outcome, _ = _run(scene, api)
    assert outcome.status is CodingStatus.COMPLETED and outcome.reconnects == 2 and len(scene.engine.prompts) == 1


def test_too_many_reconnects_stop_the_attempt_safely() -> None:
    scene = Scene()
    api = FakeApi(scene)

    def dropped() -> list[dict[str, Any]]:
        raise OpenCodeError(CodingStatus.UNAVAILABLE, "the stream dropped")

    api.streams = [dropped] * 7
    outcome, guard = _run(scene, api)
    assert outcome.status is CodingStatus.UNAVAILABLE and guard.phase is P.SAFE_TO_RESTORE and scene.log[-1] == "terminate"


# --- Completion is by relationship ----------------------------------------------------------------


def test_an_idle_event_before_the_reply_finishes_does_not_end_the_task() -> None:
    scene = Scene()
    api = FakeApi(scene)
    scene.engine.abort_stops = False
    api.streams = [lambda: [_idle_event()], lambda: [{"type": "session.status", "properties": {"sessionID": SESSION, "status": {"type": "idle"}}}]]
    outcome, _ = _run(scene, api)
    assert outcome.status is CodingStatus.TIMEOUT


def test_a_reply_that_asked_for_more_tools_has_not_finished() -> None:
    scene = Scene()
    api = FakeApi(scene)

    def tool_calls() -> list[dict[str, Any]]:
        scene.engine.finish_work()
        scene.engine.entries[-1]["info"]["finish"] = "tool-calls"
        return [_idle_event()]

    api.streams = [tool_calls]
    outcome, _ = _run(scene, api)
    assert outcome.status is CodingStatus.TIMEOUT


def test_a_reply_to_another_prompt_never_finishes_this_task() -> None:
    scene = Scene()
    api = FakeApi(scene)

    def other_parent() -> list[dict[str, Any]]:
        scene.engine.finish_work()
        scene.engine.entries[-1]["info"]["parentID"] = "msg_someone_else"
        return [_idle_event()]

    api.streams = [other_parent]
    outcome, _ = _run(scene, api)
    assert outcome.status is CodingStatus.TIMEOUT


def test_a_finished_reply_left_by_an_earlier_attempt_does_not_finish_this_one() -> None:
    scene = Scene()
    old_user = {"info": {"id": "msg_old", "sessionID": SESSION, "role": "user", "time": {"created": 0}}, "parts": []}
    old_reply = {"info": {"id": "msg_old_r", "sessionID": SESSION, "role": "assistant", "parentID": "msg_old", "finish": "stop",
                          "time": {"created": 0, "completed": 1}, "tokens": {"input": 999999, "output": 0}}, "parts": []}  # fmt: skip
    scene.engine.entries = [old_user, old_reply]
    scene.engine.delivered = False  # the new prompt never produces anything
    scene.engine.abort_stops = False
    api = FakeApi(scene)
    api.streams = [lambda: [_idle_event(), {"type": "message.updated", "properties": {"sessionID": SESSION, "info": old_reply["info"]}}]]
    outcome, _ = _run(scene, api, task=_task(session_id=SESSION))
    # Not finished by the old reply, and the old reply's tokens are not charged to this task.
    assert outcome.status is CodingStatus.TIMEOUT and outcome.tokens_in == 0


def test_an_error_ends_the_turn_as_a_failure() -> None:
    scene = Scene()
    api = FakeApi(scene)

    def failed() -> list[dict[str, Any]]:
        scene.engine.finish_work(error=True)
        return [_idle_event()]

    api.streams = [failed]
    outcome, guard = _run(scene, api)
    assert outcome.status is CodingStatus.FAILED and "MessageAbortedError" in (outcome.detail or "") and guard.phase is P.FINISHED


# --- Delayed events ---------------------------------------------------------------------------


def test_events_about_another_session_are_ignored() -> None:
    scene = Scene()
    api = FakeApi(scene)
    other = _tool_event("bash")
    other["properties"]["sessionID"] = "ses_other"
    other["properties"]["part"]["sessionID"] = "ses_other"

    def stream() -> list[dict[str, Any]]:
        scene.engine.finish_work()
        return [other, _idle_event()]

    api.streams = [stream]
    outcome, _ = _run(scene, api)
    assert outcome.status is CodingStatus.COMPLETED and outcome.tools_used == []


def test_a_finish_that_arrives_after_the_stop_cannot_turn_it_into_a_success() -> None:
    scene = Scene()
    api = FakeApi(scene)
    guard = scene.guard()

    def stopped_elsewhere() -> list[dict[str, Any]]:
        guard.secure(_Control(api), "the run was cancelled while the last event was on its way")
        scene.engine.finish_work()
        return [_idle_event()]

    api.streams = [stopped_elsewhere]
    outcome, _ = _run(scene, api, guard=guard)
    assert outcome.status is CodingStatus.CANCELLED and guard.phase is P.SAFE_TO_RESTORE
    assert guard.record.late_results == 1 and "cancelled" in (outcome.detail or "")


# --- Cancellation and every other stop use one policy ---------------------------------------------


def _busy_forever(scene: Scene, api: FakeApi) -> None:
    scene.engine.abort_stops = False
    api.streams = [_quiet]


def test_a_timeout_stops_the_container_before_the_outcome_is_returned() -> None:
    scene = Scene()
    api = FakeApi(scene)
    _busy_forever(scene, api)
    outcome, guard = _run(scene, api)
    assert outcome.status is CodingStatus.TIMEOUT and "no result within 100 seconds" in (outcome.detail or "")
    assert scene.log == ["submit", "abort", "terminate"] and guard.phase is P.SAFE_TO_RESTORE
    assert outcome.stop is not None and outcome.stop.process_terminated and not outcome.stop.observed_quiet


def test_a_run_cancelled_before_the_send_sends_nothing() -> None:
    scene = Scene()
    api = FakeApi(scene)
    cancel = threading.Event()
    cancel.set()
    outcome, guard = _run(scene, api, cancel=cancel)
    assert outcome.status is CodingStatus.CANCELLED and guard.phase is P.ABANDONED
    assert scene.engine.prompts == [] and scene.log == [] and "nothing was sent" in (outcome.detail or "")


def _stop_steps(cause: str) -> tuple[list[str], list[str], CodingOutcome]:
    scene = Scene(abort_stops=False)
    api = FakeApi(scene)
    cancel = threading.Event()
    if cause == "timeout":
        api.streams = [_quiet]
    elif cause == "cancel":
        api.streams = [lambda: (cancel.set(), [{"type": "noise", "properties": {}}])[1]]
    elif cause == "violation":
        api.streams = [lambda: [_tool_event("bash")]]
    elif cause == "budget":
        info = {"id": "m1", "role": "assistant", "sessionID": SESSION, "tokens": {"input": 5000, "output": 1}}
        api.streams = [lambda: [{"type": "message.updated", "properties": {"info": info}}]]
    elif cause == "failure":
        original = api.statuses

        def fails_once_running() -> dict[str, Any]:
            if scene.engine.prompts and not scene.log.count("abort"):
                raise OpenCodeError(CodingStatus.PROTOCOL, "the server answered nonsense")
            return original()

        api.statuses = fails_once_running  # type: ignore[method-assign]
        api.streams = [_quiet]
    outcome, _ = _run(scene, api, cancel=cancel)
    return scene.log, scene.phases(), outcome


@pytest.mark.parametrize(
    ("cause", "status"),
    [("timeout", CodingStatus.TIMEOUT), ("cancel", CodingStatus.CANCELLED), ("violation", CodingStatus.POLICY_VIOLATION),
     ("budget", CodingStatus.LIMIT), ("failure", CodingStatus.PROTOCOL)],
)  # fmt: skip
def test_every_cause_of_stopping_takes_the_same_steps(cause: str, status: CodingStatus) -> None:
    log, phases, outcome = _stop_steps(cause)
    assert outcome.status is status
    assert log == ["submit", "abort", "terminate"]
    assert phases == ["SUBMITTED", "RUNNING", "ABORT_REQUESTED", "QUIESCENCE_CHECK", "SAFE_TO_RESTORE"]


def test_a_bug_in_the_adapter_still_stops_the_writer_and_is_not_hidden() -> None:
    scene = Scene()
    api = FakeApi(scene)

    def bug() -> list[dict[str, Any]]:
        raise ZeroDivisionError("a bug")

    api.streams = [bug]
    guard = scene.guard()
    with pytest.raises(ZeroDivisionError):
        _provider(scene, api).run_task(_task(), cancel=None, progress=lambda *_: None, guard=guard)
    assert guard.phase is P.SAFE_TO_RESTORE and scene.log == ["submit", "abort", "terminate"]


# --- Tools ----------------------------------------------------------------------------------------


@pytest.mark.parametrize("tool", ["bash", "webfetch", "websearch", "task", "skill", "question", "lsp", "codesearch", "pty_exec", "github_mcp_tool", "brand_new_tool"])
def test_a_tool_outside_the_allowlist_stops_the_attempt_safely(tool: str) -> None:
    scene = Scene()
    outcome, guard = _stopped_for_policy(scene, _tool_event(tool))
    assert outcome.status is CodingStatus.POLICY_VIOLATION and guard.phase is P.SAFE_TO_RESTORE
    assert tool in (guard.record.failure_reason or "") and scene.log == ["submit", "abort", "terminate"]


def test_the_session_next_form_of_a_tool_call_is_judged_too() -> None:
    scene = Scene()
    event = {"type": "session.next.tool.called", "properties": {"sessionID": SESSION, "callID": "c1", "tool": "bash", "input": {"command": "id"}}}
    outcome, _ = _stopped_for_policy(scene, event)
    assert outcome.status is CodingStatus.POLICY_VIOLATION


def test_asking_for_a_hidden_tool_does_not_stop_the_task() -> None:
    scene = Scene()
    api = FakeApi(scene)

    def stream() -> list[dict[str, Any]]:
        scene.engine.finish_work()
        return [_tool_event("bash", "pending"), _tool_event("invalid", "completed", tool="bash", error="no such tool"), _idle_event()]

    api.streams = [stream]
    outcome, _ = _run(scene, api)
    assert outcome.status is CodingStatus.COMPLETED and outcome.tools_used == ["bash", "invalid"]


@pytest.mark.parametrize(
    "event",
    [
        {"type": "pty.created", "properties": {"info": {"id": "pty_1"}}},
        {"type": "pty.exited", "properties": {"id": "pty_1", "exitCode": 0}},
        {"type": "mcp.tools.changed", "properties": {"server": "x"}},
    ],
)
def test_a_terminal_or_an_mcp_server_stops_the_attempt(event: dict[str, Any]) -> None:
    scene = Scene()
    outcome, guard = _stopped_for_policy(scene, event)
    assert outcome.status is CodingStatus.POLICY_VIOLATION and guard.phase is P.SAFE_TO_RESTORE


def test_an_event_that_could_not_be_read_stops_the_attempt() -> None:
    for kind in ("devpilot.oversized_event", "devpilot.invalid_event"):
        scene = Scene()
        outcome, guard = _stopped_for_policy(scene, {"type": kind})
        assert outcome.status is CodingStatus.POLICY_VIOLATION and "could not be read" in (guard.record.failure_reason or "")


@pytest.mark.parametrize("kind", ["permission.asked", "permission.v2.asked"])
@pytest.mark.parametrize("name", ["bash", "edit", "some_future_permission"])
def test_any_permission_request_is_rejected_and_stops_the_attempt(kind: str, name: str) -> None:
    scene = Scene()
    api = FakeApi(scene)
    props = {"id": "per_1", "sessionID": SESSION, ("permission" if kind == "permission.asked" else "action"): name, "patterns": ["*"]}
    api.streams = [lambda: [{"type": kind, "properties": props}]]
    outcome, guard = _run(scene, api)
    assert api.rejected == [("permission", "per_1")]
    assert outcome.status is CodingStatus.POLICY_VIOLATION and outcome.permission_rejections == [name]
    assert name in (guard.record.failure_reason or "") and guard.phase is P.SAFE_TO_RESTORE


def test_a_permission_request_that_cannot_be_rejected_stops_the_attempt() -> None:
    scene = Scene()
    api = FakeApi(scene)
    api.failures["reject_permission"] = OpenCodeError(CodingStatus.UNAVAILABLE, "no answer")
    api.streams = [lambda: [{"type": "permission.asked", "properties": {"id": "per_1", "sessionID": SESSION, "permission": "edit"}}]]
    outcome, guard = _run(scene, api)
    assert outcome.status is CodingStatus.POLICY_VIOLATION and "could not be rejected" in (guard.record.failure_reason or "")


@pytest.mark.parametrize("kind", ["question.asked", "question.v2.asked"])
def test_a_question_is_rejected_and_stops_the_attempt(kind: str) -> None:
    scene = Scene()
    api = FakeApi(scene)
    api.streams = [lambda: [{"type": kind, "properties": {"id": "que_1", "sessionID": SESSION, "questions": []}}]]
    outcome, _ = _run(scene, api)
    assert api.rejected == [("question", "que_1")] and outcome.status is CodingStatus.POLICY_VIOLATION


def test_a_question_that_cannot_be_rejected_stops_the_attempt() -> None:
    scene = Scene()
    api = FakeApi(scene)
    api.failures["reject_question"] = OpenCodeError(CodingStatus.UNAVAILABLE, "no answer")
    api.streams = [lambda: [{"type": "question.asked", "properties": {"id": "que_1", "sessionID": SESSION}}]]
    outcome, _ = _run(scene, api)
    assert outcome.status is CodingStatus.POLICY_VIOLATION


def test_apply_patch_with_several_ordinary_files_runs() -> None:
    scene = Scene()
    api = FakeApi(scene)
    text = _patch("*** Add File: a.py", "+x", "*** Update File: b.py", "@@", "-a", "+b",
                  "*** Update File: c.py", "*** Move to: d.py", "@@", "-a", "+b", "*** Delete File: e.py")  # fmt: skip

    def stream() -> list[dict[str, Any]]:
        scene.engine.finish_work()
        return [_tool_event("apply_patch", patchText=text), _idle_event()]

    api.streams = [stream]
    outcome, _ = _run(scene, api)
    assert outcome.status is CodingStatus.COMPLETED and outcome.tools_used == ["apply_patch"]


@pytest.mark.parametrize(
    "text",
    [
        _patch("*** Add File: a.py", "+x", "*** Add File: .opencode/plugin/x.js", "+x"),
        _patch("*** Update File: a.py", "@@", "-a", "+b", "*** Delete File: opencode.json"),
        _patch("*** Add File: a.py", "+x", "*** Update File: b.py", "*** Move to: ../outside.py", "@@", "-a", "+b"),
    ],
)
def test_one_bad_path_in_a_many_file_patch_stops_the_attempt(text: str) -> None:
    scene = Scene()
    outcome, guard = _stopped_for_policy(scene, _tool_event("apply_patch", patchText=text))
    assert outcome.status is CodingStatus.POLICY_VIOLATION and guard.phase is P.SAFE_TO_RESTORE


@pytest.mark.parametrize("destination", ["opencode.json", ".opencode/agent/x.md", "AGENTS.md", ".git/hooks/pre-commit", ".env"])
def test_a_protected_move_destination_stops_the_attempt_though_opencode_permits_it(destination: str) -> None:
    scene = Scene()
    text = _patch("*** Update File: src.py", f"*** Move to: {destination}", "@@", "-a", "+b")
    outcome, guard = _stopped_for_policy(scene, _tool_event("apply_patch", patchText=text))
    assert outcome.status is CodingStatus.POLICY_VIOLATION and "(move)" in (guard.record.failure_reason or "")
    with pytest.raises(RestoreForbidden):
        scene.reopened(_at(scene, P.QUIESCENCE_CHECK)).restore(scene.checkpoint, ROOT)  # never before termination
    assert guard.restore(scene.checkpoint, ROOT) == 3
    assert scene.log == ["submit", "abort", "terminate", "restore", "compare"]


def _at(scene: Scene, phase: AttemptPhase) -> Any:
    record = scene.store.only()
    record.phase = phase
    return record


def test_a_write_outside_the_project_stops_the_attempt() -> None:
    scene = Scene()
    outcome, guard = _stopped_for_policy(scene, _tool_event("write", filePath="/etc/cron.d/x", content="x"))
    assert outcome.status is CodingStatus.POLICY_VIOLATION and "outside the project" in (guard.record.failure_reason or "")


@pytest.mark.parametrize(("path", "reason"), [("/etc/passwd", "outside the project"), ("/workspace/.env", "protected path"), ("opencode.json", "protected path")])
def test_a_reported_file_edit_is_judged_too(path: str, reason: str) -> None:
    scene = Scene()
    outcome, guard = _stopped_for_policy(scene, {"type": "file.edited", "properties": {"file": path}})
    assert outcome.status is CodingStatus.POLICY_VIOLATION and reason in (guard.record.failure_reason or "")


def test_reported_edits_are_listed_relative_to_the_project() -> None:
    scene = Scene()
    api = FakeApi(scene)

    def stream() -> list[dict[str, Any]]:
        scene.engine.finish_work()
        return [{"type": "file.edited", "properties": {"file": "/workspace/src/a.py"}}, {"type": "file.edited", "properties": {"file": "src/a.py"}}, _idle_event()]

    api.streams = [stream]
    outcome, _ = _run(scene, api)
    assert outcome.files_reported == ["src/a.py"]


def test_a_forbidden_tool_only_the_final_record_shows_stops_the_attempt() -> None:
    # The event was missed. The session's own record of its messages has the last word.
    scene = Scene()
    api = FakeApi(scene)

    def stream() -> list[dict[str, Any]]:
        scene.engine.finish_work()
        scene.engine.entries[-1]["parts"].append(_tool_event("bash", "completed")["properties"]["part"])
        return [_idle_event()]

    api.streams = [stream]
    outcome, guard = _run(scene, api)
    assert outcome.status is CodingStatus.POLICY_VIOLATION and guard.phase is P.SAFE_TO_RESTORE
    assert scene.log == ["submit", "abort", "terminate"]


def test_a_budget_only_the_final_record_shows_stops_the_attempt() -> None:
    scene = Scene()
    api = FakeApi(scene)

    def stream() -> list[dict[str, Any]]:
        scene.engine.finish_work()
        scene.engine.entries[-1]["info"]["tokens"] = {"input": 5000, "output": 1, "reasoning": 0, "cache": {"read": 0, "write": 0}}
        return [_idle_event()]

    api.streams = [stream]
    outcome, guard = _run(scene, api)
    assert outcome.status is CodingStatus.LIMIT and guard.phase is P.SAFE_TO_RESTORE


def test_token_use_and_cost_are_reported() -> None:
    scene = Scene()
    api = FakeApi(scene)

    def stream() -> list[dict[str, Any]]:
        scene.engine.finish_work()
        info = scene.engine.entries[-1]["info"]
        info["tokens"] = {"input": 10, "output": 5, "reasoning": 2, "cache": {"read": 3, "write": 1}}
        info["cost"] = 0.25
        return [_idle_event()]

    api.streams = [stream]
    outcome, _ = _run(scene, api)
    assert (outcome.tokens_in, outcome.tokens_out, outcome.cost_usd) == (14, 7, 0.25)


# --- Abort failure, stop failure, quarantine ---------------------------------------------------------


def test_an_abort_that_fails_is_retried_and_the_stop_goes_on() -> None:
    scene = Scene(abort_failures=99)
    api = FakeApi(scene)
    api.streams = [lambda: [_tool_event("bash")]]
    outcome, guard = _run(scene, api)
    assert scene.engine.aborts == 3 and "the abort was not acknowledged" in (outcome.detail or "")
    assert outcome.stop is not None and not outcome.stop.abort_acknowledged and outcome.stop.process_terminated
    assert guard.phase is P.SAFE_TO_RESTORE


def test_a_stop_that_cannot_be_confirmed_quarantines_with_the_original_reason() -> None:
    scene = Scene()
    scene.writer.confirms = False
    api = FakeApi(scene)
    _busy_forever(scene, api)
    outcome, guard = _run(scene, api)
    assert outcome.status is CodingStatus.TIMEOUT  # the cause stays the cause
    assert outcome.stop is not None and outcome.stop.quarantined and guard.phase is P.QUARANTINED
    assert "quarantined" in (outcome.detail or "") and guard.record.failure_reason == "no result within 100 seconds"


def test_a_stop_that_raises_quarantines() -> None:
    scene = Scene()
    scene.writer.raises = True
    outcome, guard = _stopped_for_policy(scene, _tool_event("bash"))
    assert outcome.stop is not None and outcome.stop.quarantined and guard.phase is P.QUARANTINED


def test_a_quarantined_attempt_is_never_restored_and_holds_the_project() -> None:
    scene = Scene()
    scene.writer.confirms = False
    _, guard = _stopped_for_policy(scene, _tool_event("bash"))
    with pytest.raises(RestoreForbidden):
        guard.restore(scene.checkpoint, ROOT)
    assert "restore" not in scene.log
    with pytest.raises(AttemptRefused, match="held"):
        scene.guard(call_id="call_2")
    assert scene.reopened().recover(_Control(FakeApi(scene))).quarantined


# --- Crash recovery and idempotency ---------------------------------------------------------------


def _crashed(scene: Scene, api: FakeApi) -> None:
    def crash() -> list[dict[str, Any]]:
        raise KeyboardInterrupt  # the process died while the engine was working

    api.streams = [crash]
    with pytest.raises(KeyboardInterrupt):
        _run(scene, api)


def test_a_backend_crash_leaves_a_record_that_finishes_the_stop() -> None:
    scene = Scene(abort_stops=False)
    api = FakeApi(scene)
    _crashed(scene, api)
    assert scene.store.only().phase is P.RUNNING and scene.engine.working

    report = scene.reopened().recover(_Control(api))

    assert report.safe_to_restore and not scene.engine.working
    assert len(scene.engine.prompts) == 1  # never a second prompt
    assert scene.log == ["submit", "abort", "terminate"]


def test_recovery_is_idempotent_through_to_the_verified_restore() -> None:
    scene = Scene()
    api = FakeApi(scene)
    control = _Control(api)
    _crashed(scene, api)
    assert scene.reopened().recover(control).safe_to_restore
    assert scene.reopened().recover(control).safe_to_restore  # a second restart reconfirms and changes nothing
    guard = scene.reopened()
    guard.restore(scene.checkpoint, ROOT)
    assert scene.store.only().phase is P.RESTORED
    log = list(scene.log)
    assert scene.reopened().recover(control).phase is P.RESTORED
    assert scene.log == log and len(scene.engine.prompts) == 1


def test_a_recovered_attempt_cannot_be_run_again() -> None:
    scene = Scene()
    api = FakeApi(scene)
    _crashed(scene, api)
    guard = scene.reopened()
    guard.recover(_Control(api))
    outcome, _ = _run(scene, api, guard=guard)
    assert outcome.status is CodingStatus.CANCELLED and len(scene.engine.prompts) == 1


def test_recovery_with_the_server_gone_still_ends_the_container() -> None:
    scene = Scene()
    api = FakeApi(scene)
    _crashed(scene, api)
    scene.engine.unreadable = True
    scene.engine.abort_failures = 99
    report = scene.reopened().recover(_Control(api))
    assert report.safe_to_restore and not report.abort_acknowledged and report.process_terminated


# --- abort_first ------------------------------------------------------------------------------------


def test_abort_first_waits_for_quiet_and_then_sends_one_prompt() -> None:
    scene = Scene()
    api = FakeApi(scene)
    api.streams = [_idle(scene)]
    outcome, _ = _run(scene, api, task=_task(session_id=SESSION, abort_first=True))
    assert outcome.status is CodingStatus.COMPLETED and scene.log == ["abort", "submit"]


def test_abort_first_on_a_session_that_will_not_stop_sends_nothing() -> None:
    scene = Scene()
    scene.engine.state = "busy"
    outcome, guard = _run(scene, FakeApi(scene), task=_task(session_id=SESSION, abort_first=True))
    assert outcome.status is CodingStatus.BUSY and guard.phase is P.ABANDONED
    assert scene.engine.prompts == [] and "nothing was sent" in (outcome.detail or "")


def test_a_busy_session_is_never_prompted() -> None:
    scene = Scene()
    scene.engine.state = "busy"
    outcome, guard = _run(scene, FakeApi(scene), task=_task(session_id=SESSION))
    assert outcome.status is CodingStatus.BUSY and scene.engine.prompts == [] and guard.phase is P.ABANDONED
