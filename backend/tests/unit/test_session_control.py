"""A1 and A2: a prompt is sent at most once, and nothing is restored while a writer may be active.

In-memory fakes only (tests/coding_fakes.py): no HTTP, no Docker, no database, no real clock.
"""

from __future__ import annotations

import random
import re
import threading
from pathlib import Path
from typing import Any

import pytest

from app.coding.session_control import (
    _NEXT,
    BLOCKING_PHASES,
    UNRESOLVED_PHASES,
    AttemptRefused,
    LifecycleError,
    Reconciliation,
    RestoreForbidden,
    StopPolicy,
    SubmitVerdict,
    classify_submission,
    message_ids,
    new_entries,
    new_message_id,
    observe_quiescence,
    turn_finished,
    workspace_fingerprint,
)
from app.coding.types import AttemptPhase
from tests.coding_fakes import PHASES, SESSION, Scene, StoreCrash

P = AttemptPhase
ROOT = Path("/not/used")


def _stopped(scene: Scene, reason: str = "a timeout") -> Any:
    guard = scene.guard()
    assert scene.submit(guard) is SubmitVerdict.ACCEPTED
    return guard, guard.secure(scene.engine, reason)


# --- A1: classifying a submission ------------------------------------------------------------


@pytest.mark.parametrize(
    ("status", "verdict"),
    [
        (204, SubmitVerdict.ACCEPTED),
        (None, SubmitVerdict.AMBIGUOUS),  # a timeout or a dropped connection
        (200, SubmitVerdict.AMBIGUOUS),  # this server answers unknown paths with 200 and its web page
        (202, SubmitVerdict.AMBIGUOUS),
        (302, SubmitVerdict.AMBIGUOUS),
        (408, SubmitVerdict.AMBIGUOUS),
        (429, SubmitVerdict.AMBIGUOUS),
        (500, SubmitVerdict.AMBIGUOUS),
        (502, SubmitVerdict.AMBIGUOUS),
        (503, SubmitVerdict.AMBIGUOUS),
        (400, SubmitVerdict.REJECTED),
        (401, SubmitVerdict.REJECTED),
        (403, SubmitVerdict.REJECTED),
        (404, SubmitVerdict.REJECTED),
        (409, SubmitVerdict.REJECTED),
        (422, SubmitVerdict.REJECTED),
    ],
)
def test_only_a_204_is_accepted_and_only_a_plain_refusal_is_rejected(status: int | None, verdict: SubmitVerdict) -> None:
    assert classify_submission(status) is verdict


def test_a_client_message_id_has_the_servers_own_shape() -> None:
    first, second = new_message_id(1_793_347_447_981), new_message_id(1_793_347_447_981)
    assert re.fullmatch(r"msg_[0-9a-f]{12}[0-9A-Za-z]{14}", first)
    assert first != second and first[:16] == second[:16]
    assert new_message_id(1_793_347_448_981)[:16] > first[:16]  # later ids sort later


# --- A1: the prompt is sent at most once -----------------------------------------------------


def test_a_timeout_on_submit_sends_once_aborts_and_stops_the_writer() -> None:
    scene = Scene(submit_status=None, delivered=False)
    guard = scene.guard()

    assert scene.submit(guard) is SubmitVerdict.AMBIGUOUS
    report = guard.secure(scene.engine, "the submission was ambiguous")

    assert len(scene.engine.prompts) == 1
    assert scene.log[:2] == ["submit", "abort"] and scene.log[-1] == "terminate"
    assert scene.phases() == ["SUBMITTED", "AMBIGUOUS", "ABORT_REQUESTED", "QUIESCENCE_CHECK", "SAFE_TO_RESTORE"]
    assert report.safe_to_restore and report.process_terminated


def test_an_ambiguous_submit_of_a_session_that_had_started_is_reconciled_as_started() -> None:
    scene = Scene(submit_status=None, delivered=True)
    guard = scene.guard()
    assert scene.submit(guard) is SubmitVerdict.AMBIGUOUS
    assert guard.record.reconciliation == Reconciliation.STARTED
    assert "the prompt had started" in (guard.record.failure_reason or "")
    guard.secure(scene.engine, "later reason")
    assert len(scene.engine.prompts) == 1 and scene.engine.aborts == 1


def test_the_exact_message_is_found_even_when_the_session_is_idle_again() -> None:
    scene = Scene(submit_status=502, delivered=True)
    guard = scene.guard()
    scene.engine.submit = _delivered_then_finished(scene)  # type: ignore[method-assign]
    assert scene.submit(guard) is SubmitVerdict.AMBIGUOUS
    assert guard.record.reconciliation == Reconciliation.STARTED
    assert guard.record.message_id in message_ids(scene.engine.entries)


def _delivered_then_finished(scene: Scene) -> Any:
    original = type(scene.engine).submit

    def submit(session_id: str, body: dict[str, Any]) -> int | None:
        status = original(scene.engine, session_id, body)
        scene.engine.finish_work()
        return status

    return submit


def test_without_client_ids_a_new_message_counts_as_started_and_earlier_ones_do_not() -> None:
    scene = Scene(submit_status=None, delivered=False)
    scene.engine.entries.append({"info": {"id": "msg_old", "role": "user"}, "parts": []})
    guard = scene.guard()
    assert scene.submit(guard, use_message_id=False) is SubmitVerdict.AMBIGUOUS
    assert guard.record.message_id is None and "messageID" not in scene.engine.prompts[0]
    assert guard.record.reconciliation == Reconciliation.NOT_SEEN

    scene = Scene(submit_status=None, delivered=True)
    guard = scene.guard()
    scene.submit(guard, use_message_id=False)
    assert guard.record.reconciliation == Reconciliation.STARTED


def test_the_id_sent_is_the_id_recorded_and_saved_before_the_send() -> None:
    scene = Scene()
    guard = scene.guard()
    seen: list[Any] = []
    original = type(scene.engine).submit

    def submit(session_id: str, body: dict[str, Any]) -> int | None:
        seen.append((scene.store.only().phase, scene.store.only().message_id, scene.store.only().session_id))
        return original(scene.engine, session_id, body)

    scene.engine.submit = submit  # type: ignore[method-assign]
    scene.submit(guard)
    assert seen == [(P.SUBMITTED, guard.record.message_id, SESSION)]
    assert scene.engine.prompts[0]["messageID"] == guard.record.message_id


def test_a_delayed_prompt_that_wakes_after_the_abort_is_seen_and_stopped_by_termination() -> None:
    # The server took the prompt but answered nothing, and starts it only after the abort has gone by.
    scene = Scene(submit_status=None, delivered=True, start_delay=1)
    guard = scene.guard()

    assert scene.submit(guard) is SubmitVerdict.AMBIGUOUS
    assert guard.record.reconciliation == Reconciliation.NOT_SEEN  # absent from /session/status, not in the list
    assert "may still start" in (guard.record.failure_reason or "")

    report = guard.secure(scene.engine, "ambiguous")

    assert scene.engine.aborts == 1 and scene.log.index("abort") < scene.log.index("terminate")
    assert not report.observed_quiet  # it woke up and was seen working
    assert report.safe_to_restore and not scene.engine.working  # the container's end stopped it, not the abort


def test_a_delayed_prompt_can_fool_the_watch_which_is_why_the_watch_never_decides() -> None:
    # It starts after the watch has finished. Everything looked still: only termination removes it.
    scene = Scene(submit_status=None, delivered=True, start_delay=3)
    guard = scene.guard()
    scene.submit(guard)
    report = guard.secure(scene.engine, "ambiguous")
    assert report.observed_quiet and report.abort_acknowledged and not scene.engine.working
    assert report.process_terminated and report.safe_to_restore

    scene = Scene(submit_status=None, delivered=True, start_delay=3)
    scene.writer.confirms = False
    guard = scene.guard()
    scene.submit(guard)
    report = guard.secure(scene.engine, "ambiguous")
    assert report.observed_quiet and report.quarantined  # the same view, and no permission to restore
    for _ in range(3):
        scene.sleep(scene.policy.poll_seconds)
    assert scene.engine.working  # the writer the watch could not see
    with pytest.raises(RestoreForbidden):
        guard.restore(scene.checkpoint, ROOT)


def test_a_delayed_prompt_with_an_unconfirmed_stop_is_quarantined_with_the_writer_active() -> None:
    scene = Scene(submit_status=None, delivered=True, start_delay=1)
    scene.writer.confirms = False
    guard = scene.guard()
    scene.submit(guard)
    report = guard.secure(scene.engine, "ambiguous")
    assert report.quarantined and scene.engine.working
    with pytest.raises(RestoreForbidden):
        guard.restore(scene.checkpoint, ROOT)
    assert "restore" not in scene.log


def test_a_refusal_with_nothing_running_stops_nothing() -> None:
    scene = Scene(submit_status=400, delivered=False)
    guard = scene.guard()
    assert scene.submit(guard) is SubmitVerdict.REJECTED
    assert guard.phase is P.REJECTED and guard.record.failure_reason == "the server refused the prompt (400)"
    assert guard.secure(scene.engine, "ignored").phase is P.REJECTED
    assert scene.log == ["submit"]  # no abort, no stop


def test_a_refusal_while_the_session_works_is_treated_as_ambiguous() -> None:
    scene = Scene(submit_status=400, delivered=False)
    scene.engine.state = "busy"
    guard = scene.guard()
    assert scene.submit(guard) is SubmitVerdict.AMBIGUOUS
    assert guard.phase is P.AMBIGUOUS


def test_a_refusal_whose_effect_cannot_be_read_is_treated_as_ambiguous() -> None:
    scene = Scene(submit_status=400, delivered=False)
    guard = scene.guard()
    original = type(scene.engine).submit

    def submit(session_id: str, body: dict[str, Any]) -> int | None:
        status = original(scene.engine, session_id, body)
        scene.engine.unreadable = True
        return status

    scene.engine.submit = submit  # type: ignore[method-assign]
    assert scene.submit(guard) is SubmitVerdict.AMBIGUOUS
    assert guard.record.reconciliation == Reconciliation.UNKNOWN
    assert "could not be read" in (guard.record.failure_reason or "")


def test_a_send_that_raises_counts_as_ambiguous() -> None:
    scene = Scene()
    guard = scene.guard()

    def boom(session_id: str, body: dict[str, Any]) -> int | None:
        raise ValueError("a bug in the client")

    scene.engine.submit = boom  # type: ignore[method-assign]
    assert scene.submit(guard) is SubmitVerdict.AMBIGUOUS


def test_a_session_that_cannot_be_read_before_sending_sends_nothing() -> None:
    scene = Scene(unreadable=True)
    guard = scene.guard()
    assert scene.submit(guard) is SubmitVerdict.NOT_SENT
    assert guard.phase is P.ABANDONED and scene.engine.prompts == []


def test_the_same_guard_can_never_send_a_second_prompt() -> None:
    scene = Scene(submit_status=None, delivered=False)
    guard = scene.guard()
    scene.submit(guard)
    message = guard.record.message_id
    for _ in range(2):
        with pytest.raises(LifecycleError):
            scene.submit(guard)
    guard.secure(scene.engine, "ambiguous")
    with pytest.raises(LifecycleError):
        scene.submit(guard)
    assert len(scene.engine.prompts) == 1 and guard.record.message_id == message  # the refused tries changed nothing


def test_a_call_that_already_ran_cannot_run_again() -> None:
    scene = Scene()
    guard = scene.guard()
    scene.submit(guard)
    guard.secure(scene.engine, "x")
    guard.restore(scene.checkpoint, ROOT)
    with pytest.raises(AttemptRefused, match="already ran"):
        scene.guard()  # same run and call, though the first attempt is over
    assert scene.guard(call_id="call_2").phase is P.PREPARED  # a retry is a new call


def test_a_cancelled_run_sends_nothing_and_cannot_send_later() -> None:
    scene = Scene()
    guard = scene.guard()
    cancel = threading.Event()
    cancel.set()
    assert scene.submit(guard, cancel=cancel) is SubmitVerdict.NOT_SENT
    assert guard.phase is P.ABANDONED and scene.engine.prompts == []
    with pytest.raises(LifecycleError):
        scene.submit(guard)
    assert "cancelled" in (guard.record.failure_reason or "")
    assert guard.report().detail == "nothing was sent, so there was nothing to stop"


@pytest.mark.parametrize("phase", sorted(BLOCKING_PHASES))
def test_no_new_attempt_starts_while_the_project_is_held(phase: AttemptPhase) -> None:
    scene = Scene()
    guard = scene.guard()
    guard.record.phase = phase
    scene.store.save(guard.record)
    with pytest.raises(AttemptRefused, match="held"):
        scene.guard(call_id="call_2")
    assert scene.guard(call_id="call_3", project_id="another_project").phase is P.PREPARED


def test_the_original_failure_reason_survives_everything_after_it() -> None:
    scene = Scene(submit_status=None, delivered=False)
    scene.writer.confirms = False
    guard = scene.guard()
    scene.submit(guard)
    first = guard.record.failure_reason
    assert first is not None and "no answer" in first
    guard.secure(scene.engine, "the run was cancelled")
    guard.secure(scene.engine, "a third reason")
    assert guard.record.failure_reason == first and scene.store.only().failure_reason == first
    assert "confirmed stopped" in (guard.record.detail or "")


def test_a_failure_of_the_store_before_the_send_stops_the_send() -> None:
    scene = Scene()
    guard = scene.guard()
    scene.store.crash_after = 0
    with pytest.raises(StoreCrash):
        scene.submit(guard)
    assert scene.engine.prompts == []  # no durable record, so no prompt


# --- A2: what counts as stopped --------------------------------------------------------------


def test_an_acknowledged_abort_is_not_quiescence() -> None:
    scene = Scene(abort_stops=False)  # the server says yes and goes on working
    guard = scene.guard()
    scene.submit(guard)
    seen: list[AttemptPhase] = []
    scene.writer.terminate = lambda: (seen.append(guard.phase), False)[1]  # type: ignore[method-assign]
    report = guard.secure(scene.engine, "a timeout")
    assert report.abort_acknowledged and not report.observed_quiet and not report.process_terminated
    assert seen == [P.QUIESCENCE_CHECK] and report.quarantined


@pytest.mark.parametrize("phase", [p for p in PHASES if p is not P.SAFE_TO_RESTORE])
def test_restoring_is_refused_in_every_phase_but_one(phase: AttemptPhase) -> None:
    scene = Scene()
    guard = scene.guard()
    guard.record.phase = phase
    with pytest.raises(RestoreForbidden):
        guard.restore(scene.checkpoint, ROOT)
    assert scene.log == []


def test_the_restore_comes_strictly_after_confirmed_termination() -> None:
    scene = Scene(abort_stops=False)
    guard, report = _stopped(scene)
    assert guard.restore(scene.checkpoint, ROOT) == 3
    assert scene.log == ["submit", "abort", "terminate", "restore", "compare"]
    assert scene.phases()[-2:] == ["SAFE_TO_RESTORE", "RESTORED"]
    final = guard.report()
    assert report.process_terminated and not report.restore_verified and final.restore_verified


def test_quiet_observation_alone_never_permits_a_restore() -> None:
    scene = Scene()
    scene.writer.confirms = False
    guard, report = _stopped(scene)
    assert report.observed_quiet and report.abort_acknowledged  # everything looks fine from outside
    assert not report.process_terminated and report.quarantined
    with pytest.raises(RestoreForbidden):
        guard.restore(scene.checkpoint, ROOT)


def test_a_stop_that_raises_is_an_unconfirmed_stop() -> None:
    scene = Scene()
    scene.writer.raises = True
    _, report = _stopped(scene)
    assert report.quarantined and not report.process_terminated


def test_a_stop_that_says_gone_while_the_workspace_still_changes_is_quarantined() -> None:
    scene = Scene(abort_stops=False)
    scene.writer.really_stops = False
    guard, report = _stopped(scene)
    assert report.process_terminated and not report.workspace_stable and report.quarantined
    assert "not still" in (guard.record.detail or "")


def test_a_workspace_that_cannot_be_read_after_the_stop_is_quarantined() -> None:
    scene = Scene()
    scene.writer.terminate = lambda: (setattr(scene.workspace, "unreadable", True), True)[1]  # type: ignore[method-assign]
    _, report = _stopped(scene)
    assert report.quarantined and not report.workspace_stable


def test_abort_is_retried_a_bounded_number_of_times_and_the_stop_goes_on() -> None:
    scene = Scene(abort_failures=99)
    _, report = _stopped(scene)
    assert scene.engine.aborts == 3
    assert not report.abort_acknowledged and report.process_terminated and report.safe_to_restore


def test_a_failed_abort_followed_by_a_success_is_acknowledged() -> None:
    scene = Scene(abort_failures=2)
    _, report = _stopped(scene)
    assert scene.engine.aborts == 3 and report.abort_acknowledged


def test_an_unreachable_server_is_stopped_without_waiting_for_the_whole_limit() -> None:
    scene = Scene()
    guard = scene.guard()
    scene.submit(guard)
    scene.engine.unreadable = True
    report = guard.secure(scene.engine, "the server stopped answering")
    assert report.safe_to_restore and not report.observed_quiet
    assert scene.now < scene.policy.settle_timeout_seconds


def test_every_cause_of_stopping_takes_the_same_steps() -> None:
    logs = []
    for reason in ("no result within 600 seconds", "the run was cancelled", "the engine ran a forbidden tool", "over budget", "ambiguous"):
        scene = Scene(abort_stops=False)
        _, report = _stopped(scene, reason)
        logs.append((tuple(scene.log), tuple(scene.phases()), report.phase, scene.now))
    assert len(set(logs)) == 1


def test_a_finished_attempt_the_audit_rejects_takes_the_same_path_before_any_restore() -> None:
    scene = Scene()
    guard = scene.guard()
    scene.submit(guard)
    scene.engine.finish_work()
    assert guard.mark_finished()
    with pytest.raises(RestoreForbidden):
        guard.restore(scene.checkpoint, ROOT)
    assert guard.secure(scene.engine, "the audit found a protected file").safe_to_restore
    guard.restore(scene.checkpoint, ROOT)
    assert scene.log[-4:] == ["abort", "terminate", "restore", "compare"]


def test_a_second_cancellation_during_the_stop_changes_nothing() -> None:
    scene = Scene(abort_stops=False)
    cancel = threading.Event()
    guard = scene.guard()
    scene.submit(guard, cancel=cancel)
    scene.on_wait.append(cancel.set)
    report = guard.secure(scene.engine, "a timeout")
    assert report.safe_to_restore and scene.log[-1] == "terminate"
    assert scene.now >= scene.policy.settle_timeout_seconds  # the watch ran its full time


def test_stopping_twice_does_nothing_more() -> None:
    scene = Scene()
    guard, _ = _stopped(scene)
    log = list(scene.log)
    assert guard.secure(scene.engine, "again").safe_to_restore
    assert scene.log == log


def test_a_kept_attempt_cannot_be_stopped() -> None:
    scene = Scene()
    guard = scene.guard()
    scene.submit(guard)
    scene.engine.finish_work()
    guard.mark_finished()
    guard.keep()
    with pytest.raises(LifecycleError):
        guard.secure(scene.engine, "too late")
    assert guard.record.failure_reason is None


# --- Quarantine ------------------------------------------------------------------------------


def _quarantined() -> tuple[Scene, Any]:
    scene = Scene()
    scene.writer.confirms = False
    guard, _ = _stopped(scene)
    return scene, guard


def test_a_quarantined_attempt_has_no_way_out() -> None:
    assert P.QUARANTINED not in _NEXT
    scene, guard = _quarantined()
    for target in PHASES:
        with pytest.raises(LifecycleError):
            guard._move(target)
    with pytest.raises(RestoreForbidden):
        guard.restore(scene.checkpoint, ROOT)
    assert guard.phase is P.QUARANTINED


def test_a_quarantined_attempt_is_left_alone_by_recovery_and_holds_the_project() -> None:
    scene, guard = _quarantined()
    assert scene.store.unresolved() == []
    scene.writer.confirms = True
    assert scene.reopened().recover(scene.engine).quarantined  # a later success does not undo it
    assert scene.store.only().phase is P.QUARANTINED
    with pytest.raises(AttemptRefused, match="held"):
        scene.guard(call_id="call_2")


def test_a_restore_that_fails_part_way_is_quarantined() -> None:
    scene = Scene()
    scene.checkpoint.fails = True
    guard, _ = _stopped(scene)
    with pytest.raises(RestoreForbidden, match="quarantined"):
        guard.restore(scene.checkpoint, ROOT)
    assert guard.phase is P.QUARANTINED


def test_a_restore_that_does_not_match_the_checkpoint_is_quarantined() -> None:
    scene = Scene()
    scene.checkpoint.differs = ["a.py: different content"]
    guard, _ = _stopped(scene)
    with pytest.raises(RestoreForbidden, match="differs"):
        guard.restore(scene.checkpoint, ROOT)
    assert guard.phase is P.QUARANTINED and not guard.report().restore_verified


# --- Late results ----------------------------------------------------------------------------


def test_a_late_finish_cannot_turn_a_stopped_attempt_into_a_success() -> None:
    scene = Scene()
    guard, _ = _stopped(scene)
    assert guard.mark_finished() is False
    assert guard.phase is P.SAFE_TO_RESTORE and guard.record.late_results == 1
    assert scene.store.only().late_results == 1
    assert any(e == "coding.late_result_ignored" for e, _ in scene.events)


def test_a_late_finish_during_the_stop_is_ignored_and_the_stop_goes_on() -> None:
    scene = Scene()
    guard = scene.guard()
    scene.submit(guard)
    results: list[bool] = []
    scene.on_wait.append(lambda: results.append(guard.mark_finished()))
    report = guard.secure(scene.engine, "a timeout")
    assert results and not any(results)
    assert report.safe_to_restore and guard.record.failure_reason == "a timeout"


def test_a_finish_from_a_thread_cannot_race_a_stop_into_an_illegal_phase() -> None:
    for _ in range(50):
        scene = Scene()
        guard = scene.guard()
        scene.submit(guard)
        barrier = threading.Barrier(2)
        errors: list[Exception] = []

        def run(action: Any) -> None:
            barrier.wait()
            try:
                action()
            except Exception as exc:  # pragma: no cover - the failure this test exists to catch
                errors.append(exc)

        threads = [threading.Thread(target=run, args=(a,)) for a in (guard.mark_finished, lambda: guard.secure(scene.engine, "x"))]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        assert errors == [] and guard.phase is P.SAFE_TO_RESTORE


# --- Completion by relationship --------------------------------------------------------------


def _user(message_id: str = "msg_u") -> dict[str, Any]:
    return {"info": {"id": message_id, "role": "user", "time": {"created": 1}}, "parts": []}


def _reply(parent: str = "msg_u", *, finish: str | None = "stop", completed: bool = True, parts: list[Any] | None = None, **info: Any) -> dict[str, Any]:
    times: dict[str, int] = {"created": 2, **({"completed": 3} if completed else {})}
    return {"info": {"id": f"msg_r_{parent}", "role": "assistant", "parentID": parent, "time": times, "finish": finish, **info}, "parts": parts or []}


def _tool_part(status: str) -> dict[str, Any]:
    return {"type": "tool", "tool": "edit", "state": {"status": status, "input": {}}}


@pytest.mark.parametrize("finish", ["stop", "length", "content-filter", "end_turn"])
def test_a_finished_reply_to_the_exact_prompt_ends_the_turn(finish: str) -> None:
    assert turn_finished([_user(), _reply(finish=finish)], known=[], message_id="msg_u")


@pytest.mark.parametrize(
    ("messages", "message_id"),
    [
        ([], "msg_u"),  # nothing yet
        ([_user()], "msg_u"),  # accepted, no reply yet
        ([_reply()], "msg_u"),  # a reply with no prompt
        ([_user("msg_other"), _reply("msg_other")], "msg_u"),  # our prompt is not there
        ([_user(), _reply(finish="tool-calls")], "msg_u"),  # the model wants another step
        ([_user(), _reply(finish="unknown")], "msg_u"),
        ([_user(), _reply(finish=None)], "msg_u"),
        ([_user(), _reply(completed=False)], "msg_u"),  # still writing
        ([_user(), _reply("msg_elsewhere")], "msg_u"),  # answers another prompt
        ([_user(), _reply(parts=[_tool_part("running")])], "msg_u"),
        ([_user(), _reply(parts=[_tool_part("pending")])], "msg_u"),
    ],
)
def test_anything_short_of_that_does_not(messages: list[Any], message_id: str) -> None:
    assert not turn_finished(messages, known=[], message_id=message_id)


def test_tools_that_ended_do_not_hold_the_turn_open() -> None:
    parts = [_tool_part("completed"), _tool_part("error"), {"type": "text"}, "odd"]
    assert turn_finished([_user(), _reply(parts=parts)], known=[], message_id="msg_u")


def test_an_error_ends_the_turn_even_without_a_finish() -> None:
    assert turn_finished([_user(), _reply(finish=None, completed=False, error={"name": "APIError"})], known=[], message_id="msg_u")


def test_a_later_user_message_from_compaction_is_followed() -> None:
    messages = [_user(), _reply(finish="stop", summary=True), _user("msg_continue"), _reply("msg_continue")]
    assert turn_finished(messages, known=[], message_id="msg_u")
    messages = [_user(), _reply(finish="stop", summary=True), _user("msg_continue")]
    assert not turn_finished(messages, known=[], message_id="msg_u")


def test_what_an_earlier_attempt_left_never_counts() -> None:
    old = [_user("msg_old"), _reply("msg_old")]
    known = ["msg_old", "msg_r_msg_old"]
    assert not turn_finished(old, known=known, message_id=None)
    assert not turn_finished([*old, _user()], known=known, message_id=None)
    assert turn_finished([*old, _user(), _reply()], known=known, message_id=None)


def test_new_entries_leave_out_what_was_known_and_anything_malformed() -> None:
    entries = [_user("msg_old"), _user("msg_new"), "odd", {"info": "odd"}]
    assert new_entries(entries, ["msg_old"]) == [_user("msg_new")]


# --- Watching --------------------------------------------------------------------------------


def _watch(scene: Scene, **kwargs: Any) -> Any:
    return observe_quiescence(
        scene.engine, SESSION, fingerprint=scene.workspace.fingerprint, policy=scene.policy,
        clock=scene.clock, sleep=scene.sleep, **kwargs,
    )  # fmt: skip


def test_an_idle_session_with_a_still_workspace_is_seen_quiet() -> None:
    scene = Scene()
    seen = _watch(scene)
    assert seen.quiet and seen.polls == 2 and seen.last_state == "absent"


def test_a_workspace_that_keeps_changing_is_not_quiet_until_it_stops() -> None:
    scene = Scene()
    scene.workspace.keeps_changing = True
    assert not _watch(scene).quiet

    scene = Scene()
    scene.workspace.keeps_changing = True

    def stop_soon() -> None:
        if scene.now >= 1.5:
            scene.workspace.keeps_changing = False

    scene.on_wait.append(stop_soon)
    seen = _watch(scene)
    assert seen.quiet and scene.now >= 1.5


def test_absence_from_the_status_list_alone_is_not_quiet() -> None:
    scene = Scene()
    scene.engine.entries = [{"info": {"id": "m1", "role": "assistant", "time": {"created": 1}}, "parts": []}]
    assert scene.engine.state is None
    assert not _watch(scene).quiet  # an unfinished assistant turn

    scene = Scene()
    scene.engine.entries = [{"info": {"id": "m2", "role": "user", "time": {"created": 1}}, "parts": []}]
    assert not _watch(scene).quiet  # a new prompt the server has not started yet
    assert _watch(scene, known_messages=["m2"]).quiet  # the same message, known from before


def test_a_message_that_keeps_changing_is_not_quiet() -> None:
    scene = Scene()
    entry = {"info": {"id": "m1", "role": "assistant", "time": {"completed": 1}}, "parts": []}
    scene.engine.entries = [entry]
    scene.on_wait.append(lambda: entry["parts"].append({}))
    assert not _watch(scene).quiet


@pytest.mark.parametrize("state", ["busy", "retry", "something-new"])
def test_a_working_or_unknown_state_is_not_quiet(state: str) -> None:
    scene = Scene()
    scene.engine.state = state
    seen = _watch(scene)
    assert not seen.quiet and seen.last_state == state


def test_a_listed_idle_state_can_be_quiet() -> None:
    scene = Scene()
    scene.engine.state = "idle"
    assert _watch(scene).quiet


def test_an_unreadable_workspace_is_not_quiet() -> None:
    scene = Scene()
    scene.workspace.unreadable = True
    assert not _watch(scene).quiet


def test_watching_stops_at_the_time_limit() -> None:
    scene = Scene(abort_stops=False)
    scene.engine.state = "busy"
    seen = _watch(scene)
    assert not seen.quiet and seen.waited_seconds == scene.policy.settle_timeout_seconds


def test_one_failed_reading_breaks_the_run_of_identical_readings() -> None:
    scene = Scene()
    calls = {"n": 0}
    original = scene.engine.statuses

    def flaky() -> Any:
        calls["n"] += 1
        if calls["n"] == 2:
            scene.engine.unreadable = True
            try:
                return original()
            finally:
                scene.engine.unreadable = False
        return original()

    scene.engine.statuses = flaky  # type: ignore[method-assign]
    seen = _watch(scene)
    assert seen.quiet and seen.polls == 4  # 1 good, 1 failed, then 2 good


def test_the_fingerprint_sees_changes_and_does_not_follow_links(tmp_path: Path) -> None:
    root = tmp_path / "w"
    (root / "src").mkdir(parents=True)
    (root / "src" / "a.py").write_text("one")
    first = workspace_fingerprint(root)
    assert first is not None and first == workspace_fingerprint(root)
    (root / "src" / "a.py").write_text("one two")
    second = workspace_fingerprint(root)
    assert second != first
    (root / "b.py").write_text("x")
    assert workspace_fingerprint(root) != second
    (root / "b.py").unlink()
    assert workspace_fingerprint(root) == second


def test_a_missing_workspace_has_no_fingerprint(tmp_path: Path) -> None:
    assert workspace_fingerprint(tmp_path / "missing") is None


# --- The lifecycle table ---------------------------------------------------------------------


def test_only_the_listed_moves_exist() -> None:
    for source in PHASES:
        for target in PHASES:
            scene = Scene()
            guard = scene.guard()
            guard.record.phase = source
            if target in _NEXT.get(source, frozenset()):
                guard._move(target)
                assert guard.phase is target
            else:
                with pytest.raises(LifecycleError):
                    guard._move(target)
                assert guard.phase is source


def test_the_stated_lifecycle_is_the_only_road_to_a_restore() -> None:
    # Every path into SAFE_TO_RESTORE passes QUIESCENCE_CHECK, which only ABORT_REQUESTED leads to.
    into = {p for p, targets in _NEXT.items() if P.SAFE_TO_RESTORE in targets}
    assert into == {P.QUIESCENCE_CHECK}
    assert {p for p, targets in _NEXT.items() if P.QUIESCENCE_CHECK in targets} == {P.ABORT_REQUESTED}
    assert {p for p, targets in _NEXT.items() if P.RESTORED in targets} == {P.SAFE_TO_RESTORE}
    assert P.SUBMITTED in {p for p, targets in _NEXT.items() if P.RUNNING in targets and P.AMBIGUOUS in targets}


# --- Crash recovery --------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("phase", "expected"),
    [
        (P.PREPARED, P.ABANDONED),
        (P.SUBMITTED, P.SAFE_TO_RESTORE),
        (P.RUNNING, P.SAFE_TO_RESTORE),
        (P.AMBIGUOUS, P.SAFE_TO_RESTORE),
        (P.FINISHED, P.SAFE_TO_RESTORE),
        (P.ABORT_REQUESTED, P.SAFE_TO_RESTORE),
        (P.QUIESCENCE_CHECK, P.SAFE_TO_RESTORE),
        (P.SAFE_TO_RESTORE, P.SAFE_TO_RESTORE),
    ],
)
def test_a_restarted_backend_finishes_every_unresolved_phase(phase: AttemptPhase, expected: AttemptPhase) -> None:
    assert phase in UNRESOLVED_PHASES
    scene = Scene()
    guard = scene.guard()
    guard.record.phase = phase
    guard.record.session_id = SESSION
    scene.store.save(guard.record)
    assert scene.reopened().recover(scene.engine).phase is expected
    assert scene.engine.prompts == []  # recovery never sends a prompt
    assert scene.store.only().phase is expected


def test_a_crash_during_the_send_counts_as_ambiguous_and_stops_the_writer() -> None:
    scene = Scene()
    guard = scene.guard()

    def crash(session_id: str, body: dict[str, Any]) -> int | None:
        scene.engine.__class__.submit(scene.engine, session_id, body)
        raise KeyboardInterrupt  # the process died here: the answer was never seen

    scene.engine.submit = crash  # type: ignore[method-assign]
    with pytest.raises(KeyboardInterrupt):
        scene.submit(guard)
    assert scene.store.only().phase is P.SUBMITTED and scene.engine.working

    report = scene.reopened().recover(scene.engine)
    assert report.safe_to_restore and not scene.engine.working and len(scene.engine.prompts) == 1


def test_recovery_with_an_unreachable_server_and_a_failed_stop_is_quarantined() -> None:
    scene = Scene()
    guard = scene.guard()
    scene.submit(guard)
    scene.engine.unreadable = True
    scene.writer.confirms = False
    assert scene.reopened(scene.store.only()).recover(scene.engine).quarantined


def test_recovery_does_not_trust_a_restore_permission_it_cannot_reconfirm() -> None:
    for broken in ("stop", "workspace"):
        scene = Scene()
        _stopped(scene)
        if broken == "stop":
            scene.writer.confirms = False
        else:
            scene.workspace.keeps_changing = True
        assert scene.reopened().recover(scene.engine).quarantined


def test_the_recorded_facts_survive_a_restart() -> None:
    scene = Scene(submit_status=None, delivered=True)
    guard = scene.guard()
    scene.submit(guard)
    message = guard.record.message_id
    reason = guard.record.failure_reason
    reopened = scene.reopened()
    assert (reopened.phase, reopened.record.message_id, reopened.record.failure_reason) == (P.AMBIGUOUS, message, reason)
    assert reopened.record.session_id == SESSION and reopened.record.reconciliation == Reconciliation.STARTED


def _crash_scene(step: int) -> tuple[Scene, bool]:
    """Run an attempt with the backend dying at save number `step`. True when it died at all."""
    scene = Scene(abort_stops=False)
    guard = scene.guard()
    scene.store.crash_after = step
    try:
        scene.submit(guard)
        guard.secure(scene.engine, "a timeout")
        guard.restore(scene.checkpoint, ROOT)
    except StoreCrash:
        scene.store.crash_after = None
        return scene, True
    return scene, False


def test_a_crash_at_every_single_save_still_ends_safely_with_one_prompt_at_most() -> None:
    crashes = 0
    for step in range(20):
        scene, crashed = _crash_scene(step)
        if not crashed:
            break
        crashes += 1
        record = scene.store.only()
        report = scene.reopened(record).recover(scene.engine) if record.phase in UNRESOLVED_PHASES else None
        assert len(scene.engine.prompts) <= 1
        final = scene.store.only().phase
        assert final in (P.ABANDONED, P.SAFE_TO_RESTORE), (step, final)
        if final is P.SAFE_TO_RESTORE:
            assert report is not None and report.process_terminated and not scene.engine.working
            assert scene.reopened().restore(scene.checkpoint, ROOT) == 3
            assert scene.store.only().phase is P.RESTORED
    assert crashes >= 7  # every phase save along the way was exercised


# --- A simulation of many interleavings ------------------------------------------------------


def test_no_interleaving_ever_restores_while_a_writer_is_active() -> None:
    """The invariant holds for every interleaving, given a stop that tells the truth. A stop that says
    "gone" while the process lives is the one case nothing here can catch once it goes quiet: a writer
    that wakes after the last reading is invisible. Stage 9 must confirm termination independently."""
    outcomes: dict[str, int] = {}
    for seed in range(1500):
        rng = random.Random(seed)
        scene = Scene(
            policy=StopPolicy(settle_timeout_seconds=rng.choice((1.0, 3.0, 5.0)), poll_seconds=0.5, stable_polls=rng.choice((1, 2, 3))),
            submit_status=rng.choice((204, 204, None, 200, 408, 429, 500, 503, 400, 404)),
            delivered=rng.random() < 0.7,
            start_delay=rng.choice((0, 0, 1, 2, 5, 12)),
            abort_stops=rng.random() < 0.6,
            abort_failures=rng.choice((0, 0, 1, 2, 5)),
        )
        scene.writer.confirms = rng.random() < 0.8
        scene.writer.raises = rng.random() < 0.05
        guard = scene.guard()
        verdict = scene.submit(guard, use_message_id=rng.random() < 0.8)
        if rng.random() < 0.2 and verdict is SubmitVerdict.ACCEPTED and scene.engine.working:
            scene.engine.finish_work()
            guard.mark_finished()
        report = guard.secure(scene.engine, "stop")

        restored = False
        try:
            guard.restore(scene.checkpoint, ROOT)
            restored = True
        except RestoreForbidden:
            pass

        assert len(scene.engine.prompts) <= 1, seed
        if restored:
            assert not scene.engine.working, seed  # the invariant
            assert report.phase is P.SAFE_TO_RESTORE and report.process_terminated and report.workspace_stable, seed
            assert scene.log.index("terminate") < scene.log.index("restore"), seed
        if report.quarantined:
            assert "restore" not in scene.log, seed
        if scene.engine.working:
            assert not restored and report.phase in (P.QUARANTINED, P.REJECTED), seed
        outcomes[str(report.phase)] = outcomes.get(str(report.phase), 0) + 1
    assert set(outcomes) >= {"SAFE_TO_RESTORE", "QUARANTINED", "REJECTED"}
    assert sum(outcomes.values()) == 1500


def test_a_lying_stop_is_caught_when_the_writer_shows_itself_in_the_window() -> None:
    for delay in (0, 1, 2):
        scene = Scene(abort_stops=False, start_delay=delay)
        scene.writer.really_stops = False
        _, report = _stopped(scene)
        assert report.quarantined and report.process_terminated and not report.workspace_stable
