"""`find_source_requirement_gaps`: GPT's conservative look at which files of the project may relate to each requirement.

From the evaluation upgrade. A word match is never treated as proof that a requirement is implemented: every row is
`unverified_candidate` or `no_source_evidence_found`, and `verified_requirements` is always 0. GPT uses it to find where to
look before it writes `submit_gap_analysis`, whose claims code checks against the real file list.

The upgrade's `explain_development_tools` and `compare_development_tools` are not registered for GPT: `compare_tools`
(tools/adapters/catalog.py) already answers from the reviewed catalog with live readiness and evidence, and two overlapping
tools would only confuse the choice. Their function-level listing is served over HTTP instead (api/evaluation.py).
"""

from __future__ import annotations

from typing import Annotated, Any

from pydantic import Field, StringConstraints

from app.db.models import RunState
from app.evaluation.gaps import compare_requirements
from app.tools.base import ErrorClass, RiskLevel, ToolArgs, ToolContext, ToolError, ToolSpec
from app.tools.registry import ToolRegistry


class RequirementItem(ToolArgs):
    identifier: Annotated[str, StringConstraints(min_length=1, max_length=32)] = Field(description="The requirement's code, e.g. R-001")
    description: Annotated[str, StringConstraints(min_length=3, max_length=1200)]


class CompareSourceArgs(ToolArgs):
    requirements: Annotated[list[RequirementItem], Field(min_length=1, max_length=30)]


def find_source_requirement_gaps(ctx: ToolContext, args: CompareSourceArgs) -> dict[str, Any]:
    workspace = ctx.workspace
    if workspace is None or not workspace.has_project(ctx.project.id):
        raise ToolError(ErrorClass.VALIDATION_FAILED, "the project has no workspace yet: import the repository first")
    try:
        return compare_requirements([(x.identifier, x.description) for x in args.requirements], workspace.list_files(ctx.project.id),
                                    lambda path: workspace.read_bytes(ctx.project.id, path))  # fmt: skip
    except ValueError as exc:
        raise ToolError(ErrorClass.VALIDATION_FAILED, str(exc)) from exc


def register_source_triage(registry: ToolRegistry) -> None:
    registry.register(
        ToolSpec(
            name="find_source_requirement_gaps",
            description="Conservative word-match scan of the project's files against requirements. Returns candidate files only, never proof: "
            "inspect them, then write submit_gap_analysis, whose claims are checked against the real file list.",
            args_model=CompareSourceArgs,
            risk=RiskLevel.READ,
            states=frozenset({RunState.PLANNING}),
            handler=find_source_requirement_gaps,
        )
    )
