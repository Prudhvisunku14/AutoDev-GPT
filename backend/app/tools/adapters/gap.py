"""`submit_gap_analysis`: GPT compares the existing code with the requirements before planning; code checks the comparison.

When a run starts from existing code (a repository imported in this run, or one the person named and DevPilot read), the
plan gate refuses `submit_plan` until a gap analysis is accepted. GPT writes the analysis; these checks, not the prompt,
decide whether it stands:
- every requirement it cites exists in this run's analysis, and every functional requirement is either already met
  (a similarity) or missing (a feature to build): nothing is silently dropped;
- every file it keeps, modifies or deletes exists in the source, and every file it adds does not;
- licence, copying and notice files are never deleted, and it may not delete more than a fifth of the existing files:
  DevPilot adapts repositories, it does not blindly overwrite them;
- a component it replaces says why, and every missing feature has a test that will verify it.
The accepted analysis is recorded (`analysis.gap_recorded`) and handed to the coding engine with each task's brief.
"""

from __future__ import annotations

import re
from typing import Annotated, Any, Literal

from pydantic import Field, StringConstraints
from sqlalchemy import select

from app.core.events import event_payload, list_events
from app.db.models import Requirement, RunState
from app.sources.events import latest_check
from app.tools.adapters.repositories import imported_record
from app.tools.base import ErrorClass, RiskLevel, ToolArgs, ToolContext, ToolError, ToolSpec
from app.tools.registry import ToolRegistry

Text = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=600)]
Reason = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=600)]
Path = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=300)]
Ref = Annotated[str, StringConstraints(strip_whitespace=True, pattern=r"^R-\d{3}$")]
GAP_EVENT = "analysis.gap_recorded"
MAX_DELETED_SHARE = 0.2
_PROTECTED = re.compile(r"(^|/)(LICEN[CS]E|COPYING|NOTICE|THIRD[_-]PARTY|AUTHORS)([._-][^/]*)?$", re.IGNORECASE)


class Similarity(ToolArgs):
    requirement_ref: Ref
    existing: Text = Field(description="Where the existing code already does this: a file, component or behaviour")


class Missing(ToolArgs):
    requirement_ref: Ref
    description: Text = Field(description="What must be built for this requirement")


class Conflict(ToolArgs):
    path: Path = Field(description="The existing file whose behaviour conflicts with a requirement")
    description: Text
    resolution: Text


class FileChange(ToolArgs):
    path: Path
    change: Literal["add", "modify", "delete", "keep"]
    reason: Reason


class PlannedTest(ToolArgs):
    requirement_ref: Ref
    description: Text = Field(description="The automated test that will show the requirement is met")


class ReuseDecision(ToolArgs):
    component: Text = Field(description="A component, folder or file of the existing code")
    decision: Literal["reuse", "adapt", "replace"]
    reason: Reason


class SubmitGapAnalysisArgs(ToolArgs):
    existing_functionality: list[Text] = Field(min_length=1, max_length=40, description="What the existing code already does, in short statements")
    similarities: list[Similarity] = Field(max_length=60, description="Requirements the existing code already meets, and where")
    missing_features: list[Missing] = Field(max_length=60, description="Requirements that need new or changed code")
    conflicts: list[Conflict] = Field(max_length=40)
    architecture_changes: list[Text] = Field(max_length=30)
    files: list[FileChange] = Field(min_length=1, max_length=200, description="Every file to add, modify, delete or deliberately keep")
    tests: list[PlannedTest] = Field(min_length=1, max_length=100)
    reuse_decisions: list[ReuseDecision] = Field(min_length=1, max_length=40, description="Why each existing part is reused, adapted or replaced")


def source_files(ctx: ToolContext) -> tuple[set[str] | None, set[str]]:
    """The existing files (None when DevPilot has no list), and the licence and notice files that must survive."""
    imported = imported_record(ctx.session, ctx.run.id)
    if imported is not None:
        prefix = f"{imported['into']}/" if imported.get("into") else ""
        listed = imported.get("files")
        protected = {prefix + f for f in (*imported.get("license_files", []), imported.get("notices_file"), imported.get("record_file")) if f}
        return ({prefix + f for f in listed} if isinstance(listed, list) else None), protected
    checked = latest_check(ctx.session, ctx.project.id) or {}
    paths = [p for r in checked.get("resources", []) if r.get("kind") == "github" and r.get("status") == "accessible" for p in r.get("paths", [])]
    return (set(paths) if paths else None), set()


def needs_gap_analysis(ctx: ToolContext) -> bool:
    if imported_record(ctx.session, ctx.run.id) is not None:
        return True
    checked = latest_check(ctx.session, ctx.project.id) or {}
    return any(r.get("kind") == "github" and r.get("status") == "accessible" for r in checked.get("resources", []))


def recorded_gap(session: Any, run_id: str) -> dict[str, Any] | None:
    found = [event_payload(e) for e in list_events(session, run_id=run_id, limit=5_000) if e.type == GAP_EVENT]
    return found[-1] if found else None


def problems(args: SubmitGapAnalysisArgs, requirements: dict[str, tuple[str, str]], files: set[str] | None, protected: set[str]) -> list[str]:
    """Every problem at once. `requirements`: code -> (kind, text)."""
    found: list[str] = []
    cited = [s.requirement_ref for s in args.similarities] + [m.requirement_ref for m in args.missing_features] + [t.requirement_ref for t in args.tests]
    found += [f"{ref} is not a requirement of this run" for ref in sorted(set(cited) - set(requirements))]
    covered = {s.requirement_ref for s in args.similarities} | {m.requirement_ref for m in args.missing_features}
    found += [f"{code} ({text[:60]}) is neither met by the existing code nor listed as missing" for code, (kind, text) in sorted(requirements.items())
              if kind == "functional" and code not in covered]  # fmt: skip
    tested = {t.requirement_ref for t in args.tests}
    found += [f"missing feature {ref} has no planned test" for ref in sorted({m.requirement_ref for m in args.missing_features} - tested)]
    paths = [f.path for f in args.files]
    found += [f"{path} is listed twice" for path in sorted({p for p in paths if paths.count(p) > 1})]
    for change in args.files:
        if change.path.startswith("/") or ".." in change.path.split("/") or "\\" in change.path:
            found.append(f"{change.path}: a path inside the project, written with /")
            continue
        exists = files is not None and change.path in files
        if files is not None and change.change in ("modify", "delete", "keep") and not exists:
            found.append(f"{change.path} does not exist in the existing code, so it cannot be {change.change}d" if change.change != "keep" else f"{change.path} does not exist in the existing code")
        if files is not None and change.change == "add" and exists:
            found.append(f"{change.path} already exists: modify it instead of adding it")
        if change.change == "delete" and (change.path in protected or _PROTECTED.search(change.path)):
            found.append(f"{change.path} is a licence or notice file: reused code keeps its licence and attribution")
    deleted = sum(1 for f in args.files if f.change == "delete")
    if files and deleted > max(1, int(len(files) * MAX_DELETED_SHARE)):
        found.append(f"the analysis deletes {deleted} of {len(files)} existing files; DevPilot adapts existing code rather than overwriting it, so at most {int(MAX_DELETED_SHARE * 100)}% may be deleted")
    found += [f"replacing {d.component} needs a reason of at least a sentence" for d in args.reuse_decisions if d.decision == "replace" and len(d.reason) < 20]
    return found


def submit_gap_analysis(ctx: ToolContext, args: SubmitGapAnalysisArgs) -> dict[str, object]:
    requirements = {r.code: (str(r.kind), r.text) for r in ctx.session.scalars(select(Requirement).where(Requirement.run_id == ctx.run.id))}
    if not requirements:
        raise ToolError(ErrorClass.POLICY_REFUSED, "submit the analysis first: the gap analysis compares the existing code with its requirements")
    files, protected = source_files(ctx)
    found = problems(args, requirements, files, protected)
    if found:
        raise ToolError(ErrorClass.VALIDATION_FAILED, f"the gap analysis has {len(found)} problem(s)", found[:40])
    payload = args.model_dump() | {"source_files_known": files is not None, "source_file_count": len(files) if files is not None else None}
    ctx.events.record(ctx.session, project_id=ctx.project.id, run_id=ctx.run.id, type=GAP_EVENT, payload=payload)
    counts = {key: len(payload[key]) for key in ("similarities", "missing_features", "conflicts", "files", "tests", "reuse_decisions")}
    return {"recorded": True, **counts, "note": "Plan the tasks from this analysis; each task's brief to the coding engine includes it."}


def brief_text(gap: dict[str, Any] | None) -> str:
    """The part of a coding brief that carries the accepted gap analysis."""
    if not gap:
        return ""
    lines = ["Existing code: build on it, do not rewrite it. Keep its licence and notice files."]
    lines += [f"- {f['change']} {f['path']}: {f['reason']}" for f in gap.get("files", [])[:60]]
    lines += [f"- {d['decision']} {d['component']}: {d['reason']}" for d in gap.get("reuse_decisions", [])[:20]]
    lines += [f"- conflict in {c['path']}: {c['resolution']}" for c in gap.get("conflicts", [])[:20]]
    return "\n".join(lines)


def register_gap_tool(registry: ToolRegistry) -> None:
    registry.register(
        ToolSpec(
            name="submit_gap_analysis",
            description="When the project starts from existing code, compare it with the requirements before planning: what exists, what is similar, "
            "what is missing or conflicting, the architecture changes, every file to add, modify, delete or keep, the tests, and why each part is reused, "
            "adapted or replaced. Python checks it against the real file list; submit_plan waits for an accepted one.",
            args_model=SubmitGapAnalysisArgs,
            risk=RiskLevel.READ,
            states=frozenset({RunState.PLANNING}),
            handler=submit_gap_analysis,
        )
    )
