"""Choosing the coding provider for a task (ARCHITECTURE §9.1).

GPT asks for code; Python chooses who writes it. A provider that cannot run right now is excluded with
its reason, and the first remaining one in preference order is chosen. The choice and every exclusion
are recorded, so a run shows why it used what it used.

`ProviderSelector` is the fixed-preference policy. It stays as the baseline, and `SELECTION_POLICY=fixed`
selects it. The adaptive policy is in adaptive.py and has the same interface.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Protocol

from app.selection.model import SelectionContext, SelectionReport, ToolProfile

if TYPE_CHECKING:
    from app.coding.launcher import CodingLauncher


@dataclass(frozen=True)
class ProviderOption:
    name: str
    launcher: CodingLauncher
    capabilities: frozenset[str]
    simulated: bool = False  # True: deterministic files, no model and no real coding engine
    profile: ToolProfile | None = None  # what the adaptive policy needs; the fixed policy ignores it


@dataclass(frozen=True)
class Selection:
    provider: str
    launcher: CodingLauncher
    simulated: bool
    excluded: dict[str, str]  # provider name -> why it was not used
    report: SelectionReport | None = None  # what an adaptive choice weighed

    def details(self) -> dict[str, Any]:
        """For the event log: the full ranking, so a run can show why it used what it used."""
        return self.report.to_dict() if self.report is not None else {"policy": "fixed"}

    def summary(self) -> dict[str, Any]:
        """For GPT: enough to explain the choice."""
        return self.report.summary() if self.report is not None else {"policy": "fixed"}


class NoProvider(Exception):
    """No provider can do this task. `excluded` says why, per provider."""

    def __init__(self, capability: str, excluded: dict[str, str]) -> None:
        reasons = "; ".join(f"{name}: {why}" for name, why in excluded.items()) or "none is configured"
        super().__init__(f"no coding provider can do {capability}: {reasons}")
        self.excluded = excluded


class Selector(Protocol):
    """What the coding tools need from a selection policy."""

    def first_launcher(self) -> CodingLauncher: ...

    def choose(self, capability: str, context: SelectionContext | None = None) -> Selection: ...


class ProviderSelector:
    def __init__(self, options: list[ProviderOption]) -> None:
        self._options = options

    @property
    def options(self) -> tuple[ProviderOption, ...]:
        """The providers in preference order, for describing the configuration. Choosing still goes through `choose`."""
        return tuple(self._options)

    def first_launcher(self) -> CodingLauncher:
        """The launcher that can stop what an earlier backend started."""
        return self._options[0].launcher

    def choose(self, capability: str, context: SelectionContext | None = None) -> Selection:
        """The first available provider in preference order. `context` is ignored: that is what makes it fixed."""
        excluded: dict[str, str] = {}
        for option in self._options:
            if capability not in option.capabilities:
                excluded[option.name] = f"it does not do {capability}"
                continue
            reason = option.launcher.unavailable_reason()
            if reason is not None:
                excluded[option.name] = reason
                continue
            return Selection(option.name, option.launcher, option.simulated, excluded)
        raise NoProvider(capability, excluded)
