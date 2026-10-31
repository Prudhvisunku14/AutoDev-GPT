"""generate_code, submit_implementation and finish: from a plan to delivered, verified files.

GPT stays the only orchestrator: it chooses what to do next, and these tools do it. Python owns
everything that must not depend on a model's good behaviour:

- The brief comes from the saved plan, and Python chooses the coding provider.
- A coding attempt changes only the run's disposable copy. The real project is written once, by
  `finish`, after the tests passed, and only with the files that were tested.
- Every attempt goes through `AttemptGuard`. The writer is stopped and confirmed gone before the audit
  reads a file, and a refused attempt is restored only through the guard.
- The tests run in the Docker sandbox, and the verdict comes from what the sandbox really did.
- Recovery is bounded in code (app/recovery/engine.py). When it says STOP, the run ends.

The guard saves in its own transactions, so all of its work is in the `run` step, where no database
transaction is open.
"""

from __future__ import annotations

import json
import logging
import threading
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
from app.recovery.engine import Action, Decision, decide_coding, decide_verification
from app.sandbox.docker_cli import SandboxError, SandboxUnavailable
from app.sandbox.policy import SandboxPolicyError, build_spec
from app.sandbox.runner import SandboxRunner
from app.sandbox.spec import ExecResult, ExecSpec, ExecStatus
from app.selection.selector import NoProvider, ProviderSelector
from app.tools.adapters.workspace_fs import WorkspaceError
from app.tools.base import ErrorClass, ExternalStep, RecoveryInfo, RiskLevel, ToolArgs, ToolContext, ToolError, ToolSpec
from app.tools.gates import MAX_TASKS
from app.tools.registry import ToolRegistry
from app.verification.report import build_report
from app.verification.results import Criterion, Verdict, VerificationResult, failure_kind, judge

logger = logging.getLogger("devpilot.coding")

LongText = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=2000)]
_FINISHED_TASK = "DONE"
_FAILED_TASK = "FAILED"
_RESTORABLE_ERRORS = (RestoreForbidden, OSError, ValueError)


@dataclass(frozen=True)
class CodingRuntime:
    """What the coding tools need beyond the per-call context."""

    selector: ProviderSelector
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


def _begin_generate_code(runtime: CodingRuntime, ctx: ToolContext, args: GenerateCodeArgs) -> _CodingPlan:
    run, project = ctx.run, ctx.project
    task = ctx.session.scalars(select(PlanTask).where(PlanTask.run_id == run.id, PlanTask.order == args.task_order)).first()
    if task is None:
        raise ToolError(ErrorClass.INVALID_ARGS, f"the plan has no task {args.task_order}")
    stack = ctx.catalog.get(project.stack or "")
    if stack is None or stack.test_command is None:
        raise ToolError(ErrorClass.POLICY_REFUSED, f"stack {project.stack} cannot be verified yet, so no code is written for it; python-cli can")
    if ctx.workspace is None or ctx.call_id is None:
        raise ToolError(ErrorClass.TOOL_ERROR, "the workspace is not configured for this backend")
    try:
        selection = runtime.selector.choose(task.capability)
    except NoProvider as exc:
        raise ToolError(ErrorClass.POLICY_REFUSED, str(exc)) from exc
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
                                      "simulated": selection.simulated, "excluded": selection.excluded})  # fmt: skip
    brief = build_brief(_brief_input(ctx, task, args, stack_id=stack.id, language=stack.language, files=sorted(before.files)))
    spent = run.tokens_in + run.tokens_out
    coding_task = CodingTask(
        key=f"{run.id}:{task.order}", title=task.title, brief=brief, session_id=None, abort_first=False,
        max_tokens=min(ctx.settings.opencode_max_tokens_per_task, max(ctx.settings.run_max_tokens - spent, 1)),
        timeout_seconds=ctx.settings.opencode_timeout_seconds, workspace=root,
    )  # fmt: skip
    _record(ctx, "coding.started", {"task": task.order, "title": task.title, "provider": selection.provider, "files_before": len(before.files)})
    left = max(ctx.settings.run_budget_usd - run.cost_usd, 0.0) if run.cost_known else None  # attempts together stay inside the run's budget
    return _CodingPlan(project.id, run.id, ctx.call_id, task.order, coding_task, root, checkpoint, before, selection.launcher, selection.provider, selection.simulated, ctx.settings, left)


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
    return f"{last['reason']}\n\n{last['output_tail']}"


class _LateWriter:
    """The guard needs a writer before the container exists. Until one is bound, nothing can be confirmed."""

    def __init__(self) -> None:
        self._writer: Any = None

    def bind(self, writer: Any) -> None:
        self._writer = writer

    def terminate(self) -> Termination:
        return Termination(False, "no container was started") if self._writer is None else self._writer.terminate()


def _run_generate_code(runtime: CodingRuntime, plan: _CodingPlan, cancel: threading.Event | None, progress: Progress) -> _CodingResult:
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
            if report.clean:
                guard.keep()
                return _CodingResult(outcome, kept=True, changes=changes, phase=str(guard.phase))
            violations = report.violations
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
    if run.cost_known:
        spent = attempt_cost(result.ledger, plan.usd_budget)
        if spent is None:
            spent = outcome.cost_usd if outcome.cost_usd is not None else turn_cost_usd(Usage(input_tokens=outcome.tokens_in + unreported, output_tokens=outcome.tokens_out), ctx.settings)
        run.cost_usd += spent or 0.0
    data: dict[str, object] = {
        "task_order": task.order, "title": task.title, "status": outcome.status.value, "kept": result.kept, "provider": plan.provider,
        "simulated": plan.simulated, "changes": result.changes, "violations": result.violations, "phase": result.phase,
        "tokens": outcome.tokens_in + outcome.tokens_out, "detail": outcome.detail,
    }  # fmt: skip
    _record(ctx, "coding.finished", {**data, "restored": result.restored, "quarantined": result.quarantined, "stop": result.detail})
    if result.kept:
        task.status = _FINISHED_TASK
        data["all_tasks_done"] = not _unfinished_tasks(ctx)
        return data
    task.status = _FAILED_TASK
    used = ctx.session.scalar(
        select(func.count()).select_from(CodingAttempt).where(CodingAttempt.run_id == run.id, CodingAttempt.task_key == plan.task.key, CodingAttempt.phase != AttemptPhase.KEPT)
    ) or 0  # fmt: skip
    decision = decide_coding(outcome.status, quarantined=result.quarantined, attempts_used=used, max_attempts=ctx.settings.opencode_max_attempts_per_task)
    data["recovery"] = _decided(ctx, decision, task=task.order)
    if decision.action is Action.STOP:
        _end_failed(runtime, ctx, outcome="quarantined" if result.quarantined else "coding_failed", detail=f"{decision.reason}: {outcome.detail}", keep_copy=result.quarantined)
    return data


# --- submit_implementation ------------------------------------------------------------------------


@dataclass(frozen=True)
class _VerifyPlan:
    sandbox: SandboxRunner
    spec: ExecSpec
    criteria: tuple[Criterion, ...]
    digest: str


def _begin_submit(runtime: CodingRuntime, ctx: ToolContext, args: SubmitImplementationArgs) -> _VerifyPlan:
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
    if run.state is not RunState.VERIFYING:
        _change_state(ctx, RunState.VERIFYING, "submit_implementation accepted")
    _record(ctx, "verification.started", {"command": spec.command, "image": spec.image, "container": spec.container_name, "repairs_so_far": run.repairs, "summary": args.summary})
    return _VerifyPlan(runtime.sandbox, spec, criteria, runtime.candidates.digest(run.id))


def _run_verification(plan: _VerifyPlan, cancel: threading.Event | None, progress: Progress) -> ExecResult:
    try:
        return plan.sandbox.run(plan.spec, cancel=cancel)
    except SandboxUnavailable as exc:
        return ExecResult.unavailable(plan.spec, str(exc))
    except SandboxError as exc:
        raise ToolError(ErrorClass.TOOL_ERROR, str(exc)) from exc


def _finish_submit(runtime: CodingRuntime, ctx: ToolContext, plan: _VerifyPlan, result: ExecResult) -> dict[str, object]:
    run = ctx.run
    verification = replace(judge(result, plan.criteria), fingerprint=plan.digest)
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
    if verification.verdict is Verdict.PASSED:
        _change_state(ctx, RunState.DOCUMENTING, "verification passed")
        data["next_state"] = RunState.DOCUMENTING.value
        return data
    decision = decide_verification(failure_kind(verification), repairs_used=run.repairs, max_repairs=ctx.settings.max_repair_attempts)
    data["recovery"] = _decided(ctx, decision)
    if decision.action is Action.REPAIR:
        run.repairs += 1
        _change_state(ctx, RunState.REPAIRING, f"verification failed: {verification.reason}")
        data["next_state"] = RunState.REPAIRING.value
    else:
        _end_failed(runtime, ctx, outcome="verification_failed", detail=f"{verification.reason}; {decision.reason}", keep_copy=False)
    return data


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
