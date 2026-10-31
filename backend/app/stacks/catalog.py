"""StackCatalog: the only technology stacks GPT may choose (ARCHITECTURE §7.1)."""

from __future__ import annotations

from pathlib import Path
from typing import Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, ValidationError

TestMethod = Literal["unit", "api", "e2e", "static"]

DEFAULT_CATALOG = Path(__file__).with_name("catalog.yaml")


class StackCatalogError(ValueError):
    pass


class StackEntry(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    id: str = Field(pattern=r"^[a-z0-9][a-z0-9-]{1,40}$")
    name: str = Field(min_length=1)
    language: str = Field(min_length=1)
    frameworks: tuple[str, ...]
    description: str = Field(min_length=1)
    test_methods: tuple[TestMethod, ...] = Field(min_length=1)
    # The approved sandbox command that runs this stack's tests. None: nothing can verify it yet.
    test_command: str | None = None


class StackCatalog:
    def __init__(self, entries: list[StackEntry]) -> None:
        if not entries:
            raise StackCatalogError("stack catalog is empty")
        self._entries = {e.id: e for e in entries}

    @classmethod
    def load(cls, path: Path = DEFAULT_CATALOG) -> StackCatalog:
        try:
            data = yaml.safe_load(path.read_text(encoding="utf-8"))
        except (OSError, yaml.YAMLError) as exc:
            raise StackCatalogError(f"cannot read stack catalog {path}: {exc}") from exc
        stacks = data.get("stacks") if isinstance(data, dict) else None
        if not isinstance(stacks, dict):
            raise StackCatalogError("stack catalog must define a 'stacks' mapping")
        try:
            return cls([StackEntry(id=key, **value) for key, value in stacks.items()])
        except (TypeError, ValidationError) as exc:
            raise StackCatalogError(f"invalid stack catalog entry: {exc}") from exc

    def get(self, stack_id: str) -> StackEntry | None:
        return self._entries.get(stack_id)

    @property
    def ids(self) -> list[str]:
        return sorted(self._entries)

    def describe(self) -> str:
        """Plain-text list for the planning prompt."""
        return "\n".join(
            f"- {e.id}: {e.description} (language: {e.language}; verification methods: {', '.join(e.test_methods)}; "
            f"can be built and tested now: {'yes' if e.test_command else 'no'})"
            for e in sorted(self._entries.values(), key=lambda e: e.id)
        )
