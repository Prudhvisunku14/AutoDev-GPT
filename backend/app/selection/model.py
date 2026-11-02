"""What the selector works with: what a tool declares, what it has shown, and what a choice weighed.

Plain data with no database and no I/O, so a decision can be tested and benchmarked on its own.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field, replace
from enum import StrEnum
from typing import Any

BUILD, REPAIR, MODIFY = "build", "repair", "modify"  # a new task, a fix after failed tests, a change to existing code
REAL, SIMULATED = "real", "simulated"  # where an outcome came from; history never crosses this line


class ToolStatus(StrEnum):
    VERIFIED = "VERIFIED"  # ran end to end, with the result checked
    IMPLEMENTED = "IMPLEMENTED"  # built and tested, but not verified end to end
    PLANNED = "PLANNED"  # no code exists, so it can never be selected


class Refusal(StrEnum):
    """Why a tool may not be used. No score can overcome any of these."""

    CAPABILITY = "CAPABILITY"
    STACK = "STACK"
    TASK_KIND = "TASK_KIND"
    NOT_IMPLEMENTED = "NOT_IMPLEMENTED"
    UNAVAILABLE = "UNAVAILABLE"
    CREDENTIALS = "CREDENTIALS"
    NETWORK = "NETWORK"
    SECURITY = "SECURITY"
    ENVIRONMENT = "ENVIRONMENT"
    BUDGET = "BUDGET"
    RESOURCES = "RESOURCES"
    SIMULATED = "SIMULATED"


def task_category(capability: str, stack: str | None, kind: str) -> str:
    """Which attempts count as comparable. History is never carried across categories."""
    return f"{capability}:{stack or 'any'}:{kind}"


def _in_unit_range(name: str, value: float | None) -> None:
    if value is not None and not 0.0 <= value <= 1.0:
        raise ValueError(f"{name} must be between 0 and 1, not {value}")


@dataclass(frozen=True)
class ToolProfile:
    """What is declared about a tool. A number nobody has measured stays None, and is then scored cautiously."""

    tool_id: str
    status: ToolStatus
    capabilities: Mapping[str, float]  # what it can do and how well, in [0, 1]. Absent means it cannot
    stacks: Mapping[str, float]
    kinds: Mapping[str, float]
    environment: str  # where it runs; refused unless the policy approves it
    credentials: frozenset[str] = frozenset()  # by name: a value is never read here
    network: frozenset[str] = frozenset()  # destinations it must reach
    isolated: bool = True  # False: it would execute on the host
    disposable_copy_only: bool = True  # False: it could write outside the run's copy
    quality: float | None = None  # expected output quality, in [0, 1]
    seconds: float | None = None  # expected duration of one attempt
    cost_usd: float | None = None  # expected cost of one attempt
    min_cost_usd: float | None = None  # the least an attempt can start with. None: not known
    min_tokens: int = 0  # the fewest tokens an attempt can start with
    memory_mb: int | None = None  # peak memory of one attempt

    def __post_init__(self) -> None:
        _in_unit_range("quality", self.quality)
        for table in (self.capabilities, self.stacks, self.kinds):
            for key, value in table.items():
                _in_unit_range(key, value)


@dataclass(frozen=True)
class ToolHistory:
    """One tool's settled attempts in one task category, from one kind of evidence."""

    attempts: int = 0
    successes: int = 0
    failures: Mapping[str, int] = field(default_factory=dict)  # failure category -> count
    seconds_total: float = 0.0
    seconds_samples: int = 0
    cost_total: float = 0.0
    cost_samples: int = 0
    last_outcome: str | None = None
    last_success: str | None = None

    def with_outcome(self, *, success: bool, seconds: float | None = None, cost_usd: float | None = None, failure: str | None = None, at: str | None = None) -> ToolHistory:
        """This history plus one settled attempt. The benchmark learns through this, in memory."""
        failures = dict(self.failures)
        if not success:
            key = failure or "UNKNOWN"
            failures[key] = failures.get(key, 0) + 1
        return replace(
            self, attempts=self.attempts + 1, successes=self.successes + int(success), failures=failures,
            seconds_total=self.seconds_total + (seconds or 0.0), seconds_samples=self.seconds_samples + int(seconds is not None),
            cost_total=self.cost_total + (cost_usd or 0.0), cost_samples=self.cost_samples + int(cost_usd is not None),
            last_outcome="VERIFIED" if success else key, last_success=at if success else self.last_success,
        )  # fmt: skip


@dataclass(frozen=True)
class SelectionContext:
    """What this one choice depends on. Two calls with equal contexts choose the same tool."""

    stack: str | None = None
    kind: str = BUILD
    remaining_usd: float | None = None  # None: this run's cost is not tracked
    remaining_tokens: int | None = None
    history: Mapping[tuple[str, str], ToolHistory] = field(default_factory=dict)  # (tool, evidence) -> history
    failed_here: frozenset[str] = frozenset()  # tools that already failed this task in this run
    demote_failed: bool = True  # False: recovery chose to repair or retry on the same tool, so it keeps its rank


@dataclass(frozen=True)
class CandidateScore:
    """One eligible tool's score. Each metric is in [0, 1]."""

    tool_id: str
    score: float
    capability: float
    reliability: float
    quality: float
    latency: float
    cost: float
    resource: float
    attempts: int  # comparable settled attempts behind `reliability`
    expected_seconds: float | None
    expected_cost_usd: float | None
    unknown: tuple[str, ...]  # metrics with no data, so scored cautiously
    simulated: bool

    def to_dict(self) -> dict[str, Any]:
        return {
            "tool": self.tool_id, "score": round(self.score, 6), "simulated": self.simulated, "attempts": self.attempts, "unknown": list(self.unknown),
            "breakdown": {name: round(getattr(self, name), 6) for name in ("capability", "reliability", "quality", "latency", "cost", "resource")},
            "expected_seconds": self.expected_seconds, "expected_cost_usd": self.expected_cost_usd,
        }  # fmt: skip


@dataclass(frozen=True)
class Rejection:
    tool_id: str
    codes: tuple[Refusal, ...]
    reasons: tuple[str, ...]

    def to_dict(self) -> dict[str, Any]:
        return {"tool": self.tool_id, "codes": [c.value for c in self.codes], "reasons": list(self.reasons)}


@dataclass(frozen=True)
class SelectionReport:
    """Why a tool was chosen. `real_candidates` keeps a comparison of simulations from reading as a real one."""

    policy: str
    category: str
    ranking: tuple[CandidateScore, ...]
    rejected: tuple[Rejection, ...]
    compared: bool  # False: one tool was eligible, so nothing was weighed against anything
    real_candidates: int
    confidence: float  # how much verified evidence backs the choice, in [0, 1). Zero means none
    margin: float | None  # the lead over the next tool. None when nothing was compared
    fallback: str | None  # "switched" or "retried_same" after a failure on this task, otherwise None
    explanation: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "policy": self.policy, "category": self.category, "ranking": [c.to_dict() for c in self.ranking], "rejected": [r.to_dict() for r in self.rejected],
            "compared": self.compared, "real_candidates": self.real_candidates, "confidence": round(self.confidence, 6),
            "margin": None if self.margin is None else round(self.margin, 6), "fallback": self.fallback, "explanation": self.explanation,
        }  # fmt: skip

    def summary(self) -> dict[str, Any]:
        """The part GPT receives. The full ranking stays in the event log."""
        return {
            "policy": self.policy, "compared": self.compared, "real_candidates": self.real_candidates, "score": round(self.ranking[0].score, 3),
            "confidence": round(self.confidence, 3), "fallback": self.fallback, "explanation": self.explanation,
        }  # fmt: skip
