"""Choosing the coding provider for a task (ARCHITECTURE §9.1).

GPT asks for code; Python chooses who writes it. A provider that cannot run right now is excluded with
its reason, and the first remaining one in preference order is chosen. The choice and every exclusion
are recorded, so a run shows why it used what it used.

This is the deterministic core only. The adaptive scoring from past outcomes is the full Phase 7.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from app.coding.launcher import CodingLauncher


@dataclass(frozen=True)
class ProviderOption:
    name: str
    launcher: CodingLauncher
    capabilities: frozenset[str]
    simulated: bool = False  # True: deterministic files, no model and no real coding engine


@dataclass(frozen=True)
class Selection:
    provider: str
    launcher: CodingLauncher
    simulated: bool
    excluded: dict[str, str]  # provider name -> why it was not used


class NoProvider(Exception):
    """No provider can do this task. `excluded` says why, per provider."""

    def __init__(self, capability: str, excluded: dict[str, str]) -> None:
        reasons = "; ".join(f"{name}: {why}" for name, why in excluded.items()) or "none is configured"
        super().__init__(f"no coding provider can do {capability}: {reasons}")
        self.excluded = excluded


class ProviderSelector:
    def __init__(self, options: list[ProviderOption]) -> None:
        self._options = options

    def first_launcher(self) -> CodingLauncher:
        """The launcher that can stop what an earlier backend started."""
        return self._options[0].launcher

    def choose(self, capability: str) -> Selection:
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
