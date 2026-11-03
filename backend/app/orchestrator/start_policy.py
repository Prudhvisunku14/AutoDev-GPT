"""What may stop a run from starting beyond its own limits: a hook for a server that serves the dashboard for real builds.

A run's own limits (RUN_BUDGET_USD, turns, tokens, time) are enforced while it runs (app/orchestrator/budget.py). A
`StartPolicy` decides before it starts, across runs. The normal backend has none. `scripts/live_server.py` uses
`SessionBudget`: the most one server session's runs may spend together, approved in the terminal when it started, and
the time that session has left before its guard stops it.

A run counts at its whole cap from the moment it is admitted until it ends, then at what it cost. A run whose cost is
unknown counts at its cap. So runs in flight can never take the session past its total, and nothing is estimated.
"""

from __future__ import annotations

import threading
import time
from collections.abc import Callable
from typing import Any, Protocol

from sqlalchemy.orm import Session

from app.db.models import Run


class StartPolicy(Protocol):
    def admit(self, session: Session, run: Run) -> str | None:
        """None: the run may start, and is now counted. Otherwise why it may not start."""
        ...

    def describe(self, session: Session) -> dict[str, Any]:
        """What GET /budget shows about this policy."""
        ...


class SessionBudget:
    def __init__(self, total_usd: float, run_cap_usd: float, *, run_seconds: float, ends_at: float | None = None, clock: Callable[[], float] = time.monotonic) -> None:
        if total_usd <= 0 or run_cap_usd <= 0:
            raise ValueError("a session total and a run cap must be positive")
        self._total = total_usd
        self._cap = run_cap_usd
        self._run_seconds = run_seconds  # the longest a run may take (RUN_WALL_CLOCK_SECONDS)
        self._ends_at = ends_at  # on `clock`, when the session's guard stops the server; None: no time limit
        self._clock = clock
        self._admitted: list[str] = []
        self._lock = threading.Lock()  # two quick starts must not both pass the check

    def committed(self, session: Session) -> float:
        total = 0.0
        for run_id in self._admitted:
            run = session.get(Run, run_id)
            if run is not None and run.is_terminal and run.cost_known:
                total += run.cost_usd
            else:  # running, unknown, or gone: the worst it could be
                total += max(self._cap, run.cost_usd if run is not None else 0.0)
        return total

    def seconds_left(self) -> float | None:
        return None if self._ends_at is None else max(self._ends_at - self._clock(), 0.0)

    def admit(self, session: Session, run: Run) -> str | None:
        with self._lock:
            left = self.seconds_left()
            if left is not None and left < self._run_seconds:
                return f"this server session ends in {left / 60:.0f} minutes, and a run may take {self._run_seconds / 60:.0f}: restart the server for a new session"
            if run.id in self._admitted:  # a resume: the run is already counted at its whole cap
                return None
            committed = self.committed(session)
            if committed + self._cap > self._total + 1e-9:
                return (f"the session's approved total of ${self._total:.2f} could be passed: ${committed:.4f} is already counted "
                        f"and one more run may cost up to ${self._cap:.2f}")  # fmt: skip
            self._admitted.append(run.id)
            return None

    def describe(self, session: Session) -> dict[str, Any]:
        committed = self.committed(session)
        return {
            "approved_total_usd": self._total,
            "committed_usd": round(committed, 6),
            "remaining_usd": round(max(self._total - committed, 0.0), 6),
            "runs_admitted": len(self._admitted),
            "seconds_left": self.seconds_left(),
        }
