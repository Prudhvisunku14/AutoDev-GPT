"""Phase 2 tools: the three gate tools and request_human_input (ARCHITECTURE §4.1, §9.3 step 2).

A gate moves the run forward only when its code-level checks pass. Every problem found
is returned at once, so GPT can fix them all in one retry. Handlers run inside a
savepoint (ToolExecutor.invoke): a rejected submission leaves no rows behind.
"""

from __future__ import annotations

import json
from typing import Annotated, Any, Literal

from pydantic import Field, StringConstraints, model_validator
from sqlalchemy import select

from app.core.events import event_payload, list_events
from app.db.models import AcceptanceCriterion, PlanTask, Requirement, RunState
from app.orchestrator.run_control import change_state
from app.rag.blueprint import plan_problems
from app.verification import browser_acceptance
from app.verification.browser_acceptance import BrowserCheckSpec
from app.tools.base import ErrorClass, RiskLevel, ToolArgs, ToolContext, ToolError, ToolSpec
from app.tools.registry import ToolRegistry

ShortText = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=200)]
Text = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=1000)]
LongText = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=2000)]
RequirementCode = Annotated[str, StringConstraints(strip_whitespace=True, pattern=r"^R-\d{3}$")]
CriterionCode = Annotated[str, StringConstraints(strip_whitespace=True, pattern=r"^AC-\d{3}$")]
StackId = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=64)]

MAX_REQUIREMENTS = 50
MAX_TASKS = 30
MAX_CRITERIA = 100
WEBSITE_STACKS = frozenset({"static-html", "react-vite"})  # verified by app.verification.web, with frozen browser checks


# --- Argument models ------------------------------------------------------------


class RequirementItem(ToolArgs):
    code: RequirementCode = Field(description="R-001, R-002, ... numbered in order")
    text: Text = Field(description="One atomic, testable requirement")
    kind: Literal["functional", "non_functional"]


class SubmitAnalysisArgs(ToolArgs):
    summary: LongText = Field(description="What will be built, in one paragraph")
    requirements: list[RequirementItem] = Field(min_length=1, max_length=MAX_REQUIREMENTS)
    assumptions: list[Text] = Field(max_length=30, description="Assumptions made to fill gaps")
    ambiguities: list[Text] = Field(max_length=30, description="Open questions that remain")


class TaskFeatures(ToolArgs):
    language: ShortText = Field(description="Programming language; must match the chosen stack")
    framework: ShortText | None = Field(description="Main framework, or null")
    multi_file: bool
    size: Literal["small", "medium", "large"]


ComponentName = Annotated[str, StringConstraints(pattern=r"^[a-z][a-z0-9_-]{0,31}$")]


class ComponentItem(ToolArgs):
    name: ComponentName = Field(description="A short name, e.g. frontend or backend")
    stack: StackId = Field(description="An id from the stack catalog")
    path: ComponentName = Field(description="The component's folder in the project, one level deep")


class PlanTaskItem(ToolArgs):
    title: ShortText
    description: LongText
    capability: Literal["code_generation", "test_generation", "documentation"]
    requirement_refs: list[RequirementCode] = Field(min_length=1, description="Requirements this task implements")
    features: TaskFeatures
    component: ComponentName | None = Field(description="The component this task belongs to; null when the plan has no components")
    depends_on: list[Annotated[int, Field(ge=1, le=MAX_TASKS)]] = Field(max_length=MAX_TASKS, description="Numbers of the tasks that must be done first, in any component")

    @model_validator(mode="before")
    @classmethod
    def _single_stack_defaults(cls, data: Any) -> Any:
        """A plan written before components existed omits these: it means no component and no dependency."""
        return {"component": None, "depends_on": [], **data} if isinstance(data, dict) else data


class SubmitPlanArgs(ToolArgs):
    stack: StackId = Field(description="An id from the stack catalog; with components, the main component's stack")
    stack_rationale: Text
    summary: LongText
    tasks: list[PlanTaskItem] = Field(min_length=1, max_length=MAX_TASKS)
    components: list[ComponentItem] | None = Field(max_length=8, description="The project's parts, each with its own stack and folder; null for a one-stack project")

    @model_validator(mode="before")
    @classmethod
    def _one_stack_default(cls, data: Any) -> Any:
        return {"components": None, **data} if isinstance(data, dict) else data


class CriterionItem(ToolArgs):
    code: CriterionCode = Field(description="AC-001, AC-002, ... numbered in order")
    given: Text
    when: Text
    then: Text = Field(description="An observable result: output, return value, status code, file or UI state")
    priority: Literal["MUST", "SHOULD", "COULD"]
    method: Literal["unit", "api", "e2e", "static", "manual"]
    requirement_refs: list[RequirementCode] = Field(min_length=1)
    browser: BrowserCheckSpec | None = Field(description="Websites only: the browser check that proves this criterion, using what a visitor sees "
                                                     "(roles and accessible names, labels, visible text). Required for every automated criterion of a website; null otherwise")

    @model_validator(mode="before")
    @classmethod
    def _no_browser_by_default(cls, data: Any) -> Any:
        """Criteria written before browser checks existed omit the field: it means no browser check."""
        return {"browser": None, **data} if isinstance(data, dict) else data


class SubmitCriteriaArgs(ToolArgs):
    criteria: list[CriterionItem] = Field(min_length=1, max_length=MAX_CRITERIA)


class RequestHumanInputArgs(ToolArgs):
    question: Text = Field(description="One clear question for the user")
    reason: Text = Field(description="Why a reasonable assumption is not safe here")


# --- Handlers ------------------------------------------------------------------


def submit_analysis(ctx: ToolContext, args: SubmitAnalysisArgs) -> dict[str, object]:
    problems: list[str] = []
    codes = [r.code for r in args.requirements]
    expected = [f"R-{i:03d}" for i in range(1, len(codes) + 1)]
    if codes != expected:
        problems.append(f"requirement codes must be {_preview(expected)} in order; got {_preview(codes)}")
    seen: dict[str, str] = {}
    for item in args.requirements:
        key = item.text.casefold()
        if key in seen:
            problems.append(f"{item.code} repeats {seen[key]}")
        seen.setdefault(key, item.code)
    _reject_if(problems, "submit_analysis")

    for item in args.requirements:
        ctx.session.add(Requirement(run_id=ctx.run.id, code=item.code, text=item.text, kind=item.kind))
    ctx.run.analysis_json = json.dumps(
        {"summary": args.summary, "assumptions": args.assumptions, "ambiguities": args.ambiguities}
    )
    ctx.events.record(
        ctx.session,
        project_id=ctx.project.id,
        run_id=ctx.run.id,
        type="analysis.accepted",
        payload={"requirements": len(codes), "assumptions": len(args.assumptions), "ambiguities": len(args.ambiguities)},
    )
    change_state(ctx.session, ctx.run, RunState.PLANNING, ctx.events, reason="submit_analysis accepted")
    return {"accepted": True, "requirements": codes, "next_state": RunState.PLANNING.value}


def submit_plan(ctx: ToolContext, args: SubmitPlanArgs) -> dict[str, object]:
    from app.tools.adapters.gap import needs_gap_analysis, recorded_gap  # imported here: the gap tool reads the repository adapter's records

    if needs_gap_analysis(ctx) and recorded_gap(ctx.session, ctx.run.id) is None:
        raise ToolError(ErrorClass.VALIDATION_FAILED, "this project starts from existing code: call submit_gap_analysis first, so the plan builds on what exists")
    problems: list[str] = []
    known = set(_requirement_codes(ctx))
    stack = ctx.catalog.get(args.stack)
    if stack is None:
        problems.append(f"unknown stack {args.stack!r}; choose one of: {', '.join(ctx.catalog.ids)}")

    components = {c.name: c for c in args.components or []}
    component_stacks = {}
    if args.components:
        for label, values in (("names", [c.name for c in args.components]), ("folders", [c.path for c in args.components])):
            if len(set(values)) != len(values):
                problems.append(f"component {label} must be unique")
        for c in args.components:
            component_stacks[c.name] = ctx.catalog.get(c.stack)
            if component_stacks[c.name] is None:
                problems.append(f"component {c.name}: unknown stack {c.stack!r}; choose one of: {', '.join(ctx.catalog.ids)}")
        if args.stack not in {c.stack for c in args.components}:
            problems.append(f"stack {args.stack!r} must be the stack of one of the components")

    titles: dict[str, int] = {}
    covered: set[str] = set()
    for i, task in enumerate(args.tasks, start=1):
        key = task.title.casefold()
        if key in titles:
            problems.append(f"task {i} has the same title as task {titles[key]}")
        titles.setdefault(key, i)
        unknown = sorted(set(task.requirement_refs) - known)
        if unknown:
            problems.append(f"task {i} references unknown requirements: {', '.join(unknown)}")
        covered.update(task.requirement_refs)
        task_stack = stack
        if args.components:
            if task.component not in components:
                problems.append(f"task {i} must name one of the components: {', '.join(components)}")
                task_stack = None
            else:
                task_stack = component_stacks.get(task.component)
        elif task.component is not None:
            problems.append(f"task {i} names component {task.component!r}, but the plan has no components")
        if task_stack is not None and task.features.language.casefold() != task_stack.language.casefold():
            problems.append(f"task {i} language {task.features.language!r} does not match its stack's language {task_stack.language!r}")
        for dependency in task.depends_on:
            if dependency == i or dependency > len(args.tasks):
                problems.append(f"task {i} cannot depend on task {dependency}")
    cycle = _dependency_cycle({i: t.depends_on for i, t in enumerate(args.tasks, start=1)})
    if cycle:
        problems.append(f"task dependencies form a cycle: {' -> '.join(map(str, cycle))}")
    unused = sorted(set(components) - {t.component for t in args.tasks})
    if unused:
        problems.append(f"components without any task: {', '.join(unused)}")
    uncovered = sorted(known - covered)
    if uncovered:
        problems.append(f"requirements not covered by any task: {', '.join(uncovered)}")
    if not any(t.capability == "code_generation" for t in args.tasks):
        problems.append("at least one task must have capability code_generation")
    if ctx.project.stack is not None and stack is not None and stack.id != ctx.project.stack:
        problems.append(f"the user selected stack {ctx.project.stack}, but the plan selected {stack.id}")
    rag = [event_payload(e) for e in list_events(ctx.session, run_id=ctx.run.id, limit=5_000) if e.type == "rag.blueprint"]
    if rag:  # a RAG application keeps its blueprint's components, stacks and dependencies
        problems += plan_problems([c.model_dump() for c in args.components or []], [(t.component, t.depends_on) for t in args.tasks], rag[-1])
    _reject_if(problems, "submit_plan")

    assert stack is not None
    for i, task in enumerate(args.tasks, start=1):
        ctx.session.add(
            PlanTask(
                run_id=ctx.run.id,
                order=i,
                title=task.title,
                description=task.description,
                capability=task.capability,
                features_json=task.features.model_dump_json(),
                requirement_refs_json=json.dumps(task.requirement_refs),
                component=task.component,
                depends_on_json=json.dumps(sorted(set(task.depends_on))),
            )
        )
    ctx.run.plan_json = json.dumps({"stack": stack.id, "stack_rationale": args.stack_rationale, "summary": args.summary,
                                    "components": [c.model_dump() for c in args.components or []]})  # fmt: skip
    ctx.project.stack = stack.id
    ctx.events.record(
        ctx.session,
        project_id=ctx.project.id,
        run_id=ctx.run.id,
        type="plan.accepted",
        payload={"stack": stack.id, "tasks": len(args.tasks)},
    )
    change_state(ctx.session, ctx.run, RunState.DEFINING_CRITERIA, ctx.events, reason="submit_plan accepted")
    return {
        "accepted": True,
        "stack": stack.id,
        "tasks": len(args.tasks),
        "allowed_methods": [*stack.test_methods, "manual"],
        "next_state": RunState.DEFINING_CRITERIA.value,
    }


def submit_criteria(ctx: ToolContext, args: SubmitCriteriaArgs) -> dict[str, object]:
    problems: list[str] = []
    known = set(_requirement_codes(ctx))
    stack = ctx.catalog.get(ctx.project.stack or "")
    if stack is None:
        problems.append("the project has no valid stack; the plan must be accepted first")
    allowed_methods = set(stack.test_methods) if stack else set()
    for component in json.loads(ctx.run.plan_json or "{}").get("components") or []:  # each component's stack brings its own methods
        component_stack = ctx.catalog.get(component["stack"])
        allowed_methods |= set(component_stack.test_methods) if component_stack else set()

    codes = [c.code for c in args.criteria]
    expected = [f"AC-{i:03d}" for i in range(1, len(codes) + 1)]
    if codes != expected:
        problems.append(f"criterion codes must be {_preview(expected)} in order; got {_preview(codes)}")
    referenced: set[str] = set()
    for c in args.criteria:
        unknown = sorted(set(c.requirement_refs) - known)
        if unknown:
            problems.append(f"{c.code} references unknown requirements: {', '.join(unknown)}")
        referenced.update(c.requirement_refs)
        if c.priority == "MUST" and c.method == "manual":
            problems.append(f"{c.code} is MUST but uses the manual method; MUST criteria need an automated method")
        if c.method != "manual" and stack is not None and c.method not in allowed_methods:
            problems.append(
                f"{c.code} uses method {c.method!r}, which the plan's stack {stack.id} does not support "
                f"(allowed: {', '.join(sorted(allowed_methods))}, manual)"
            )
    missing = sorted(known - referenced)
    if missing:
        problems.append(f"requirements without any criterion: {', '.join(missing)}")
    if not any(c.priority == "MUST" for c in args.criteria):
        problems.append("at least one criterion must be MUST")
    website = stack is not None and stack.id in WEBSITE_STACKS and not json.loads(ctx.run.plan_json or "{}").get("components")
    for c in args.criteria:
        if website and c.method != "manual" and c.browser is None:
            problems.append(f"{c.code} is an automated criterion of a website and needs a `browser` check that proves it")
        elif not website and c.browser is not None:
            problems.append(f"{c.code} has a `browser` check, but browser checks are only for one-stack website projects")
        if c.browser is not None:
            problems += browser_acceptance.step_problems(c.browser.steps, c.code)
    _reject_if(problems, "submit_criteria")

    for c in args.criteria:
        ctx.session.add(
            AcceptanceCriterion(
                run_id=ctx.run.id,
                code=c.code,
                given=c.given,
                when=c.when,
                then=c.then,
                priority=c.priority,
                method=c.method,
                requirement_refs_json=json.dumps(c.requirement_refs),
            )
        )
    must = sum(1 for c in args.criteria if c.priority == "MUST")
    if website:  # frozen now, before any code exists; the coding provider never receives them
        frozen, digest = browser_acceptance.freeze(
            {"code": c.code, "priority": c.priority, "start": c.browser.start, "steps": [s.model_dump() for s in c.browser.steps]} for c in args.criteria if c.browser is not None
        )
        ctx.run.browser_acceptance_json, ctx.run.browser_acceptance_sha256 = frozen, digest
        ctx.events.record(ctx.session, project_id=ctx.project.id, run_id=ctx.run.id, type="acceptance.frozen",
                          payload={"kind": browser_acceptance.KIND, "checks": sum(c.browser is not None for c in args.criteria), "sha256": digest})  # fmt: skip
    ctx.events.record(
        ctx.session,
        project_id=ctx.project.id,
        run_id=ctx.run.id,
        type="criteria.accepted",
        payload={"criteria": len(codes), "must": must, "requirement_coverage": 1.0},
    )
    change_state(ctx.session, ctx.run, RunState.IMPLEMENTING, ctx.events, reason="submit_criteria accepted")
    return {"accepted": True, "criteria": len(codes), "must": must, "next_state": RunState.IMPLEMENTING.value}


# --- Registration ----------------------------------------------------------------


def register_phase2_tools(registry: ToolRegistry) -> None:
    registry.register(
        ToolSpec(
            name="submit_analysis",
            description="Submit the requirement analysis: numbered atomic requirements, assumptions and ambiguities.",
            args_model=SubmitAnalysisArgs,
            risk=RiskLevel.READ,
            states=frozenset({RunState.ANALYZING}),
            handler=submit_analysis,
        )
    )
    registry.register(
        ToolSpec(
            name="submit_plan",
            description="Submit the development plan: one stack from the catalog and tasks that cover every requirement.",
            args_model=SubmitPlanArgs,
            risk=RiskLevel.READ,
            states=frozenset({RunState.PLANNING}),
            handler=submit_plan,
        )
    )
    registry.register(
        ToolSpec(
            name="submit_criteria",
            description="Submit acceptance criteria (given/when/then) that cover every requirement.",
            args_model=SubmitCriteriaArgs,
            risk=RiskLevel.READ,
            states=frozenset({RunState.DEFINING_CRITERIA}),
            handler=submit_criteria,
        )
    )
    registry.register(
        ToolSpec(
            name="request_human_input",
            description="Pause the run and ask the user one question. Use only when a safe assumption is not possible.",
            args_model=RequestHumanInputArgs,
            risk=RiskLevel.READ,
            states=None,
            handler=None,
            asks_human=True,
        )
    )


def _requirement_codes(ctx: ToolContext) -> list[str]:
    return list(ctx.session.scalars(select(Requirement.code).where(Requirement.run_id == ctx.run.id)))


def _dependency_cycle(depends_on: dict[int, list[int]]) -> list[int]:
    """One cycle among the task dependencies, as a path, or [] when there is none."""
    state: dict[int, int] = {}  # 1: being visited, 2: done

    def visit(task: int, path: list[int]) -> list[int]:
        state[task] = 1
        for dependency in depends_on.get(task, []):
            if state.get(dependency) == 1:
                return [*path[path.index(dependency):], dependency] if dependency in path else [task, dependency]
            if dependency in depends_on and state.get(dependency) is None:
                found = visit(dependency, [*path, dependency])
                if found:
                    return found
        state[task] = 2
        return []

    for task in sorted(depends_on):
        if state.get(task) is None:
            found = visit(task, [task])
            if found:
                return found
    return []


def _reject_if(problems: list[str], tool: str) -> None:
    if problems:
        raise ToolError(
            ErrorClass.VALIDATION_FAILED, f"{tool} was rejected; fix every problem listed and call it again", problems
        )


def _preview(codes: list[str], limit: int = 6) -> str:
    shown = ", ".join(codes[:limit])
    return shown + (", ..." if len(codes) > limit else "")
