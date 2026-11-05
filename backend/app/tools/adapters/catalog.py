"""`compare_tools`: GPT's read-only view of the tool catalog, in every working state.

GPT, the orchestrator, uses it to choose and explain tools from what DevPilot really has: each tool's tasks, limitations,
readiness on this backend and recorded evidence. It reads only the catalog; it contacts nothing and changes nothing.
"""

from __future__ import annotations

from functools import partial
from typing import Annotated, Any

from pydantic import Field, StringConstraints

from app.toolcatalog.catalog import MAX_COMPARED, ToolCatalog
from app.tools.base import RiskLevel, ToolArgs, ToolContext, ToolSpec
from app.tools.registry import ToolRegistry

Name = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=60)]
Task = Annotated[str, StringConstraints(strip_whitespace=True, pattern=r"^[a-z_]{2,40}$")]


class CompareToolsArgs(ToolArgs):
    names: list[Name] = Field(max_length=MAX_COMPARED, description="Tools to compare by name (e.g. opencode, n8n); an empty list lists every tool")
    tasks: list[Task] | None = Field(max_length=12, description="Catalog task ids to recommend a tool for (e.g. code_generation, workflow_automation); null for none")


def _brief(entry: dict[str, Any]) -> dict[str, Any]:
    """What GPT needs to choose: no long evidence list, only the strongest line."""
    return {key: entry[key] for key in ("id", "name", "role", "advantages", "limitations", "readiness", "functions", "strongest_evidence", "verified_live")} | {
        "tasks": [task["id"] for task in entry["tasks"]]
    }


def compare_tools(catalog: ToolCatalog, _ctx: ToolContext, args: CompareToolsArgs) -> dict[str, Any]:
    if not args.names:
        listing = catalog.describe()
        result: dict[str, Any] = {"tools": [_brief(entry) for entry in listing["tools"]], "planned": listing["planned"], "task_ids": sorted(listing["tasks"])}
        if args.tasks:
            result["recommendation"] = catalog.recommend(args.tasks)
        return result
    compared = catalog.compare(args.names, args.tasks)
    return {**compared, "tools": [_brief(entry) for entry in compared["tools"]]}


def register_catalog_tools(registry: ToolRegistry, catalog: ToolCatalog) -> None:
    registry.register(
        ToolSpec(
            name="compare_tools",
            description="Compare DevPilot's development tools (GPT, OpenCode, Docker, the website verifier, n8n, GitHub, Google Drive): tasks, limitations, "
            "readiness on this backend and recorded evidence. Names DevPilot has no adapter for are reported as unknown. Reads only.",
            args_model=CompareToolsArgs,
            risk=RiskLevel.READ,
            states=None,
            handler=partial(compare_tools, catalog),
        )
    )
