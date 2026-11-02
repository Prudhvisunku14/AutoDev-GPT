"""What the selector learns from, and when (ARCHITECTURE §9.1).

One row per coding attempt. An attempt that finished is only PENDING: a file existing proves nothing, so
it counts as a success when the tests in the sandbox pass, and as a failure when they fail. A sandbox that
gave no answer says nothing about the code, so the attempt stays PENDING and teaches nothing.

Statistics are computed from these rows, never kept as running totals, so nothing can be counted twice
and every figure can be traced to its attempts. Callers own the transaction.
"""

from __future__ import annotations

from collections import defaultdict
from datetime import datetime

from sqlalchemy import func, select, update
from sqlalchemy.orm import Session

from app.coding.types import CodingStatus
from app.db.models import ToolOutcome, utcnow
from app.selection.model import REAL, SIMULATED, ToolHistory
from app.verification.report import REAL_SANDBOX

PENDING, VERIFIED, FAILED = "PENDING", "VERIFIED", "FAILED"
# The run stopped, or another attempt held the project: neither says anything about the tool.
_SAYS_NOTHING = frozenset({CodingStatus.CANCELLED, CodingStatus.BUSY})


def evidence_of(*, simulated: bool, sandbox: object) -> str:
    """Real only when a real provider's work was judged by the real sandbox. Anything less must not inform a real choice."""
    return REAL if not simulated and type(sandbox).__name__ == REAL_SANDBOX else SIMULATED


def record_attempt(
    session: Session, *, run_id: str, call_id: str, task_key: str, tool_id: str, category: str, evidence: str, status: CodingStatus, kept: bool, seconds: float | None, cost_usd: float | None
) -> ToolOutcome | None:
    """A kept attempt waits for verification. One the audit refused, or that failed, is settled now."""
    if status in _SAYS_NOTHING:
        return None
    existing = session.scalars(select(ToolOutcome).where(ToolOutcome.run_id == run_id, ToolOutcome.call_id == call_id)).first()
    if existing is not None:
        return existing
    row = ToolOutcome(
        run_id=run_id, call_id=call_id, task_key=task_key, tool_id=tool_id, task_category=category, evidence=evidence, status=PENDING if kept else FAILED,
        failure_category=None if kept else f"CODING_{status.value}", duration_ms=None if seconds is None else int(seconds * 1000), cost_usd=cost_usd, settled_at=None if kept else utcnow(),
    )  # fmt: skip
    session.add(row)
    session.flush()
    return row


def settle_verification(session: Session, run_id: str, *, passed: bool, failure: str | None = None) -> int:
    """The tests judged this run's waiting attempts. Returns how many were settled.

    Verification judges the whole project, so every waiting attempt shares the verdict.
    """
    result = session.execute(
        update(ToolOutcome)
        .where(ToolOutcome.run_id == run_id, ToolOutcome.status == PENDING)
        .values(status=VERIFIED if passed else FAILED, failure_category=None if passed else failure, settled_at=utcnow())
        .execution_options(synchronize_session=False)
    )
    return int(getattr(result, "rowcount", 0) or 0)


def load_history(session: Session, category: str) -> dict[tuple[str, str], ToolHistory]:
    """Settled attempts in this category, by (tool, evidence), so the two kinds of evidence cannot mix."""
    rows = session.execute(
        select(
            ToolOutcome.tool_id, ToolOutcome.evidence, ToolOutcome.status, ToolOutcome.failure_category, func.count(), func.sum(ToolOutcome.duration_ms), func.count(ToolOutcome.duration_ms),
            func.sum(ToolOutcome.cost_usd), func.count(ToolOutcome.cost_usd), func.max(ToolOutcome.settled_at),
        )
        .where(ToolOutcome.task_category == category, ToolOutcome.status != PENDING)
        .group_by(ToolOutcome.tool_id, ToolOutcome.evidence, ToolOutcome.status, ToolOutcome.failure_category)
    ).all()  # fmt: skip
    grouped: dict[tuple[str, str], list[tuple[str, str | None, int, int, int, float, int, datetime]]] = defaultdict(list)
    for tool, evidence, status, failure, count, duration_ms, durations, cost, costs, settled in rows:
        grouped[(tool, evidence)].append((status, failure, count, int(duration_ms or 0), durations, float(cost or 0.0), costs, settled))
    history: dict[tuple[str, str], ToolHistory] = {}
    for key, groups in grouped.items():
        latest = max(groups, key=lambda g: g[7])
        wins = [g for g in groups if g[0] == VERIFIED]
        history[key] = ToolHistory(
            attempts=sum(g[2] for g in groups), successes=sum(g[2] for g in wins), failures={g[1] or "UNKNOWN": g[2] for g in groups if g[0] == FAILED},
            seconds_total=sum(g[3] for g in groups) / 1000, seconds_samples=sum(g[4] for g in groups), cost_total=sum(g[5] for g in groups), cost_samples=sum(g[6] for g in groups),
            last_outcome=VERIFIED if latest[0] == VERIFIED else latest[1], last_success=max(g[7] for g in wins).isoformat() if wins else None,
        )  # fmt: skip
    return history


def failed_tools(session: Session, task_key: str) -> frozenset[str]:
    """Tools that already failed this task in this run, so a retry looks elsewhere first."""
    return frozenset(session.scalars(select(ToolOutcome.tool_id).where(ToolOutcome.task_key == task_key, ToolOutcome.status == FAILED)))
