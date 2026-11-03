"""Adaptive tool selection: a deterministic policy, not another orchestrator (ARCHITECTURE §9.1).

GPT asks for a capability. This module decides which authorized tool does the work, in two stages:

A. Eligibility. A tool that fails any mandatory check is out, with its reasons. No score can bring it back.
B. Utility. The rest are ranked by

       U = 0.30·capability + 0.25·reliability + 0.15·quality + 0.10·latency + 0.10·cost + 0.10·resource

   Every term is in [0, 1]. The weights are the initial configuration and were not tuned.

Reliability is (successes + α) / (attempts + α + β), counted only from outcomes verified on comparable
tasks. A tool with no history scores α / (α + β), never 1. Latency, cost and memory use `r / (r + x)`
against a fixed reference `r`, so a score does not move when an unrelated tool is added, and zero cannot
divide anything. A number nobody has measured scores `unknown_metric`, which is deliberately low.

The same inputs always give the same choice: nothing here is random, and ties fall to the configured
order, then to the name. This module reads no database and starts nothing.
"""

from __future__ import annotations

import ctypes
import sys
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field, fields
from typing import TYPE_CHECKING

from app.selection.model import REAL, SIMULATED, CandidateScore, Refusal, Rejection, SelectionContext, SelectionReport, ToolHistory, ToolProfile, ToolStatus, task_category
from app.selection.selector import NoProvider, ProviderOption, Selection

if TYPE_CHECKING:
    from app.coding.launcher import CodingLauncher
    from app.config import Settings

SWITCHED, RETRIED_SAME = "switched", "retried_same"
_SCORE_DIGITS = 9  # two scores that differ only in floating-point noise count as a tie
_WEIGHT_TOLERANCE = 1e-6


@dataclass(frozen=True)
class Weights:
    capability: float = 0.30
    reliability: float = 0.25
    quality: float = 0.15
    latency: float = 0.10
    cost: float = 0.10
    resource: float = 0.10

    def __post_init__(self) -> None:
        values = [getattr(self, f.name) for f in fields(self)]
        if any(value < 0 for value in values):
            raise ValueError("selection weights cannot be negative")
        if abs(sum(values) - 1.0) > _WEIGHT_TOLERANCE:
            raise ValueError(f"selection weights must add up to 1, not {sum(values):.6f}")


@dataclass(frozen=True)
class ScoringConfig:
    """Beta(2, 2) by default: as if four attempts had split evenly, so one result moves reliability by 0.1, not 0.5."""

    weights: Weights = field(default_factory=Weights)
    alpha: float = 2.0
    beta: float = 2.0
    latency_reference_seconds: float = 120.0
    cost_reference_usd: float = 0.25
    memory_reference_mb: float = 2_048.0
    unknown_metric: float = 0.25  # equal to three times the reference: an unmeasured tool cannot look efficient
    prior_weight: float = 2.0  # how many observations a declared estimate is worth

    def __post_init__(self) -> None:
        if self.alpha <= 0 or self.beta <= 0:
            raise ValueError("alpha and beta must be positive, or an untested tool could score 0 or 1")
        if min(self.latency_reference_seconds, self.cost_reference_usd, self.memory_reference_mb) <= 0:
            raise ValueError("the reference values must be positive")
        if not 0.0 <= self.unknown_metric <= 1.0:
            raise ValueError("unknown_metric must be between 0 and 1")

    @classmethod
    def from_settings(cls, settings: Settings) -> ScoringConfig:
        return cls(
            weights=Weights(settings.selection_weight_capability, settings.selection_weight_reliability, settings.selection_weight_quality,
                            settings.selection_weight_latency, settings.selection_weight_cost, settings.selection_weight_resource),
            alpha=settings.selection_reliability_alpha, beta=settings.selection_reliability_beta, latency_reference_seconds=settings.selection_latency_reference_seconds,
            cost_reference_usd=settings.selection_cost_reference_usd, memory_reference_mb=settings.selection_memory_reference_mb,
        )  # fmt: skip


@dataclass(frozen=True)
class EligibilityPolicy:
    """What this deployment allows. Built from configuration, so nothing a model says can change it."""

    approved_environments: frozenset[str]
    configured_credentials: frozenset[str] = frozenset()  # by name: this class never holds a value
    approved_network: frozenset[str] = frozenset()
    allow_simulated: bool = True


def available_memory_mb() -> int | None:
    """How much more this machine can commit, which is what runs out here first. None where it cannot be told."""
    if sys.platform != "win32":
        return None

    class Status(ctypes.Structure):
        _fields_ = (("length", ctypes.c_ulong), ("load", ctypes.c_ulong), ("total_phys", ctypes.c_ulonglong), ("avail_phys", ctypes.c_ulonglong), ("total_commit", ctypes.c_ulonglong),
                    ("avail_commit", ctypes.c_ulonglong), ("total_virtual", ctypes.c_ulonglong), ("avail_virtual", ctypes.c_ulonglong), ("avail_extended", ctypes.c_ulonglong))  # fmt: skip

    status = Status()
    status.length = ctypes.sizeof(Status)
    if not ctypes.WinDLL("kernel32").GlobalMemoryStatusEx(ctypes.byref(status)):
        return None
    return int(status.avail_commit // 1024**2)


def reliability(successes: int, attempts: int, alpha: float, beta: float) -> float:
    """The smoothed estimate. It can approach 1 with evidence and never reaches it."""
    return (successes + alpha) / (attempts + alpha + beta)


def efficiency(value: float | None, reference: float, unknown: float) -> float:
    """1 at zero, 0.5 at the reference, towards 0 above it. Independent of the other candidates."""
    return unknown if value is None else reference / (reference + max(value, 0.0))


def expected(prior: float | None, total: float, samples: int, prior_weight: float) -> float | None:
    """A declared estimate, moved towards what was measured. One sample cannot replace it outright."""
    if samples == 0:
        return prior
    if prior is None:
        return total / samples
    return (prior * prior_weight + total) / (prior_weight + samples)


def _evidence(option: ProviderOption) -> str:
    return SIMULATED if option.simulated else REAL


def _history(option: ProviderOption, context: SelectionContext) -> ToolHistory:
    """Only outcomes of the tool's own kind: simulated results never count for a real tool."""
    return context.history.get((option.name, _evidence(option)), ToolHistory())


def _expected_cost(profile: ToolProfile, history: ToolHistory, config: ScoringConfig) -> float | None:
    return expected(profile.cost_usd, history.cost_total, history.cost_samples, config.prior_weight)


def _budget_refusal(profile: ToolProfile, history: ToolHistory, context: SelectionContext, config: ScoringConfig) -> str | None:
    if context.remaining_tokens is not None and profile.min_tokens > context.remaining_tokens:
        return f"it needs at least {profile.min_tokens} tokens and the run has {context.remaining_tokens} left"
    if context.remaining_usd is None:
        return None
    cost = _expected_cost(profile, history, config)
    if profile.min_cost_usd is None and cost is None:
        return "its cost is not known, and this run has a spending limit"
    floor = max(profile.min_cost_usd or 0.0, cost or 0.0)
    if floor > context.remaining_usd:
        return f"an attempt needs about ${floor:.4f} and the run has ${context.remaining_usd:.4f} left"
    return None


def refusals(option: ProviderOption, capability: str, context: SelectionContext, policy: EligibilityPolicy, config: ScoringConfig, memory_mb: int | None) -> list[tuple[Refusal, str]]:
    """Every mandatory check. Empty means eligible. The launcher is asked last, and only if it could matter."""
    profile = option.profile
    assert profile is not None
    found: list[tuple[Refusal, str]] = []
    if profile.status is ToolStatus.PLANNED:
        found.append((Refusal.NOT_IMPLEMENTED, "it is planned and has no implementation"))
    if capability not in profile.capabilities or capability not in option.capabilities:
        found.append((Refusal.CAPABILITY, f"it does not do {capability}"))
    if context.stack is not None and context.stack not in profile.stacks:
        found.append((Refusal.STACK, f"it has no verified path for the {context.stack} stack"))
    if context.kind not in profile.kinds:
        found.append((Refusal.TASK_KIND, f"it does not do {context.kind} tasks"))
    missing = sorted(profile.credentials - policy.configured_credentials)
    if missing:
        found.append((Refusal.CREDENTIALS, f"it needs a credential that is not configured: {', '.join(missing)}"))
    blocked = sorted(profile.network - policy.approved_network)
    if blocked:
        found.append((Refusal.NETWORK, f"it needs a network destination that is not approved: {', '.join(blocked)}"))
    if not profile.isolated:
        found.append((Refusal.SECURITY, "it would execute on the host, outside any sandbox"))
    if not profile.disposable_copy_only:
        found.append((Refusal.SECURITY, "it could write outside the run's disposable copy"))
    if profile.environment not in policy.approved_environments:
        found.append((Refusal.ENVIRONMENT, f"its execution environment {profile.environment!r} is not approved"))
    if option.simulated and not policy.allow_simulated:
        found.append((Refusal.SIMULATED, "this deployment does not allow simulated tools"))
    over_budget = _budget_refusal(profile, _history(option, context), context, config)
    if over_budget is not None:
        found.append((Refusal.BUDGET, over_budget))
    if memory_mb is not None and profile.memory_mb is not None and profile.memory_mb > memory_mb:
        found.append((Refusal.RESOURCES, f"it needs about {profile.memory_mb} MB and the machine can commit {memory_mb} MB"))
    if not found:
        reason = option.launcher.unavailable_reason()
        if reason is not None:
            found.append((Refusal.UNAVAILABLE, reason))
    return found


def score(option: ProviderOption, capability: str, context: SelectionContext, config: ScoringConfig) -> CandidateScore:
    """Stage B, for a tool that passed stage A."""
    profile = option.profile
    assert profile is not None
    history = _history(option, context)
    suits = [profile.capabilities[capability], profile.kinds[context.kind]]
    if context.stack is not None:
        suits.append(profile.stacks[context.stack])
    seconds = expected(profile.seconds, history.seconds_total, history.seconds_samples, config.prior_weight)
    cost = _expected_cost(profile, history, config)
    metrics = {
        "capability": sum(suits) / len(suits),
        "reliability": reliability(history.successes, history.attempts, config.alpha, config.beta),
        "quality": config.unknown_metric if profile.quality is None else profile.quality,
        "latency": efficiency(seconds, config.latency_reference_seconds, config.unknown_metric),
        "cost": efficiency(cost, config.cost_reference_usd, config.unknown_metric),
        "resource": efficiency(profile.memory_mb, config.memory_reference_mb, config.unknown_metric),
    }
    unknown = tuple(name for name, value in (("reliability", history.attempts or None), ("quality", profile.quality), ("latency", seconds), ("cost", cost), ("resource", profile.memory_mb)) if value is None)
    total = sum(getattr(config.weights, name) * value for name, value in metrics.items())
    return CandidateScore(option.name, total, attempts=history.attempts, expected_seconds=seconds, expected_cost_usd=cost, unknown=unknown, simulated=option.simulated, **metrics)


def _explain(ranking: Sequence[CandidateScore], fallback: str | None) -> str:
    chosen = ranking[0]
    parts = [f"{chosen.tool_id} is the only eligible tool, so nothing was compared."] if len(ranking) == 1 else [f"{chosen.tool_id} scored {chosen.score:.3f}, ahead of {ranking[1].tool_id} at {ranking[1].score:.3f}."]
    if fallback == SWITCHED:
        parts.append("Another tool failed this task, so it was tried instead.")
    elif fallback == RETRIED_SAME:
        parts.append("It failed this task before and no alternative is eligible, so it is tried again within the attempt limit.")
    parts.append(f"Its reliability rests on {chosen.attempts} verified attempts on comparable tasks." if chosen.attempts else "It has no verified history on comparable tasks, so its reliability is only the prior.")
    if "quality" in chosen.unknown or "latency" in chosen.unknown or "cost" in chosen.unknown:
        parts.append(f"Scored cautiously for lack of data: {', '.join(name for name in chosen.unknown if name != 'reliability')}.")
    return " ".join(parts)


class AdaptiveSelector:
    """The adaptive policy, with the interface of `ProviderSelector`.

    `planned` lists tools that do not exist yet, so a report can say so. They have no launcher and can never win.
    """

    def __init__(self, options: list[ProviderOption], *, policy: EligibilityPolicy, config: ScoringConfig | None = None, planned: Sequence[ToolProfile] = (), memory_probe: Callable[[], int | None] = available_memory_mb) -> None:
        names = [o.name for o in options]
        if len(names) != len(set(names)):
            raise ValueError("two tools share a name, so their history could not be told apart")
        for option in options:
            if option.profile is None or option.profile.tool_id != option.name:
                raise ValueError(f"{option.name} has no matching profile, so it cannot be judged")
        self._options = options
        self._policy = policy
        self._config = config or ScoringConfig()
        self._planned = tuple(planned)
        self._memory_probe = memory_probe

    @property
    def options(self) -> tuple[ProviderOption, ...]:
        """The providers in preference order, for describing the configuration. Choosing still goes through `choose`."""
        return tuple(self._options)

    def first_launcher(self) -> CodingLauncher:
        """The launcher that can stop what an earlier backend started."""
        return self._options[0].launcher

    def choose(self, capability: str, context: SelectionContext | None = None) -> Selection:
        context = context or SelectionContext()
        memory_mb = self._memory_probe()
        eligible: list[ProviderOption] = []
        rejected: list[Rejection] = []
        for option in self._options:
            found = refusals(option, capability, context, self._policy, self._config, memory_mb)
            if found:
                rejected.append(Rejection(option.name, tuple(code for code, _ in found), tuple(reason for _, reason in found)))
            else:
                eligible.append(option)
        if any(not option.simulated for option in eligible):  # a simulation is never chosen over something real
            for option in [o for o in eligible if o.simulated]:
                eligible.remove(option)
                rejected.append(Rejection(option.name, (Refusal.SIMULATED,), ("a real tool is eligible, and a simulation never replaces one",)))
        rejected += [Rejection(p.tool_id, (Refusal.NOT_IMPLEMENTED,), ("it is planned and has no implementation",)) for p in self._planned]
        excluded = {r.tool_id: "; ".join(r.reasons) for r in rejected}
        if not eligible:
            raise NoProvider(capability, excluded)
        position = {option.name: index for index, option in enumerate(self._options)}
        scored = [score(option, capability, context, self._config) for option in eligible]
        # A tool that failed this task goes behind the rest, but stays eligible: the attempt limit, not this order, decides when to stop.
        ranking = tuple(sorted(scored, key=lambda c: (context.demote_failed and c.tool_id in context.failed_here, -round(c.score, _SCORE_DIGITS), position[c.tool_id], c.tool_id)))
        chosen = ranking[0]
        fallback = None
        if context.failed_here:
            fallback = RETRIED_SAME if chosen.tool_id in context.failed_here else SWITCHED
        report = SelectionReport(
            policy="adaptive", category=task_category(capability, context.stack, context.kind), ranking=ranking, rejected=tuple(rejected), compared=len(ranking) > 1,
            real_candidates=sum(1 for c in ranking if not c.simulated), confidence=chosen.attempts / (chosen.attempts + self._config.alpha + self._config.beta),
            margin=chosen.score - ranking[1].score if len(ranking) > 1 else None, fallback=fallback, explanation=_explain(ranking, fallback),
        )  # fmt: skip
        option = next(o for o in eligible if o.name == chosen.tool_id)
        return Selection(option.name, option.launcher, option.simulated, excluded, report)
