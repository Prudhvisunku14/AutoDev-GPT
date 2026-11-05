"""The reviewed facts about each development tool (config/tool_catalog.yaml), validated before the app starts.

The file says what a tool is for, its advantages and limitations, what it needs and the evidence recorded for it. It never
says whether the tool is usable now: that is computed from the running backend (live.py). A malformed file stops the app.
"""

from __future__ import annotations

import datetime as dt
from pathlib import Path
from typing import Annotated, Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, StringConstraints, ValidationError

Level = Literal["documentation", "offline_test", "real_container", "live"]
Outcome = Literal["passed", "failed", "partial"]
LEVELS: tuple[Level, ...] = ("documentation", "offline_test", "real_container", "live")  # weakest first
ToolId = Annotated[str, StringConstraints(pattern=r"^[a-z][a-z0-9_]{1,39}$")]
Line = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=300)]


class CatalogError(ValueError):
    pass


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class Evidence(_Strict):
    level: Level
    outcome: Outcome
    date: dt.date
    summary: Line
    source: Line


class ToolFacts(_Strict):
    id: ToolId
    name: Line
    role: Line
    aliases: tuple[Annotated[str, StringConstraints(strip_whitespace=True, to_lower=True, min_length=2, max_length=40)], ...]
    description: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=800)]
    tasks: tuple[str, ...] = Field(min_length=1)
    functions: tuple[str, ...]
    advantages: tuple[Line, ...] = Field(min_length=1)
    limitations: tuple[Line, ...] = Field(min_length=1)
    requirements: tuple[Line, ...]
    evidence: tuple[Evidence, ...]

    def strongest(self) -> Evidence | None:
        """The strongest passed evidence, or else the strongest of any outcome: what the tool has really been shown to do."""
        passed = [e for e in self.evidence if e.outcome == "passed"]
        pool = passed or list(self.evidence)
        return max(pool, key=lambda e: (LEVELS.index(e.level), e.date), default=None)

    @property
    def verified_live(self) -> bool:
        return any(e.level == "live" and e.outcome == "passed" for e in self.evidence)


class PlannedTool(_Strict):
    id: ToolId
    name: Line
    description: Line


class CatalogFacts(_Strict):
    tasks: dict[str, Line]
    tools: tuple[ToolFacts, ...]
    planned: tuple[PlannedTool, ...]

    def get(self, tool_id: str) -> ToolFacts | None:
        return next((tool for tool in self.tools if tool.id == tool_id), None)


def load_facts(path: Path) -> CatalogFacts:
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError) as exc:
        raise CatalogError(f"cannot read the tool catalog {path}: {exc}") from exc
    if not isinstance(data, dict) or not isinstance(data.get("tools"), dict) or not isinstance(data.get("tasks"), dict):
        raise CatalogError("the tool catalog must define 'tasks' and 'tools' mappings")
    try:
        tools = tuple(ToolFacts(id=key, **value) for key, value in data["tools"].items())
        planned = tuple(PlannedTool(id=key, **value) for key, value in (data.get("planned") or {}).items())
        facts = CatalogFacts(tasks=data["tasks"], tools=tools, planned=planned)
    except (TypeError, ValidationError) as exc:
        raise CatalogError(f"invalid tool catalog entry: {exc}") from exc
    problems = []
    seen_aliases: dict[str, str] = {}
    for tool in facts.tools:
        problems += [f"{tool.id}: unknown task {task!r}" for task in tool.tasks if task not in facts.tasks]
        for alias in (tool.id, *tool.aliases):
            if seen_aliases.setdefault(alias.lower(), tool.id) != tool.id:
                problems.append(f"alias {alias!r} names both {seen_aliases[alias.lower()]} and {tool.id}")
    clash = {p.id for p in facts.planned} & {t.id for t in facts.tools}
    problems += [f"{name} is both built and planned" for name in sorted(clash)]
    if problems:
        raise CatalogError("invalid tool catalog: " + "; ".join(problems))
    return facts
