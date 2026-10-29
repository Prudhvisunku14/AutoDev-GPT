"""Engine and session factory: SQLite in WAL mode with foreign keys enforced."""

from __future__ import annotations

import sqlite3
from typing import Any

from sqlalchemy import Connection, Engine, create_engine, event, inspect
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool
from sqlalchemy.schema import CreateColumn

from app.config import Settings
from app.db.models import Base


def create_db_engine(settings: Settings) -> Engine:
    path = settings.database_path
    kwargs: dict[str, Any] = {"connect_args": {"check_same_thread": False, "timeout": 30}}
    if path is None:
        kwargs["poolclass"] = StaticPool  # one shared in-memory database
    else:
        path.parent.mkdir(parents=True, exist_ok=True)
    engine = create_engine(settings.resolved_database_url, **kwargs)
    event.listen(engine, "connect", _configure_sqlite_connection)
    event.listen(engine, "begin", _begin_immediate)
    return engine


def _configure_sqlite_connection(dbapi_connection: sqlite3.Connection, _record: Any) -> None:
    # Let SQLAlchemy's "begin" event control transactions instead of the sqlite3 module.
    dbapi_connection.isolation_level = None
    cursor = dbapi_connection.cursor()
    try:
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.execute("PRAGMA journal_mode=WAL")
        cursor.execute("PRAGMA synchronous=NORMAL")
        cursor.execute("PRAGMA busy_timeout=30000")
    finally:
        cursor.close()


def _begin_immediate(conn: Connection) -> None:
    # Take the write lock at the start of every transaction. With deferred transactions, a
    # request that reads and then writes fails with "database is locked" (no busy wait) when
    # another writer commits in between. Single-user load makes the serialization cheap.
    conn.exec_driver_sql("BEGIN IMMEDIATE")


def create_session_factory(engine: Engine) -> sessionmaker[Session]:
    return sessionmaker(bind=engine, expire_on_commit=False)


class SchemaMigrationError(RuntimeError):
    """An existing database cannot be upgraded automatically."""


def init_db(engine: Engine) -> None:
    """Create missing tables, then add missing columns to existing tables.

    Only additive changes are automatic: a new column must be nullable or have a server
    default. Alembic replaces this once the schema stabilizes (ARCHITECTURE §10).
    """
    Base.metadata.create_all(engine)
    # One connection for inspection and DDL: with BEGIN IMMEDIATE, a second connection would wait on the first.
    with engine.begin() as conn:
        inspector = inspect(conn)
        for table in Base.metadata.sorted_tables:
            existing = {c["name"] for c in inspector.get_columns(table.name)}
            for column in table.columns:
                if column.name in existing:
                    continue
                if not column.nullable and column.server_default is None:
                    raise SchemaMigrationError(
                        f"cannot add NOT NULL column {table.name}.{column.name} without a server default; "
                        "back up and delete the database file, or migrate it manually"
                    )
                ddl = CreateColumn(column).compile(dialect=engine.dialect)
                conn.exec_driver_sql(f'ALTER TABLE "{table.name}" ADD COLUMN {ddl}')
