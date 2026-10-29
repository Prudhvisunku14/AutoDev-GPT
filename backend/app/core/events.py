"""Event storage: an append-only, redacted execution log in the `events` table.

Each event is also written to the structured log. The in-memory pub/sub that feeds
SSE arrives with the run worker in Phase 2.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Mapping
from typing import Any

from sqlalchemy import func, insert, literal, select
from sqlalchemy.orm import Session

from app.core.redaction import Redactor
from app.db.models import Event, utcnow

logger = logging.getLogger("devpilot.events")


class EventRecorder:
    def __init__(self, redactor: Redactor) -> None:
        self._redactor = redactor

    def record(
        self,
        session: Session,
        *,
        project_id: str,
        type: str,
        payload: Mapping[str, Any] | None = None,
        run_id: str | None = None,
    ) -> Event:
        safe_payload = self._redactor.redact(dict(payload or {}))
        payload_json = json.dumps(safe_payload, default=str, sort_keys=True, ensure_ascii=False)
        ts = utcnow()

        # The next seq is computed inside the INSERT, so it is taken under SQLite's write lock
        # and concurrent writers cannot produce the same (project_id, seq).
        next_seq = (
            select(func.coalesce(func.max(Event.seq), 0) + 1)
            .where(Event.project_id == project_id)
            .scalar_subquery()
        )
        stmt = (
            insert(Event)
            .from_select(
                ["project_id", "run_id", "seq", "type", "payload_json", "ts"],
                select(
                    literal(project_id),
                    literal(run_id, Event.run_id.type),
                    next_seq,
                    literal(type),
                    literal(payload_json),
                    literal(ts, Event.ts.type),
                ),
            )
            .returning(Event.id)
        )
        event_id = session.execute(stmt).scalar_one()
        event = session.get(Event, event_id)
        assert event is not None

        logger.info(
            "event %s",
            type,
            extra={"project_id": project_id, "run_id": run_id, "seq": event.seq, "payload": safe_payload},
        )
        return event


def list_events(
    session: Session,
    *,
    project_id: str | None = None,
    run_id: str | None = None,
    after_seq: int = 0,
    limit: int = 100,
) -> list[Event]:
    stmt = select(Event).where(Event.seq > after_seq)
    if project_id is not None:
        stmt = stmt.where(Event.project_id == project_id)
    if run_id is not None:
        stmt = stmt.where(Event.run_id == run_id)
    return list(session.scalars(stmt.order_by(Event.seq).limit(limit)))


def event_payload(event: Event) -> dict[str, Any]:
    loaded = json.loads(event.payload_json)
    return loaded if isinstance(loaded, dict) else {"value": loaded}
