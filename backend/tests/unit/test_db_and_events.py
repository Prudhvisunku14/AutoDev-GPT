from __future__ import annotations

import json
import threading
from datetime import UTC, datetime
from pathlib import Path

import pytest
from sqlalchemy import Engine, text
from sqlalchemy.exc import IntegrityError, StatementError
from sqlalchemy.orm import Session, sessionmaker

from app.core.events import EventRecorder, event_payload, list_events
from app.core.redaction import MASK, Redactor
from app.db import repositories
from app.db.models import Event, Project, Run, RunState, new_id
from app.db.session import create_db_engine, create_session_factory, init_db
from tests.helpers import make_settings


@pytest.fixture
def engine(tmp_path: Path) -> Engine:
    eng = create_db_engine(make_settings(tmp_path))
    init_db(eng)
    yield eng
    eng.dispose()


@pytest.fixture
def factory(engine: Engine) -> sessionmaker[Session]:
    return create_session_factory(engine)


def _project(factory: sessionmaker[Session]) -> str:
    with factory.begin() as s:
        p = repositories.create_project(
            s, project_id=new_id(), name="p", requirement_text="r", workspace_path="w"
        )
        return p.id


def test_sqlite_runs_in_wal_mode_with_foreign_keys(engine: Engine) -> None:
    with engine.connect() as conn:
        assert conn.exec_driver_sql("PRAGMA journal_mode").scalar() == "wal"
        assert conn.exec_driver_sql("PRAGMA foreign_keys").scalar() == 1


def test_database_file_is_created_under_configured_path(tmp_path: Path, engine: Engine) -> None:
    assert (tmp_path / "data" / "test.db").is_file()


def test_foreign_keys_are_enforced(factory: sessionmaker[Session]) -> None:
    with pytest.raises(IntegrityError), factory.begin() as s:
        s.add(Run(project_id="f" * 32))


def test_timestamps_are_utc_aware_after_reload(factory: sessionmaker[Session]) -> None:
    pid = _project(factory)
    with factory() as s:
        project = s.get(Project, pid)
        assert project is not None
        assert project.created_at.tzinfo is UTC


def test_naive_datetimes_are_refused(factory: sessionmaker[Session]) -> None:
    pid = _project(factory)
    with pytest.raises(StatementError, match="naive datetimes"), factory.begin() as s:
        run = repositories.create_run(s, s.get(Project, pid))  # type: ignore[arg-type]
        run.started_at = datetime(2026, 1, 1)
        s.flush()


def test_run_defaults_and_cancel(factory: sessionmaker[Session]) -> None:
    pid = _project(factory)
    with factory.begin() as s:
        run = repositories.create_run(s, s.get(Project, pid))  # type: ignore[arg-type]
        assert run.state is RunState.CREATED
        assert (run.turns, run.tokens_in, run.tokens_out, run.cost_usd) == (0, 0, 0, 0.0)
        assert repositories.get_active_run(s, pid) is run
        repositories.cancel_run(run)
    with factory() as s:
        reloaded = repositories.get_run(s, run.id)
        assert reloaded is not None and reloaded.is_terminal
        assert reloaded.ended_at is not None and reloaded.outcome == "cancelled"
        assert repositories.get_active_run(s, pid) is None
        assert [r.id for r in repositories.list_runs(s, pid)] == [run.id]
        assert repositories.count_runs(s) == 1


def test_list_projects_pagination(factory: sessionmaker[Session]) -> None:
    ids = {_project(factory) for _ in range(3)}
    with factory() as s:
        page1 = repositories.list_projects(s, limit=2, offset=0)
        page2 = repositories.list_projects(s, limit=2, offset=2)
        assert len(page1) == 2 and len(page2) == 1
        assert {p.id for p in page1 + page2} == ids
        assert repositories.count_projects(s) == 3


def test_events_get_monotonic_seq_per_project(factory: sessionmaker[Session]) -> None:
    recorder = EventRecorder(Redactor())
    p1, p2 = _project(factory), _project(factory)
    with factory.begin() as s:
        seqs = [recorder.record(s, project_id=p1, type="t").seq for _ in range(3)]
        other = recorder.record(s, project_id=p2, type="t").seq
    assert seqs == [1, 2, 3]
    assert other == 1


def test_event_payload_is_redacted_before_storage(factory: sessionmaker[Session]) -> None:
    secret = "ghp_" + "s" * 36
    recorder = EventRecorder(Redactor(["known-secret-xyz"]))
    pid = _project(factory)
    with factory.begin() as s:
        recorder.record(
            s, project_id=pid, type="t", payload={"note": f"{secret} known-secret-xyz", "api_key": "k"}
        )
    with factory() as s:
        (stored,) = s.query(Event).all()
        assert secret not in stored.payload_json
        assert "known-secret-xyz" not in stored.payload_json
        assert event_payload(stored) == {"api_key": MASK, "note": f"{MASK} {MASK}"}


def test_list_events_filters_by_run_and_seq(factory: sessionmaker[Session]) -> None:
    recorder = EventRecorder(Redactor())
    pid = _project(factory)
    with factory.begin() as s:
        run = repositories.create_run(s, s.get(Project, pid))  # type: ignore[arg-type]
        recorder.record(s, project_id=pid, type="project.created")
        for i in range(3):
            recorder.record(s, project_id=pid, run_id=run.id, type="run.step", payload={"i": i})
    with factory() as s:
        all_run = list_events(s, run_id=run.id)
        assert [e.seq for e in all_run] == [2, 3, 4]
        assert [e.seq for e in list_events(s, run_id=run.id, after_seq=2, limit=1)] == [3]
        assert len(list_events(s, project_id=pid)) == 4


def test_event_payload_non_dict_json_is_wrapped() -> None:
    assert event_payload(Event(payload_json=json.dumps([1, 2]))) == {"value": [1, 2]}


def test_concurrent_writers_never_duplicate_seq(factory: sessionmaker[Session]) -> None:
    recorder = EventRecorder(Redactor())
    pid = _project(factory)
    errors: list[BaseException] = []

    def worker() -> None:
        try:
            for _ in range(5):
                with factory.begin() as s:
                    # Read first, then write: the pattern that deadlocks with deferred transactions.
                    s.execute(text("SELECT count(*) FROM events")).scalar()
                    recorder.record(s, project_id=pid, type="concurrent")
        except BaseException as exc:  # surfaced below
            errors.append(exc)

    threads = [threading.Thread(target=worker) for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert errors == []
    with factory() as s:
        seqs = sorted(e.seq for e in list_events(s, project_id=pid, limit=500))
    assert seqs == list(range(1, 41))


def test_in_memory_database_works(tmp_path: Path) -> None:
    eng = create_db_engine(make_settings(tmp_path, database_url="sqlite:///:memory:"))
    init_db(eng)
    factory = create_session_factory(eng)
    pid = _project(factory)
    with factory() as s:
        assert repositories.get_project(s, pid) is not None
    eng.dispose()
