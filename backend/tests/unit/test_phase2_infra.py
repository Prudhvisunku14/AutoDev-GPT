"""Additive schema migration, the stack catalog loader and the RunManager."""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest
from sqlalchemy import Engine

from app.db.session import SchemaMigrationError, create_db_engine, init_db
from app.orchestrator.run_manager import RunManager
from app.stacks.catalog import StackCatalog, StackCatalogError
from tests.harness import Harness
from tests.helpers import make_settings


@pytest.fixture
def engine(tmp_path: Path) -> Engine:
    eng = create_db_engine(make_settings(tmp_path))
    init_db(eng)
    yield eng
    eng.dispose()


def _columns(engine: Engine, table: str) -> set[str]:
    with engine.connect() as conn:
        return {row[1] for row in conn.exec_driver_sql(f'PRAGMA table_info("{table}")')}


def test_phase1_database_gains_phase2_columns_and_keeps_data(engine: Engine) -> None:
    # Recreate the Phase 1 shape of `runs`: drop the columns Phase 2 added.
    phase2_columns = ["tool_calls", "nudges", "active_seconds", "cost_known", "pause_reason", "status_detail",
                      "analysis_json", "plan_json", "pending_messages_json"]
    with engine.begin() as conn:
        for column in phase2_columns:
            conn.exec_driver_sql(f"ALTER TABLE runs DROP COLUMN {column}")
        conn.exec_driver_sql("INSERT INTO projects (id, name, requirement_text, workspace_path, status, created_at) "
                             "VALUES ('p1', 'old', 'r', 'w', 'ACTIVE', '2026-10-29 00:00:00')")
        conn.exec_driver_sql("INSERT INTO runs (id, project_id, state, turns, tokens_in, tokens_out, cost_usd, created_at) "
                             "VALUES ('r1', 'p1', 'CREATED', 0, 0, 0, 0, '2026-10-29 00:00:00')")
    assert not set(phase2_columns) & _columns(engine, "runs")

    init_db(engine)

    assert set(phase2_columns) <= _columns(engine, "runs")
    with engine.connect() as conn:
        row = conn.exec_driver_sql("SELECT id, tool_calls, cost_known, pause_reason FROM runs").one()
    assert tuple(row) == ("r1", 0, 1, None)


def test_columns_without_a_default_are_not_added_silently(engine: Engine) -> None:
    with engine.begin() as conn:
        conn.exec_driver_sql("ALTER TABLE tool_invocations DROP COLUMN tool")
    with pytest.raises(SchemaMigrationError, match="tool_invocations.tool"):
        init_db(engine)


def test_init_db_is_idempotent(engine: Engine) -> None:
    before = _columns(engine, "runs")
    init_db(engine)
    assert _columns(engine, "runs") == before


# --- StackCatalog -------------------------------------------------------------------


def test_default_catalog_loads() -> None:
    catalog = StackCatalog.load()
    assert catalog.ids == ["python-cli", "python-fastapi", "react-vite", "static-html"]
    assert catalog.get("python-fastapi").test_methods == ("unit", "api", "static")  # type: ignore[union-attr]
    assert "python-cli:" in catalog.describe()


@pytest.mark.parametrize(
    ("content", "message"),
    [
        ("[unclosed", "cannot read"),
        ("just text", "'stacks' mapping"),
        ("stacks:\n  Bad_Id!:\n    name: n\n    language: l\n    frameworks: []\n    description: d\n    test_methods: [unit]\n", "invalid stack catalog entry"),
        ("stacks:\n  ok-id:\n    name: n\n    language: l\n    frameworks: []\n    description: d\n    test_methods: [fuzz]\n", "invalid stack catalog entry"),
        ("stacks:\n  ok-id:\n    name: n\n    unexpected: 1\n", "invalid stack catalog entry"),
        ("stacks:\n  ok-id: not-a-mapping\n", "invalid stack catalog entry"),
        ("stacks: {}\n", "empty"),
    ],
)
def test_bad_catalogs_are_rejected(tmp_path: Path, content: str, message: str) -> None:
    path = tmp_path / "catalog.yaml"
    path.write_text(content, encoding="utf-8")
    with pytest.raises(StackCatalogError, match=message):
        StackCatalog.load(path)


def test_missing_catalog_file(tmp_path: Path) -> None:
    with pytest.raises(StackCatalogError, match="cannot read"):
        StackCatalog.load(tmp_path / "missing.yaml")


# --- RunManager ---------------------------------------------------------------------


def test_submit_before_start_is_refused(tmp_path: Path) -> None:
    h = Harness(tmp_path)
    assert RunManager(h.orchestrator).submit("r" * 32) is False
    h.close()


def test_worker_advances_queued_runs_once_each(tmp_path: Path) -> None:
    from app.demo.calculator import calculator_llm

    h = Harness(tmp_path, calculator_llm())
    run_id = h.new_run()
    advanced: list[str] = []
    original = h.orchestrator.advance

    def counting(rid: str) -> None:
        advanced.append(rid)
        original(rid)

    h.orchestrator.advance = counting  # type: ignore[method-assign]

    async def scenario() -> None:
        manager = RunManager(h.orchestrator)
        await manager.start()
        assert manager.submit(run_id) and manager.submit(run_id)  # second submit: already queued
        await manager.join()
        await manager.stop()

    asyncio.run(scenario())
    assert advanced == [run_id]
    assert h.run(run_id).pause_reason == "phase_unavailable"
    h.close()


def test_worker_survives_an_exception(tmp_path: Path) -> None:
    h = Harness(tmp_path)
    attempted: list[str] = []

    def explode(rid: str) -> None:
        attempted.append(rid)
        raise RuntimeError("boom")

    h.orchestrator.advance = explode  # type: ignore[method-assign]

    async def scenario() -> None:
        manager = RunManager(h.orchestrator)
        await manager.start()
        manager.submit("a" * 32)
        manager.submit("b" * 32)
        await manager.join()
        await manager.stop()

    asyncio.run(scenario())
    assert attempted == ["a" * 32, "b" * 32]  # the second run still ran after the first failed
    h.close()
