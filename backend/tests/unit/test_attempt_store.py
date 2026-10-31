"""The attempt lifecycle on a real SQLite database: what survives a restart of the backend."""

from __future__ import annotations

from pathlib import Path

import pytest
from sqlalchemy import Engine, inspect, text
from sqlalchemy.orm import Session, sessionmaker

from app.coding.attempt_store import SqlAttemptStore
from app.coding.session_control import AttemptGuard, AttemptRecord, AttemptRefused
from app.coding.types import AttemptPhase
from app.core.redaction import Redactor
from app.db import repositories
from app.db.models import Run
from app.db.session import create_db_engine, create_session_factory, init_db
from tests.coding_fakes import Scene
from tests.helpers import make_settings

P = AttemptPhase


@pytest.fixture
def engine(tmp_path: Path) -> Engine:
    created = create_db_engine(make_settings(tmp_path))
    init_db(created)
    return created


@pytest.fixture
def sessions(engine: Engine) -> sessionmaker[Session]:
    return create_session_factory(engine)


@pytest.fixture
def run(sessions: sessionmaker[Session]) -> Run:
    with sessions.begin() as s:
        project = repositories.create_project(s, project_id="a" * 32, name="p", requirement_text="r", workspace_path="w")
        return repositories.create_run(s, project)


def _store(sessions: sessionmaker[Session], *secrets: str) -> SqlAttemptStore:
    return SqlAttemptStore(sessions, Redactor(secrets))


def _record(run: Run, call_id: str = "call_1") -> AttemptRecord:
    return AttemptRecord(project_id=run.project_id, run_id=run.id, call_id=call_id, task_key=f"{run.id}:1")


def test_the_table_is_added_to_an_existing_database(tmp_path: Path) -> None:
    engine = create_db_engine(make_settings(tmp_path))
    init_db(engine)
    with engine.begin() as conn:
        conn.execute(text("DROP TABLE coding_attempts"))
    init_db(engine)
    assert "coding_attempts" in inspect(engine).get_table_names()


def test_every_fact_round_trips(sessions: sessionmaker[Session], run: Run) -> None:
    store = _store(sessions)
    record = _record(run)
    store.claim(record)
    record.phase = P.QUIESCENCE_CHECK
    record.session_id = "ses_1"
    record.message_id = "msg_abc"
    record.known_messages = ["msg_1", "msg_2"]
    record.reconciliation = "NOT_SEEN"
    record.failure_reason = "a timeout"
    record.abort_acknowledged = True
    record.observed_quiet = True
    record.process_terminated = True
    record.workspace_stable = True
    record.restore_verified = True
    record.late_results = 2
    record.detail = "why"
    record.container_id = "c" * 64
    record.stop_evidence = "before: RUNNING; after: NOT_FOUND"
    store.save(record)
    assert _store(sessions).get(run.id, "call_1") == record


def test_a_call_claims_once_and_a_held_project_refuses(sessions: sessionmaker[Session], run: Run) -> None:
    store = _store(sessions)
    first = _record(run)
    store.claim(first)
    with pytest.raises(AttemptRefused, match="already ran"):
        store.claim(_record(run))
    with pytest.raises(AttemptRefused, match="held"):
        store.claim(_record(run, "call_2"))
    first.phase = P.ABANDONED
    store.save(first)
    store.claim(_record(run, "call_2"))
    with pytest.raises(AttemptRefused, match="already ran"):
        store.claim(_record(run, "call_1"))


def test_saving_an_attempt_that_was_never_claimed_is_a_bug(sessions: sessionmaker[Session], run: Run) -> None:
    with pytest.raises(LookupError):
        _store(sessions).save(_record(run))


def test_unresolved_lists_what_a_restart_must_finish_and_not_quarantine(sessions: sessionmaker[Session]) -> None:
    store = _store(sessions)
    expected: list[str] = []
    for index, phase in enumerate(P):
        with sessions.begin() as s:  # one project each: an attempt in most phases holds its project
            project = repositories.create_project(s, project_id=f"{index:032x}", name="p", requirement_text="r", workspace_path="w")
            record = _record(repositories.create_run(s, project), f"call_{index}")
        store.claim(record)
        record.phase = phase
        store.save(record)
        if phase in (P.PREPARED, P.SUBMITTED, P.RUNNING, P.AMBIGUOUS, P.FINISHED, P.ABORT_REQUESTED, P.QUIESCENCE_CHECK, P.SAFE_TO_RESTORE):
            expected.append(record.call_id)
    assert [r.call_id for r in store.unresolved()] == expected
    assert "QUARANTINED" not in {str(r.phase) for r in store.unresolved()}


def test_failure_text_is_redacted_before_it_is_stored(sessions: sessionmaker[Session], run: Run) -> None:
    store = _store(sessions, "s3cret-value-123456")
    record = _record(run)
    record.failure_reason = "the server said s3cret-value-123456"
    record.detail = "again s3cret-value-123456"
    record.stop_evidence = "docker said s3cret-value-123456"
    store.claim(record)
    saved = store.get(run.id, "call_1")
    assert saved is not None
    assert "s3cret" not in str(saved.failure_reason) and "s3cret" not in str(saved.detail) and "s3cret" not in str(saved.stop_evidence)
    assert saved.stop_evidence is not None and saved.stop_evidence.startswith("docker said")  # kept, only the secret goes


def test_a_full_attempt_is_durable_at_each_step_and_a_new_process_can_finish_it(sessions: sessionmaker[Session], run: Run) -> None:
    scene = Scene(submit_status=None, delivered=True)
    scene.store = _store(sessions)  # type: ignore[assignment]
    guard = scene.guard(run_id=run.id, project_id=run.project_id)
    scene.submit(guard)
    assert guard.phase is P.AMBIGUOUS

    # A new process: the store, nothing else.
    fresh = _store(sessions)
    records = fresh.unresolved()
    assert [r.phase for r in records] == [P.AMBIGUOUS]
    assert records[0].message_id == guard.record.message_id and records[0].session_id == "ses_1"
    resumed = AttemptGuard(records[0], **{**scene.deps(), "store": fresh})
    report = resumed.recover(scene.engine)

    assert report.safe_to_restore and len(scene.engine.prompts) == 1 and not scene.engine.working
    resumed.restore(scene.checkpoint, Path("/not/used"))
    saved = fresh.get(run.id, "call_1")
    assert saved is not None and saved.phase is P.RESTORED and saved.restore_verified and saved.process_terminated
    assert fresh.unresolved() == []
