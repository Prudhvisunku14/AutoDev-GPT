"""generate_code, submit_implementation and finish: from a plan to delivered, verified files.

GPT stays the only orchestrator: it chooses what to do next, and these tools do it. Python owns
everything that must not depend on a model's good behaviour:

- The brief comes from the saved plan, and Python chooses the coding provider.
- A coding attempt changes only the run's disposable copy. The real project is written once, by
  `finish`, after the tests passed, and only with the files that were tested.
- Every attempt goes through `AttemptGuard`. The writer is stopped and confirmed gone before the audit
  reads a file, and a refused attempt is restored only through the guard.
- The tests run in the Docker sandbox, and the verdict comes from what the sandbox really did.
- Recovery is bounded in code (app/recovery/). When it says STOP, the run ends.

The guard saves in its own transactions, so all of its work is in the `run` step, where no database
transaction is open.
"""

from __future__ import annotations

import json
import logging
import secrets
import threading
import time
from dataclasses import dataclass, field, replace
from functools import partial
from pathlib import Path
from typing import Annotated, Any

from pydantic import Field, StringConstraints
from sqlalchemy import func, select, update
from sqlalchemy.orm import Session, sessionmaker

from app.coding.audit import Checkpoint, audit, take_snapshot
from app.coding.brief import BriefInput, build_brief
from app.coding.candidate import CandidateError, CandidateWorkspaces
from app.coding.launcher import CodingLauncher, CodingSession, LaunchRefused, LaunchRequest
from app.coding.session_control import AttemptGuard, AttemptRecord, AttemptRefused, AttemptStore, ControlError, RestoreForbidden, StopPolicy, workspace_fingerprint
from app.coding.types import AttemptPhase, CodingOutcome, CodingStatus, CodingTask, Progress, Termination
from app.config import Settings
from app.db.models import TERMINAL_RUN_STATES, AcceptanceCriterion, CodingAttempt, PlanTask, Requirement, Run, RunState, ToolInvocation
from app.orchestrator.budget import turn_cost_usd
from app.orchestrator.llm_client import Usage
from app.orchestrator.run_control import change_state, complete_run, end_run
from app.recovery.diagnosis import AttemptSignal, FailureCategory, classify_attempt, classify_refusal, classify_verification
from app.recovery.engine import REPAIRABLE, Action, Decision, decide_coding, decide_verification
from app.recovery.policy import RecoveryConfig, RecoveryMemory, RecoveryState, ToolOptions, decide, longer_timeout
from app.sandbox.docker_cli import SandboxError, SandboxUnavailable
from app.sandbox.policy import SandboxPolicyError, build_spec
from app.sandbox.runner import SandboxRunner
from app.sandbox.spec import ExecResult, ExecSpec, ExecStatus
from app.selection.history import attempt_seconds, evidence_of, failed_in_run, failed_tools, last_attempt, load_history, record_attempt, repair_record, settle_verification
from app.selection.model import BUILD, REPAIR, SelectionContext, task_category
from app.selection.selector import NoProvider, Selector
from app.tools.adapters.repositories import imported_record
from app.tools.adapters.workspace_fs import WorkspaceError, WorkspaceFS
from app.tools.base import ErrorClass, ExternalStep, RecoveryInfo, RiskLevel, ToolArgs, ToolContext, ToolError, ToolSpec
from app.tools.gates import MAX_TASKS
from app.tools.registry import ToolRegistry
from app.verification import acceptance
from app.verification.recipes import command_for, dependency_problems, judge_recipe, readiness, recipe_for
from app.verification.report import build_report
from app.verification.results import Criterion, CriterionStatus, TestCounts, Verdict, VerificationResult, failure_kind, judge, judge_criteria

logger = logging.getLogger("devpilot.coding")

LongText = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=2000)]
_FINISHED_TASK = "DONE"
_FAILED_TASK = "FAILED"
_RESTORABLE_ERRORS = (RestoreForbidden, OSError, ValueError)
_SAME_TOOL = frozenset({Action.REPAIR.value, Action.RETRY.value, Action.RECONFIGURE.value})  # recovery chose to stay on the tool


@dataclass(frozen=True)
class CodingRuntime:
    """What the coding tools need beyond the per-call context."""

    selector: Selector
    store: AttemptStore
    candidates: CandidateWorkspaces
    sandbox: SandboxRunner
    gpt_label: str = "OpenAI Responses API"
    stop_policy: StopPolicy = field(default_factory=StopPolicy)


# --- Arguments ------------------------------------------------------------------------------------


class GenerateCodeArgs(ToolArgs):
    task_order: int = Field(ge=1, le=MAX_TASKS, description="The number of the plan task to implement or fix")
    instructions: LongText = Field(description="What to do for this task. After a failed verification, say what to fix.")


class SubmitImplementationArgs(ToolArgs):
    summary: LongText = Field(description="What was built, in a sentence or two")


class FinishArgs(ToolArgs):
    summary: LongText = Field(description="The final summary for the user")


# --- generate_code --------------------------------------------------------------------------------


@dataclass(frozen=True)
class _CodingPlan:
    project_id: str
    run_id: str
    call_id: str
    task_order: int
    task: CodingTask
    root: Path
    checkpoint: Checkpoint
    before: Any  # the snapshot the audit compares with
    launcher: CodingLauncher
    provider: str
    simulated: bool
    settings: Settings
    usd_budget: float | None = None
    category: str = ""  # which attempts this one is comparable with
    evidence: str = ""  # whether this attempt may inform a real tool's history
    selection: dict[str, Any] = field(default_factory=dict)  # what GPT is told about the choice
    recovers_from: str | None = None  # the failure this attempt is meant to fix
    recovery_action: str | None = None
    folder: str | None = None  # a component task may change only this folder


@dataclass
class _CodingResult:
    outcome: CodingOutcome
    kept: bool = False
    changes: list[dict[str, object]] = field(default_factory=list)
    violations: list[str] = field(default_factory=list)
    phase: str = ""
    quarantined: bool = False
    restored: bool = False
    detail: str = ""
    ledger: dict[str, Any] = field(default_factory=dict)  # what the gateway really counted
    seconds: float | None = None


def _begin_generate_code(runtime: CodingRuntime, ctx: ToolContext, args: GenerateCodeArgs) -> _CodingPlan:
    run, project = ctx.run, ctx.project
    task = ctx.session.scalars(select(PlanTask).where(PlanTask.run_id == run.id, PlanTask.order == args.task_order)).first()
    if task is None:
        raise ToolError(ErrorClass.INVALID_ARGS, f"the plan has no task {args.task_order}")
    blocked = _verification_blocked(ctx)
    if blocked:  # code that cannot be verified is never written: the run stops and says exactly why
        why = "; ".join(f"{b['component']} ({b['stack']}): {b['reason']}" for b in blocked)
        raise ToolError(ErrorClass.POLICY_REFUSED, f"verification is blocked, so no code is written: {why}", [b["reason"] for b in blocked],
                        after_rollback=partial(_stop_blocked, runtime, ctx, blocked))  # fmt: skip
    waiting = _waiting_for(ctx, task)
    if waiting:
        raise ToolError(ErrorClass.POLICY_REFUSED, f"task {task.order} depends on tasks that are not done yet: {', '.join(map(str, waiting))}")
    stack_id = _task_stack(ctx, task)
    stack = ctx.catalog.get(stack_id or "")
    if stack is None or (stack.test_command is None and recipe_for(stack.id) is None):  # readiness above already required an approved recipe
        raise ToolError(ErrorClass.POLICY_REFUSED, f"stack {stack_id} cannot be verified yet, so no code is written for it; python-cli can")
    if ctx.workspace is None or ctx.call_id is None:
        raise ToolError(ErrorClass.TOOL_ERROR, "the workspace is not configured for this backend")
    left = _money_left(ctx)  # attempts together stay inside the run's budget
    context = _selection_context(ctx, task.capability, task.order, stack.id, left)
    try:
        selection = runtime.selector.choose(task.capability, context)
    except NoProvider as exc:
        raise ToolError(ErrorClass.POLICY_REFUSED, str(exc), after_rollback=partial(_no_tool_left, runtime, ctx, task.order, exc)) from exc
    memory = RecoveryMemory.load(run.recovery_json)
    directive = memory if memory.applies_to(task.order) else RecoveryMemory()
    try:
        root = runtime.candidates.open_or_create(run.id, ctx.workspace.project_dir(project.id))
    except (CandidateError, WorkspaceError) as exc:
        raise ToolError(ErrorClass.POLICY_REFUSED, str(exc)) from exc
    before = take_snapshot(root)
    checkpoint = Checkpoint.create(ctx.settings.coding_checkpoints_dir, run.id, ctx.call_id, root, before)
    ctx.session.execute(
        update(ToolInvocation)
        .where(ToolInvocation.run_id == run.id, ToolInvocation.call_id == ctx.call_id)
        .values(capability=task.capability, provider=selection.provider)
        .execution_options(synchronize_session=False)
    )
    _record(ctx, "selection.chosen", {"task": task.order, "capability": task.capability, "provider": selection.provider,
                                      "simulated": selection.simulated, "excluded": selection.excluded, **selection.details()})  # fmt: skip
    brief = build_brief(_brief_input(ctx, task, args, stack_id=stack.id, language=stack.language, files=sorted(before.files)))
    spent = run.tokens_in + run.tokens_out
    coding_task = CodingTask(
        key=f"{run.id}:{task.order}", title=task.title, brief=brief, session_id=None, abort_first=False,
        max_tokens=min(ctx.settings.opencode_max_tokens_per_task, max(ctx.settings.run_max_tokens - spent, 1)),
        timeout_seconds=directive.timeout_seconds or ctx.settings.opencode_timeout_seconds, workspace=root,
    )  # fmt: skip
    _record(ctx, "coding.started", {"task": task.order, "title": task.title, "provider": selection.provider, "files_before": len(before.files)})
    return _CodingPlan(
        project.id, run.id, ctx.call_id, task.order, coding_task, root, checkpoint, before, selection.launcher, selection.provider, selection.simulated, ctx.settings, left,
        category=task_category(task.capability, stack.id, context.kind), evidence=evidence_of(simulated=selection.simulated, sandbox=runtime.sandbox), selection=selection.summary(),
        recovers_from=directive.category or None, recovery_action=directive.action or None,
        folder=next((c["path"] for c in _components(ctx) if c["name"] and c["name"] == task.component), None),
    )  # fmt: skip


def _money_left(ctx: ToolContext) -> float | None:
    return max(ctx.settings.run_budget_usd - ctx.run.cost_usd, 0.0) if ctx.run.cost_known else None


def _selection_context(ctx: ToolContext, capability: str, task_order: int, stack_id: str, left: float | None, *, kind: str | None = None) -> SelectionContext:
    """What this choice depends on. GPT asked for the capability: everything else here comes from the run's records."""
    run = ctx.run
    kind = kind or (REPAIR if run.state is RunState.REPAIRING else BUILD)
    memory = RecoveryMemory.load(run.recovery_json)
    return SelectionContext(
        stack=stack_id, kind=kind, remaining_usd=left, remaining_tokens=max(ctx.settings.run_max_tokens - run.tokens_in - run.tokens_out, 0),
        history=load_history(ctx.session, task_category(capability, stack_id, kind)), failed_here=failed_tools(ctx.session, f"{run.id}:{task_order}"),
        demote_failed=not (memory.applies_to(task_order) and memory.action in _SAME_TOOL),
    )  # fmt: skip


def _brief_input(ctx: ToolContext, task: PlanTask, args: GenerateCodeArgs, *, stack_id: str, language: str, files: list[str]) -> BriefInput:
    run = ctx.run
    refs = set(json.loads(task.requirement_refs_json))
    requirements = [(r.code, r.text) for r in ctx.session.scalars(select(Requirement).where(Requirement.run_id == run.id).order_by(Requirement.code)) if r.code in refs]
    criteria = [
        (c.code, c.priority, f"given {c.given}, when {c.when}, then {c.then}")
        for c in ctx.session.scalars(select(AcceptanceCriterion).where(AcceptanceCriterion.run_id == run.id).order_by(AcceptanceCriterion.code))
    ]
    total = ctx.session.scalar(select(func.count()).select_from(PlanTask).where(PlanTask.run_id == run.id)) or 0
    return BriefInput(
        task_order=task.order, total_tasks=total, title=task.title, description=task.description, capability=task.capability,
        requirements=requirements, criteria=criteria, stack_id=stack_id, language=language, instructions=args.instructions,
        existing_files=files, last_failure=_last_failure(run),
    )  # fmt: skip


def _last_failure(run: Run) -> str:
    if run.state is not RunState.REPAIRING or not run.verification_json:
        return ""
    last = json.loads(run.verification_json)
    memory = RecoveryMemory.load(run.recovery_json)
    diagnosis = f"Diagnosis: {memory.category}. {memory.evidence}\n\n" if memory.category else ""
    return f"{diagnosis}{last['reason']}\n\n{last['output_tail']}"


class _LateWriter:
    """The guard needs a writer before the container exists. Until one is bound, nothing can be confirmed."""

    def __init__(self) -> None:
        self._writer: Any = None

    def bind(self, writer: Any) -> None:
        self._writer = writer

    def terminate(self) -> Termination:
        return Termination(False, "no container was started") if self._writer is None else self._writer.terminate()


def _run_generate_code(runtime: CodingRuntime, plan: _CodingPlan, cancel: threading.Event | None, progress: Progress) -> _CodingResult:
    began = time.monotonic()
    result = _attempt(runtime, plan, cancel, progress)
    result.seconds = time.monotonic() - began
    return result


def _attempt(runtime: CodingRuntime, plan: _CodingPlan, cancel: threading.Event | None, progress: Progress) -> _CodingResult:
    """With no transaction open: start the engine, stop it, audit, and keep or undo. Never leaves a writer."""
    writer = _LateWriter()
    try:
        guard = AttemptGuard.open(
            store=runtime.store, project_id=plan.project_id, run_id=plan.run_id, call_id=plan.call_id, task_key=plan.task.key, writer=writer,
            fingerprint=partial(workspace_fingerprint, plan.root), policy=runtime.stop_policy, progress=progress,
        )  # fmt: skip
    except AttemptRefused as exc:
        plan.checkpoint.discard()
        return _CodingResult(CodingOutcome(status=CodingStatus.BUSY, detail=str(exc)), phase="REFUSED", detail=str(exc))
    request = LaunchRequest(run_id=plan.run_id, call_id=plan.call_id, workspace=plan.root, token_budget=plan.task.max_tokens, usd_budget=plan.usd_budget)
    try:
        session = plan.launcher.start(request)
    except LaunchRefused as exc:
        guard.abandon(str(exc))
        plan.checkpoint.discard()
        return _CodingResult(CodingOutcome(status=CodingStatus.UNAVAILABLE, detail=str(exc)), phase=str(guard.phase), detail=str(exc))
    writer.bind(session.writer)
    guard.record.container_id = session.container_id
    runtime.store.save(guard.record)
    try:
        try:
            outcome = session.provider.run_task(plan.task, cancel=cancel, progress=progress, guard=guard)
        except Exception:  # a bug must not leave a prompt running, or the project held
            logger.exception("the coding provider raised on run %s", plan.run_id)
            outcome = CodingOutcome(status=CodingStatus.FAILED, detail="the coding provider stopped with an internal error")
        result = _settle(plan, guard, session, outcome)
    finally:
        session.close()
    result.ledger = session.usage
    if not result.quarantined:
        plan.checkpoint.discard()
    return result


def _settle(plan: _CodingPlan, guard: AttemptGuard, session: CodingSession, outcome: CodingOutcome) -> _CodingResult:
    """Keep the attempt, or stop it and undo it. A kept attempt was audited after its writer stopped."""
    changes: list[dict[str, object]] = []
    violations: list[str] = []
    reason = outcome.detail or f"the attempt ended with {outcome.status}"
    if guard.phase is AttemptPhase.FINISHED and outcome.status is CodingStatus.COMPLETED:
        stopped = session.writer.terminate()
        if stopped.confirmed:
            report = audit(
                plan.root, plan.before, take_snapshot(plan.root), max_file_bytes=plan.settings.workspace_max_file_bytes,
                max_files=plan.settings.workspace_max_files, secrets=plan.settings.secret_values(),
            )  # fmt: skip
            changes = [c.to_dict() for c in report.changes]
            outside = [c.path for c in report.changes if plan.folder and not c.path.startswith(f"{plan.folder}/")]
            violations = [*report.violations, *(f"{p}: outside this task's component folder {plan.folder}/" for p in outside)]
            if not violations:
                guard.keep()
                return _CodingResult(outcome, kept=True, changes=changes, phase=str(guard.phase))
            outcome.status = CodingStatus.POLICY_VIOLATION
            reason = "the audit refused the changes: " + "; ".join(violations[:5])
        else:
            outcome.status = CodingStatus.FAILED
            reason = f"the coding container could not be confirmed stopped: {stopped.evidence}"
    report_after = guard.secure(session.control, reason)
    restored = False
    if report_after.safe_to_restore:
        try:
            guard.restore(plan.checkpoint, plan.root)
            restored = True
        except _RESTORABLE_ERRORS:
            logger.exception("restoring the disposable copy failed for run %s", plan.run_id)
    final = guard.report()
    outcome.detail = reason
    return _CodingResult(outcome, changes=[], violations=violations, phase=str(final.phase), quarantined=final.quarantined, restored=restored, detail=final.detail)


def attempt_cost(ledger: dict[str, Any], allowance: float | None) -> float | None:
    """What an attempt spent, by its gateway's ledger. A ledger that could not be read counts as the
    whole allowance, because spending must never go unseen. None when no gateway was involved."""
    if not ledger:
        return None
    if ledger.get("cost_usd") is not None:
        return float(ledger["cost_usd"])
    return (allowance or 0.0) if not ledger.get("all_known") else 0.0


def _finish_generate_code(runtime: CodingRuntime, ctx: ToolContext, plan: _CodingPlan, result: _CodingResult) -> dict[str, object]:
    run = ctx.run
    outcome = result.outcome
    task = ctx.session.scalars(select(PlanTask).where(PlanTask.run_id == run.id, PlanTask.order == plan.task_order)).one()
    run.tokens_in += outcome.tokens_in
    run.tokens_out += outcome.tokens_out
    # The gateway's ledger is what was really spent. The engine's own figures are the fallback.
    unreported = max(result.ledger.get("charged_tokens", 0) - outcome.tokens_in - outcome.tokens_out, 0)
    run.tokens_in += unreported
    spent: float | None = None
    if run.cost_known:
        spent = attempt_cost(result.ledger, plan.usd_budget)
        if spent is None:
            spent = outcome.cost_usd if outcome.cost_usd is not None else turn_cost_usd(Usage(input_tokens=outcome.tokens_in + unreported, output_tokens=outcome.tokens_out), ctx.settings)
        run.cost_usd += spent or 0.0
    record_attempt(
        ctx.session, run_id=run.id, call_id=plan.call_id, task_key=plan.task.key, tool_id=plan.provider, category=plan.category, evidence=plan.evidence,
        status=outcome.status, kept=result.kept, seconds=result.seconds, cost_usd=spent, recovers_from=plan.recovers_from, recovery_action=plan.recovery_action,
    )  # fmt: skip
    data: dict[str, object] = {
        "task_order": task.order, "title": task.title, "status": outcome.status.value, "kept": result.kept, "provider": plan.provider,
        "simulated": plan.simulated, "changes": result.changes, "violations": result.violations, "phase": result.phase,
        "tokens": outcome.tokens_in + outcome.tokens_out, "detail": outcome.detail,
    }  # fmt: skip
    _record(ctx, "coding.finished", {**data, "restored": result.restored, "quarantined": result.quarantined, "stop": result.detail})
    data["selection"] = plan.selection
    if result.kept:
        task.status = _FINISHED_TASK
        data["all_tasks_done"] = not _unfinished_tasks(ctx)
        return data
    task.status = _FAILED_TASK
    used = ctx.session.scalar(
        select(func.count()).select_from(CodingAttempt).where(CodingAttempt.run_id == run.id, CodingAttempt.task_key == plan.task.key, CodingAttempt.phase != AttemptPhase.KEPT)
    ) or 0  # fmt: skip
    decision = _after_attempt(runtime, ctx, plan, result, task, used)
    data["recovery"] = _decided(ctx, decision, task=task.order)
    if decision.action is Action.STOP:
        keep_copy = result.quarantined or decision.category == FailureCategory.AMBIGUOUS_STATE
        _end_failed(runtime, ctx, outcome="quarantined" if result.quarantined else "coding_failed", detail=f"{decision.reason}: {outcome.detail}", keep_copy=keep_copy)
    return data


# --- Components and verification readiness -------------------------------------------------------


def _components(ctx: ToolContext) -> list[dict[str, Any]]:
    """The plan's components, or the project as one component when the plan has none."""
    plan = json.loads(ctx.run.plan_json or "{}")
    return plan.get("components") or [{"name": None, "stack": ctx.project.stack, "path": None}]


def _task_stack(ctx: ToolContext, task: PlanTask) -> str | None:
    if task.component is None:
        return ctx.project.stack
    return next((c["stack"] for c in _components(ctx) if c["name"] == task.component), None)


def _verification_blocked(ctx: ToolContext) -> list[dict[str, Any]]:
    """Every component whose stack cannot be verified now, with the exact reason. Empty when all can."""
    blocked = []
    for component in _components(ctx):
        ok, reason = readiness(component["stack"] or "")
        if not ok:
            blocked.append({"component": component["name"] or "the project", "stack": component["stack"], "reason": reason})
    return blocked


def _waiting_for(ctx: ToolContext, task: PlanTask) -> list[int]:
    needed = json.loads(task.depends_on_json or "[]")
    if not needed:
        return []
    rows = ctx.session.scalars(select(PlanTask).where(PlanTask.run_id == ctx.run.id, PlanTask.order.in_(needed)))
    return sorted(t.order for t in rows if t.status != _FINISHED_TASK)


def _stop_blocked(runtime: CodingRuntime, ctx: ToolContext, blocked: list[dict[str, Any]]) -> None:
    """Nothing was written. The run ends as stopped by policy, with a report that names every blocked component."""
    _record(ctx, "verification.blocked", {"components": blocked})
    if not ctx.run.is_terminal:
        detail = "verification is blocked: " + "; ".join(f"{b['component']} ({b['stack']}): {b['reason']}" for b in blocked)
        end_run(ctx.session, ctx.run, RunState.STOPPED_BY_POLICY, ctx.events, outcome="verification_unavailable", detail=detail, reason="verification_unavailable")
    ctx.run.report_json = json.dumps(_report(runtime, ctx))


def _begin_components_submit(runtime: CodingRuntime, ctx: ToolContext, args: SubmitImplementationArgs) -> _VerifyPlan:
    """Python picks each component's recipe; nothing is installed. A missing runtime or dependency stops the run."""
    run = ctx.run
    problems = [f"task {t.order} ({t.title}) is {t.status}" for t in _unfinished_tasks(ctx)]
    if not runtime.candidates.exists(run.id):
        problems.append("no code was generated yet")
    if problems:
        raise ToolError(ErrorClass.VALIDATION_FAILED, "the implementation is not ready to verify", problems)
    components = _components(ctx)
    files = acceptance.read_tree(runtime.candidates.path(run.id))
    blocked = _verification_blocked(ctx)
    for c in components:
        recipe = recipe_for(c["stack"])
        blocked += [{"component": c["name"], "stack": c["stack"], "reason": why} for why in (dependency_problems(recipe, files, c["path"]) if recipe else [])]
    if blocked:
        why = "; ".join(f"{b['component']} ({b['stack']}): {b['reason']}" for b in blocked)
        raise ToolError(ErrorClass.POLICY_REFUSED, f"verification is blocked: {why}", [b["reason"] for b in blocked], after_rollback=partial(_stop_blocked, runtime, ctx, blocked))
    root = runtime.candidates.path(run.id)
    parts = []
    for c in components:
        recipe = recipe_for(c["stack"])
        assert recipe is not None
        spec = ExecSpec(recipe.name, recipe.runtime.image, command_for(recipe, c["path"]), root, recipe.runtime.timeout_seconds, f"devpilot-sbx-{secrets.token_hex(8)}")
        parts.append((c["name"], recipe, spec))
    criteria = tuple(Criterion(c.code, c.priority) for c in ctx.session.scalars(select(AcceptanceCriterion).where(AcceptanceCriterion.run_id == run.id).order_by(AcceptanceCriterion.code)))
    python_image = next((r.runtime.image for _n, r, _s in parts if r.argv[0] == "python"), None)
    independent = _plan_acceptance(runtime, ctx, "python-unittest", python_image) if python_image else None
    if run.state is not RunState.VERIFYING:
        _change_state(ctx, RunState.VERIFYING, "submit_implementation accepted")
    _record(ctx, "verification.started", {"components": [{"component": n, "recipe": r.name, "image": r.runtime.image, "argv": list(s.argv)} for n, r, s in parts],
                                          "repairs_so_far": run.repairs, "summary": args.summary, "independent_acceptance": independent is not None})  # fmt: skip
    return _VerifyPlan(runtime.sandbox, parts[0][2], criteria, runtime.candidates.digest(run.id), independent, tuple(parts))


def _judge_components(ctx: ToolContext, plan: _VerifyPlan, verified: _Verified) -> VerificationResult:
    """Each component judged by its own recipe, with its own evidence. The project passes only when every component's
    tests pass and every MUST criterion is proved by a passing test somewhere."""
    parts = []
    for name, recipe, _spec in plan.components:
        v = judge_recipe(recipe, verified.components[name])
        _record(ctx, "verification.component", {"component": name, "recipe": recipe.name, "image": recipe.runtime.image, **_verification_event(v)})
        parts.append((name, v))
    counts = TestCounts(
        readable=all(v.counts.readable for _, v in parts), ran=sum(v.counts.ran for _, v in parts), failures=sum(v.counts.failures for _, v in parts),
        errors=sum(v.counts.errors for _, v in parts), skipped=sum(v.counts.skipped for _, v in parts), passed_names=tuple(n for _, v in parts for n in v.counts.passed_names),
        failed_names=tuple(n for _, v in parts for n in v.counts.failed_names), summary="; ".join(f"{name}: {v.counts.summary or v.verdict.value}" for name, v in parts),
    )  # fmt: skip
    judged = judge_criteria(plan.criteria, counts)
    lead = next((v for _, v in parts if v.verdict is Verdict.NOT_RUN), None) or next((v for _, v in parts if v.verdict is Verdict.FAILED), parts[0][1])
    unproved = [c.code for c in judged if c.priority == "MUST" and c.status is not CriterionStatus.PASSED]
    failing = [f"{name}: {v.reason}" for name, v in parts if v.verdict is not Verdict.PASSED]
    verdict = Verdict.NOT_RUN if lead.verdict is Verdict.NOT_RUN else Verdict.FAILED if failing or unproved else Verdict.PASSED
    reason = "; ".join(failing) or (f"no passing test proves {', '.join(unproved)}" if unproved else "every component's tests and every MUST criterion passed")
    return VerificationResult(verdict, reason, lead.exec_status, lead.exit_code, counts, judged, lead.output_tail, lead.container, sum(v.duration_ms for _, v in parts), lead.cleanup, plan.digest)


# --- Recovery -------------------------------------------------------------------------------------


def _tools_left(runtime: CodingRuntime, ctx: ToolContext, capability: str, *, failed: frozenset[str], kind: str) -> ToolOptions:
    """What could run next, by the selector's own rules: budget, tokens, memory and security all count."""
    stack = ctx.catalog.get(ctx.project.stack or "")
    if stack is None:
        return ToolOptions(can_run=False, why=f"stack {ctx.project.stack} is unknown")
    context = replace(_selection_context(ctx, capability, 0, stack.id, _money_left(ctx), kind=kind), failed_here=failed, demote_failed=True)
    try:
        selection = runtime.selector.choose(capability, context)
    except NoProvider as exc:
        return ToolOptions(can_run=False, why=str(exc))
    ranked = [(c.tool_id, c.simulated) for c in selection.report.ranking] if selection.report is not None else [(selection.provider, selection.simulated)]
    others = [(tool, simulated) for tool, simulated in ranked if tool not in failed]
    return ToolOptions(alternatives=tuple(tool for tool, _ in others), real_alternative=any(not simulated for _, simulated in others))


def _seconds_left(ctx: ToolContext) -> float:
    """`active_seconds` is only saved when a turn of the loop ends, so the attempts' own time keeps this honest."""
    used = max(ctx.run.active_seconds, attempt_seconds(ctx.session, ctx.run.id))
    return max(ctx.settings.run_wall_clock_seconds - used, 0.0)


def _after_attempt(runtime: CodingRuntime, ctx: ToolContext, plan: _CodingPlan, result: _CodingResult, task: PlanTask, used: int) -> Decision:
    settings, run = ctx.settings, ctx.run
    if settings.recovery_policy == "bounded":
        return decide_coding(result.outcome.status, quarantined=result.quarantined, attempts_used=used, max_attempts=settings.opencode_max_attempts_per_task)
    diagnosis = classify_attempt(AttemptSignal(task.order, result.outcome.status, result.phase, result.outcome.detail or "", tuple(result.violations)))
    memory = RecoveryMemory.load(run.recovery_json)
    left = _seconds_left(ctx)
    judged, verified = repair_record(ctx.session, tool_id=plan.provider, evidence=plan.evidence, category=diagnosis.category.value)
    kind = REPAIR if run.state is RunState.REPAIRING else BUILD
    state = RecoveryState(
        attempts_used=used, max_attempts=settings.opencode_max_attempts_per_task, repairs_used=run.repairs, max_repairs=settings.max_repair_attempts, streak=memory.streak_after(diagnosis),
        tools=_tools_left(runtime, ctx, task.capability, failed=failed_tools(ctx.session, plan.task.key) | {plan.provider}, kind=kind),
        repairs_judged=judged, repairs_verified=verified, seconds_left=left, seconds_needed=result.seconds or 0.0,
        next_timeout=longer_timeout(plan.task.timeout_seconds, ceiling=settings.recovery_attempt_timeout_ceiling, seconds_left=left),
    )  # fmt: skip
    decision = decide(diagnosis, state, RecoveryConfig.from_settings(settings))
    run.recovery_json = memory.after(diagnosis, decision, task=task.order).dump()
    return decision


def _after_verification(runtime: CodingRuntime, ctx: ToolContext, verification: VerificationResult, kind: str) -> Decision:
    settings, run = ctx.settings, ctx.run
    if settings.recovery_policy == "bounded":
        return decide_verification(kind, repairs_used=run.repairs, max_repairs=settings.max_repair_attempts)
    diagnosis = classify_verification(verification)
    memory = RecoveryMemory.load(run.recovery_json)
    last = last_attempt(ctx.session, run.id)
    tools, judged, verified, needed = ToolOptions(), 0, 0, 0.0
    if last is not None:
        tools = _tools_left(runtime, ctx, last.task_category.partition(":")[0], failed=failed_in_run(ctx.session, run.id), kind=REPAIR)
        judged, verified = repair_record(ctx.session, tool_id=last.tool_id, evidence=last.evidence, category=diagnosis.category.value)
        needed = (last.duration_ms or 0) / 1000
    state = RecoveryState(
        attempts_used=0, max_attempts=settings.opencode_max_attempts_per_task, repairs_used=run.repairs, max_repairs=settings.max_repair_attempts, streak=memory.streak_after(diagnosis),
        tools=tools, repairs_judged=judged, repairs_verified=verified, seconds_left=_seconds_left(ctx), seconds_needed=needed,
    )  # fmt: skip
    decision = decide(diagnosis, state, RecoveryConfig.from_settings(settings))
    run.recovery_json = memory.after(diagnosis, decision, task=None).dump()
    return decision


def _no_tool_left(runtime: CodingRuntime, ctx: ToolContext, task_order: int, refused: NoProvider) -> None:
    """Nothing ran, so stopping loses nothing, and asking again would meet the same refusal."""
    settings = ctx.settings
    if settings.recovery_policy == "bounded":
        return
    state = RecoveryState(attempts_used=0, max_attempts=settings.opencode_max_attempts_per_task, repairs_used=ctx.run.repairs, max_repairs=settings.max_repair_attempts)
    decision = decide(classify_refusal(refused.excluded), state, RecoveryConfig.from_settings(settings))
    _decided(ctx, decision, task=task_order)
    _end_failed(runtime, ctx, outcome="no_eligible_tool", detail=str(refused), keep_copy=False)


# --- submit_implementation ------------------------------------------------------------------------


@dataclass(frozen=True)
class _AcceptancePlan:
    spec: ExecSpec
    suite: acceptance.AcceptanceSuite
    sha256: str


@dataclass(frozen=True)
class _VerifyPlan:
    sandbox: SandboxRunner
    spec: ExecSpec
    criteria: tuple[Criterion, ...]
    digest: str
    independent: _AcceptancePlan | None = None  # the evaluator's frozen suite, when the project has one
    components: tuple[tuple[str, Any, ExecSpec], ...] = ()  # (name, recipe, spec) per component of a multi-stack plan


@dataclass(frozen=True)
class _Verified:
    tests: ExecResult  # the project's own tests, written by the coding provider
    independent: ExecResult | None  # the frozen acceptance suite; None when it did not run
    components: dict[str, ExecResult] = field(default_factory=dict)  # per component of a multi-stack plan


def _begin_submit(runtime: CodingRuntime, ctx: ToolContext, args: SubmitImplementationArgs) -> _VerifyPlan:
    if any(c["name"] for c in _components(ctx)):
        return _begin_components_submit(runtime, ctx, args)
    run = ctx.run
    stack = ctx.catalog.get(ctx.project.stack or "")
    problems = [f"task {t.order} ({t.title}) is {t.status}" for t in _unfinished_tasks(ctx)]
    if stack is None or stack.test_command is None:
        problems.append("this stack has no approved test command")
    if not runtime.candidates.exists(run.id):
        problems.append("no code was generated yet")
    if problems:
        raise ToolError(ErrorClass.VALIDATION_FAILED, "the implementation is not ready to verify", problems)
    assert stack is not None and stack.test_command is not None
    try:
        spec = build_spec(stack.test_command, None, workspace=runtime.candidates.fs, project_id=run.id, settings=ctx.settings)
    except SandboxPolicyError as exc:
        raise ToolError(ErrorClass.TOOL_ERROR, str(exc)) from exc
    criteria = tuple(Criterion(c.code, c.priority) for c in ctx.session.scalars(select(AcceptanceCriterion).where(AcceptanceCriterion.run_id == run.id).order_by(AcceptanceCriterion.code)))
    independent = _plan_acceptance(runtime, ctx, stack.test_command)
    if run.state is not RunState.VERIFYING:
        _change_state(ctx, RunState.VERIFYING, "submit_implementation accepted")
    _record(ctx, "verification.started", {"command": spec.command, "image": spec.image, "container": spec.container_name, "repairs_so_far": run.repairs, "summary": args.summary,
                                          "independent_acceptance": independent is not None})  # fmt: skip
    return _VerifyPlan(runtime.sandbox, spec, criteria, runtime.candidates.digest(run.id), independent)


def _acceptance_fs(settings: Settings) -> WorkspaceFS:
    return WorkspaceFS(settings.coding_work_dir / "acceptance", max_file_bytes=settings.workspace_max_file_bytes, max_files=settings.workspace_max_files)


def _plan_acceptance(runtime: CodingRuntime, ctx: ToolContext, command: str, image: str | None = None) -> _AcceptancePlan | None:
    """The frozen suite, staged with the candidate's files but not its tests. A changed copy ends the run."""
    run = ctx.run
    if run.acceptance_suite_json is None or run.acceptance_sha256 is None:
        return None
    try:
        suite = acceptance.thaw(run.acceptance_suite_json, run.acceptance_sha256)
    except acceptance.SuiteError as exc:
        why = str(exc)
        raise ToolError(ErrorClass.POLICY_REFUSED, why, after_rollback=partial(_end_failed, runtime, ctx, outcome="acceptance_suite_changed", detail=why, keep_copy=True)) from exc
    fs = _acceptance_fs(ctx.settings)
    try:
        acceptance.stage(fs, run.id, acceptance.evaluation_files(acceptance.read_tree(runtime.candidates.path(run.id)), suite))
        spec = build_spec(command, None, workspace=fs, project_id=run.id, settings=ctx.settings)
        spec = replace(spec, image=image) if image else spec  # a multi-stack project's suite runs in its Python component's runtime
    except (WorkspaceError, SandboxPolicyError) as exc:
        acceptance.discard(fs, run.id)
        raise ToolError(ErrorClass.TOOL_ERROR, f"the acceptance run could not be prepared: {exc}") from exc
    return _AcceptancePlan(spec, suite, run.acceptance_sha256)


def _run_verification(plan: _VerifyPlan, cancel: threading.Event | None, progress: Progress) -> _Verified:
    if plan.components:  # each component in its own recipe's runtime
        results = {name: _in_sandbox(plan.sandbox, spec, cancel) for name, _recipe, spec in plan.components}
        all_ran = all(r.status is ExecStatus.SUCCEEDED for r in results.values())
        independent = _in_sandbox(plan.sandbox, plan.independent.spec, cancel) if plan.independent is not None and all_ran else None
        return _Verified(next(iter(results.values())), independent, results)
    tests = _in_sandbox(plan.sandbox, plan.spec, cancel)
    independent = None
    if plan.independent is not None and tests.status is ExecStatus.SUCCEEDED:  # the evaluator's tests judge only code whose own tests passed
        independent = _in_sandbox(plan.sandbox, plan.independent.spec, cancel)
    return _Verified(tests, independent)


def _in_sandbox(sandbox: SandboxRunner, spec: ExecSpec, cancel: threading.Event | None) -> ExecResult:
    try:
        return sandbox.run(spec, cancel=cancel)
    except SandboxUnavailable as exc:
        return ExecResult.unavailable(spec, str(exc))
    except SandboxError as exc:
        raise ToolError(ErrorClass.TOOL_ERROR, str(exc)) from exc


def _finish_submit(runtime: CodingRuntime, ctx: ToolContext, plan: _VerifyPlan, verified: _Verified) -> dict[str, object]:
    run = ctx.run
    result = verified.tests
    if plan.independent is not None:
        acceptance.discard(_acceptance_fs(ctx.settings), run.id)
    verification = _judge_components(ctx, plan, verified) if plan.components else replace(judge(result, plan.criteria), fingerprint=plan.digest)
    run.verification_json = json.dumps(verification.to_dict())
    for row in ctx.session.scalars(select(AcceptanceCriterion).where(AcceptanceCriterion.run_id == run.id)):
        judged = next((c for c in verification.criteria if c.code == row.code), None)
        if judged is not None:
            row.status, row.test_refs_json = judged.status.value, json.dumps(list(judged.tests))
    _record(ctx, "verification.finished", _verification_event(verification))
    data = _verification_data(verification)
    if run.is_terminal:  # cancelled while the tests ran
        runtime.candidates.discard(run.id)
        return data
    if result.status is ExecStatus.CANCELLED:  # the backend stopped: the run stays in VERIFYING and can test again
        data["next_state"] = RunState.VERIFYING.value
        return data
    if verification.verdict is Verdict.PASSED and plan.independent is not None:
        verification = _judge_independent(ctx, plan, verified, verification, data)
    if verification.verdict is Verdict.PASSED:
        _learn(ctx, passed=True)
        _change_state(ctx, RunState.DOCUMENTING, "verification passed")
        data["next_state"] = RunState.DOCUMENTING.value
        return data
    kind = failure_kind(verification)
    if kind in REPAIRABLE:  # the tests judged the code. Otherwise the sandbox gave no answer, and there is nothing to learn
        _learn(ctx, passed=False, failure=kind)
    decision = _after_verification(runtime, ctx, verification, kind)
    data["recovery"] = _decided(ctx, decision)
    if decision.action in (Action.REPAIR, Action.SWITCH_TOOL):
        run.repairs += 1
        _change_state(ctx, RunState.REPAIRING, f"verification failed: {verification.reason}")
        data["next_state"] = RunState.REPAIRING.value
    elif decision.action is Action.RETRY:  # the sandbox gave no answer: the run stays in VERIFYING and can test again
        data["next_state"] = RunState.VERIFYING.value
    else:
        _end_failed(runtime, ctx, outcome="verification_failed", detail=f"{verification.reason}; {decision.reason}", keep_copy=decision.category == FailureCategory.AMBIGUOUS_STATE)
    return data


def _judge_independent(ctx: ToolContext, plan: _VerifyPlan, verified: _Verified, verification: VerificationResult, data: dict[str, object]) -> VerificationResult:
    """The provider's tests passed; now the evaluator's. If they fail, recovery and GPT see the unmet criteria only."""
    independent = plan.independent
    assert independent is not None
    outcome = verified.independent or ExecResult.unavailable(independent.spec, "the acceptance suite did not run")
    judged = acceptance.judge_acceptance(outcome, independent.suite, independent.sha256)
    ctx.run.acceptance_json = json.dumps(judged.to_dict())
    _record(ctx, "acceptance.finished", {k: v for k, v in judged.to_dict().items() if k != "output_tail"})
    data["acceptance"] = judged.for_gpt()
    if judged.verdict == acceptance.PASSED:
        return verification
    failed = acceptance.for_recovery(judged, plan.digest)
    ctx.run.verification_json = json.dumps(failed.to_dict())
    data.update(verdict=failed.verdict.value, reason=failed.reason, output_tail="")
    return failed


def _learn(ctx: ToolContext, *, passed: bool, failure: str | None = None) -> None:
    """Verification is what the selector learns from: a finished attempt alone proves nothing."""
    settled = settle_verification(ctx.session, ctx.run.id, passed=passed, failure=failure)
    if settled:
        _record(ctx, "selection.learned", {"attempts": settled, "verified": passed, "failure": failure})
    memory = RecoveryMemory.load(ctx.run.recovery_json)
    if memory.action:
        _record(ctx, "recovery.outcome", {"category": memory.category, "action": memory.action, "resolved": passed})
    if passed:
        ctx.run.recovery_json = None


def _verification_event(v: VerificationResult) -> dict[str, object]:
    return {"verdict": v.verdict.value, "reason": v.reason, "exec_status": v.exec_status, "exit_code": v.exit_code, "container": v.container,
            "duration_ms": v.duration_ms, "cleanup": v.cleanup, "ran": v.counts.ran, "passed": v.counts.passed, "failed": v.counts.failures,
            "errors": v.counts.errors, "failed_names": list(v.counts.failed_names), "fingerprint": v.fingerprint}  # fmt: skip


def _verification_data(v: VerificationResult) -> dict[str, object]:
    return {
        "verdict": v.verdict.value, "reason": v.reason, "exit_code": v.exit_code, "tests_ran": v.counts.ran, "tests_passed": v.counts.passed,
        "tests_failed": v.counts.failures, "tests_errored": v.counts.errors, "failed_tests": list(v.counts.failed_names),
        "criteria": [{"code": c.code, "priority": c.priority, "status": c.status.value} for c in v.criteria], "output_tail": v.output_tail,
    }  # fmt: skip


# --- finish ---------------------------------------------------------------------------------------


def _finish_project(runtime: CodingRuntime, ctx: ToolContext, args: FinishArgs) -> dict[str, object]:
    run = ctx.run
    last = json.loads(run.verification_json or "null")
    if not isinstance(last, dict) or last.get("verdict") != Verdict.PASSED.value:
        raise ToolError(ErrorClass.POLICY_REFUSED, "nothing is delivered without a passed verification")
    imported = imported_record(ctx.session, run.id)
    if imported is not None:  # reused code goes out with its licence and its notice, or not at all
        missing = [rel for rel in (imported["notices_file"], *imported["license_files"]) if not (runtime.candidates.path(run.id) / rel).is_file()]
        if missing:
            raise ToolError(ErrorClass.POLICY_REFUSED, "the imported repository's licence and notices must be delivered with it", missing)
    assert ctx.workspace is not None
    settings = ctx.settings
    try:
        promotion = runtime.candidates.promote(
            run.id, project_id=ctx.project.id, workspaces=ctx.workspace, checkpoints_dir=settings.coding_checkpoints_dir, verified_digest=last["fingerprint"],
            max_file_bytes=settings.workspace_max_file_bytes, max_files=settings.workspace_max_files, secrets=settings.secret_values(),
        )  # fmt: skip
    except (CandidateError, WorkspaceError) as exc:
        raise ToolError(ErrorClass.VALIDATION_FAILED, str(exc)) from exc
    delivered = [c.to_dict() for c in promotion.changes]
    _record(ctx, "delivery.finished", {"files": delivered, "summary": args.summary})
    complete_run(ctx.session, run, ctx.events, detail=args.summary)
    run.report_json = json.dumps(_report(runtime, ctx))
    runtime.candidates.discard(run.id)
    return {"delivered": delivered, "state": run.state.value, "report": f"/api/v1/runs/{run.id}/report"}


# --- Shared ---------------------------------------------------------------------------------------


def _unfinished_tasks(ctx: ToolContext) -> list[PlanTask]:
    return list(ctx.session.scalars(select(PlanTask).where(PlanTask.run_id == ctx.run.id, PlanTask.status != _FINISHED_TASK).order_by(PlanTask.order)))


def _change_state(ctx: ToolContext, target: RunState, reason: str) -> None:
    change_state(ctx.session, ctx.run, target, ctx.events, reason=reason)


def _decided(ctx: ToolContext, decision: Decision, **extra: object) -> dict[str, Any]:
    payload = decision.to_dict()
    _record(ctx, "recovery.decided", {**payload, **extra})
    return payload


def _end_failed(runtime: CodingRuntime, ctx: ToolContext, *, outcome: str, detail: str, keep_copy: bool) -> None:
    """The run ends. The real project was never touched, so dropping the copy is all the undoing there is."""
    if not ctx.run.is_terminal:
        end_run(ctx.session, ctx.run, RunState.FAILED, ctx.events, outcome=outcome, detail=detail, reason=outcome)
    ctx.run.report_json = json.dumps(_report(runtime, ctx))
    if not keep_copy:
        runtime.candidates.discard(ctx.run.id)


def _report(runtime: CodingRuntime, ctx: ToolContext) -> dict[str, Any]:
    return build_report(ctx.session, ctx.run, ctx.project, gpt=runtime.gpt_label, sandbox=type(runtime.sandbox).__name__)


def _record(ctx: ToolContext, event_type: str, payload: dict[str, Any]) -> None:
    ctx.events.record(ctx.session, project_id=ctx.project.id, run_id=ctx.run.id, type=event_type, payload=payload)


def _recover_note(_info: RecoveryInfo) -> str:
    return "Startup recovery stopped the attempt and undid its changes to the disposable copy; call the tool again if it is still needed."


def register_coding_tools(registry: ToolRegistry, runtime: CodingRuntime) -> None:
    registry.register(
        ToolSpec(
            name="generate_code",
            description="Implement or fix one plan task. Python chooses the coding provider, works in a disposable copy, audits the result and returns what changed.",
            args_model=GenerateCodeArgs,
            risk=RiskLevel.WORKSPACE_WRITE,
            states=frozenset({RunState.IMPLEMENTING, RunState.REPAIRING}),
            handler=None,
            external=ExternalStep(
                begin=partial(_begin_generate_code, runtime), run=partial(_run_generate_code, runtime),
                finish=partial(_finish_generate_code, runtime), recover=_recover_note,
            ),  # fmt: skip
        )
    )
    registry.register(
        ToolSpec(
            name="submit_implementation",
            description="Run the project's tests in the Docker sandbox and judge them against the acceptance criteria. Call it when every task is DONE, and after each repair.",
            args_model=SubmitImplementationArgs,
            risk=RiskLevel.SANDBOX_EXEC,
            states=frozenset({RunState.IMPLEMENTING, RunState.REPAIRING, RunState.VERIFYING}),
            handler=None,
            external=ExternalStep(begin=partial(_begin_submit, runtime), run=_run_verification, finish=partial(_finish_submit, runtime)),
        )
    )
    registry.register(
        ToolSpec(
            name="finish",
            description="Deliver the verified files into the project and write the final report.",
            args_model=FinishArgs,
            risk=RiskLevel.WORKSPACE_WRITE,
            states=frozenset({RunState.DOCUMENTING}),
            handler=partial(_finish_project, runtime),
        )
    )


# --- After a crash --------------------------------------------------------------------------------


class _ServerGone:
    """A restarted backend has no session. The stop goes straight to ending the container."""

    def submit(self, session_id: str, body: dict[str, Any]) -> int | None:
        return None

    def statuses(self) -> dict[str, Any]:
        raise ControlError("the backend restarted")

    def messages(self, session_id: str) -> list[Any]:
        raise ControlError("the backend restarted")

    def abort(self, session_id: str) -> None:
        raise ControlError("the backend restarted")


def _charge_lost_attempt(sessions: sessionmaker[Session], record: AttemptRecord, launcher: CodingLauncher, settings: Settings) -> None:
    """The backend stopped before it could count this attempt's spending. Unreadable means the worst."""
    with sessions.begin() as s:
        run = s.get(Run, record.run_id)
        if run is not None and run.cost_known:
            allowance = max(settings.run_budget_usd - run.cost_usd, 0.0)
            run.cost_usd += attempt_cost(launcher.usage_for(record.run_id, record.call_id), allowance) or 0.0


def recover_attempts(runtime: CodingRuntime, settings: Settings, sessions: sessionmaker[Session]) -> list[AttemptRecord]:
    """Finish what a stopped backend left. Runs at startup, before any run advances. An attempt whose
    container cannot be confirmed stopped is quarantined, and its copy stays for a person to review."""
    finished: list[AttemptRecord] = []
    launcher = runtime.selector.first_launcher()
    for record in runtime.store.unresolved():
        root = settings.coding_work_dir / record.run_id
        guard = AttemptGuard(
            record, store=runtime.store, writer=launcher.writer_for(record.container_id),
            fingerprint=partial(workspace_fingerprint, root), policy=runtime.stop_policy,
        )  # fmt: skip
        report = guard.recover(_ServerGone())
        _charge_lost_attempt(sessions, record, launcher, settings)
        checkpoint = Checkpoint.find(settings.coding_checkpoints_dir, record.run_id, record.call_id)
        if report.safe_to_restore and checkpoint is not None:
            try:
                guard.restore(checkpoint, root)
                checkpoint.discard()
            except _RESTORABLE_ERRORS:
                logger.exception("restoring run %s after a restart failed", record.run_id)
        finished.append(guard.record)
    with sessions() as s:
        active = set(s.scalars(select(Run.id).where(Run.state.not_in(TERMINAL_RUN_STATES))))
        quarantined = set(s.scalars(select(CodingAttempt.run_id).where(CodingAttempt.phase == AttemptPhase.QUARANTINED)))
    runtime.candidates.sweep(active | quarantined)
    if not quarantined:
        launcher.sweep()
    return finished
