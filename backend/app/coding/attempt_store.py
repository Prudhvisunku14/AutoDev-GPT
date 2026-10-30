"""The database behind AttemptGuard. Every call is its own short transaction, committed before it
returns, so a phase is on disk before the step it describes begins."""

from __future__ import annotations

import json

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, sessionmaker

from app.coding.session_control import BLOCKING_PHASES, UNRESOLVED_PHASES, AttemptRecord, AttemptRefused
from app.core.redaction import Redactor
from app.db.models import CodingAttempt


class SqlAttemptStore:
    def __init__(self, sessions: sessionmaker[Session], redactor: Redactor) -> None:
        self._sessions = sessions
        self._redactor = redactor

    def claim(self, record: AttemptRecord) -> None:
        # BEGIN IMMEDIATE (db/session.py) makes the check and the insert one atomic step.
        try:
            with self._sessions.begin() as s:
                if self._row(s, record) is not None:
                    raise AttemptRefused("this call already ran a coding attempt; a retry is a new call")
                held = s.scalars(
                    select(CodingAttempt).where(
                        CodingAttempt.project_id == record.project_id, CodingAttempt.phase.in_(BLOCKING_PHASES)
                    )
                ).first()
                if held is not None:
                    raise AttemptRefused(f"the project is held by an attempt in phase {held.phase}")
                row = CodingAttempt(
                    project_id=record.project_id, run_id=record.run_id, call_id=record.call_id,
                    task_key=record.task_key, phase=record.phase,
                )  # fmt: skip
                self._copy(record, row)
                s.add(row)
        except IntegrityError as exc:  # only reachable if the check above stops being atomic
            raise AttemptRefused("this call already ran a coding attempt; a retry is a new call") from exc

    def save(self, record: AttemptRecord) -> None:
        with self._sessions.begin() as s:
            row = self._row(s, record)
            if row is None:
                raise LookupError("the attempt was never claimed")
            self._copy(record, row)

    def get(self, run_id: str, call_id: str) -> AttemptRecord | None:
        with self._sessions.begin() as s:
            row = s.scalars(select(CodingAttempt).where(CodingAttempt.run_id == run_id, CodingAttempt.call_id == call_id)).first()
            return None if row is None else _record(row)

    def unresolved(self) -> list[AttemptRecord]:
        with self._sessions.begin() as s:
            rows = s.scalars(select(CodingAttempt).where(CodingAttempt.phase.in_(UNRESOLVED_PHASES)).order_by(CodingAttempt.id))
            return [_record(row) for row in rows]

    def _row(self, s: Session, record: AttemptRecord) -> CodingAttempt | None:
        query = select(CodingAttempt).where(CodingAttempt.run_id == record.run_id, CodingAttempt.call_id == record.call_id)
        return s.scalars(query).first()

    def _copy(self, record: AttemptRecord, row: CodingAttempt) -> None:
        row.phase = record.phase
        row.session_id = record.session_id
        row.message_id = record.message_id
        row.known_messages_json = json.dumps(record.known_messages)
        row.reconciliation = record.reconciliation
        row.failure_reason = self._safe(record.failure_reason)
        row.abort_acknowledged = record.abort_acknowledged
        row.observed_quiet = record.observed_quiet
        row.process_terminated = record.process_terminated
        row.workspace_stable = record.workspace_stable
        row.restore_verified = record.restore_verified
        row.late_results = record.late_results
        row.detail = self._safe(record.detail)

    def _safe(self, text: str | None) -> str | None:
        return None if text is None else self._redactor.redact_text(text)


def _record(row: CodingAttempt) -> AttemptRecord:
    return AttemptRecord(
        project_id=row.project_id, run_id=row.run_id, call_id=row.call_id, task_key=row.task_key, phase=row.phase,
        session_id=row.session_id, message_id=row.message_id, known_messages=json.loads(row.known_messages_json),
        reconciliation=row.reconciliation, failure_reason=row.failure_reason, abort_acknowledged=row.abort_acknowledged,
        observed_quiet=row.observed_quiet, process_terminated=row.process_terminated,
        workspace_stable=row.workspace_stable, restore_verified=row.restore_verified,
        late_results=row.late_results, detail=row.detail,
    )  # fmt: skip
