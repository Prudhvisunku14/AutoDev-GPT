"""The project-level record of the last resource check (`resources.checked`), read by the API and by GPT's first message."""

from __future__ import annotations

from typing import Any

from sqlalchemy import select

from app.core.events import event_payload
from app.db.models import Event

CHECKED = "resources.checked"


def latest_check(session: Any, project_id: str) -> dict[str, Any] | None:
    event = session.scalars(
        select(Event).where(Event.project_id == project_id, Event.type == CHECKED, Event.run_id.is_(None)).order_by(Event.seq.desc()).limit(1)
    ).first()
    return event_payload(event) if event is not None else None
