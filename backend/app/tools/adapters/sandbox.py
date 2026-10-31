"""run_sandbox_command: GPT's only door into the Docker sandbox (ARCHITECTURE §7, SECURITY T1, T9).

GPT names an approved command and, where the command needs one, a workspace file. Python
validates both (sandbox/policy.py), runs the container and returns what happened. A sandbox
outcome, including a timeout, a non-zero exit or a memory limit, is data with a status. Only
a request that breaks the rules is a tool error, and nothing runs for it.
"""

from __future__ import annotations

import threading
from dataclasses import dataclass
from typing import Literal

from pydantic import Field

from app.db.models import RunState
from app.sandbox.docker_cli import SandboxError, SandboxUnavailable
from app.sandbox.policy import APPROVED_COMMANDS, SandboxPolicyError, build_spec
from app.sandbox.runner import SandboxRunner
from app.sandbox.spec import ExecResult, ExecSpec
from app.tools.base import ErrorClass, ExternalStep, Progress, RiskLevel, ToolArgs, ToolContext, ToolError, ToolSpec
from app.tools.registry import ToolRegistry

COMMAND_NAMES = tuple(sorted(APPROVED_COMMANDS))
_COMMAND_LIST = "; ".join(f"{name}: {APPROVED_COMMANDS[name].description}" for name in COMMAND_NAMES)


class RunSandboxCommandArgs(ToolArgs):
    command: Literal[COMMAND_NAMES] = Field(  # type: ignore[valid-type]
        description="The approved command to run. Only these names are accepted."
    )
    target: str | None = Field(
        description="Workspace-relative file that the command runs, such as calc.py. Null for commands that take no file.",
    )


@dataclass(frozen=True)
class _Plan:
    sandbox: SandboxRunner
    spec: ExecSpec


def begin_sandbox_command(ctx: ToolContext, args: RunSandboxCommandArgs) -> _Plan:
    """In a transaction: check the request against the approval table and record the start."""
    if ctx.sandbox is None or ctx.workspace is None:
        raise ToolError(ErrorClass.TOOL_ERROR, "the Docker sandbox is not configured for this backend")
    # Once a run has a disposable copy, that copy is the project: the real one has none of its changes yet.
    in_copy = ctx.candidates is not None and ctx.candidates.has_project(ctx.run.id)
    workspace, owner = (ctx.candidates, ctx.run.id) if in_copy else (ctx.workspace, ctx.project.id)
    assert workspace is not None
    try:
        spec = build_spec(args.command, args.target, workspace=workspace, project_id=owner, settings=ctx.settings)
    except SandboxPolicyError as exc:
        raise ToolError(ErrorClass.INVALID_ARGS, str(exc)) from exc
    _record(ctx, "sandbox.started", _spec_payload(spec))
    return _Plan(sandbox=ctx.sandbox, spec=spec)


def run_sandbox_container(plan: _Plan, cancel: threading.Event | None, progress: Progress) -> ExecResult:
    """With no transaction open: run the container, which can take up to SANDBOX_TIMEOUT_SECONDS."""
    try:
        return plan.sandbox.run(plan.spec, cancel=cancel)
    except SandboxUnavailable as exc:
        return ExecResult.unavailable(plan.spec, str(exc))
    except SandboxError as exc:
        raise ToolError(ErrorClass.TOOL_ERROR, str(exc)) from exc


def finish_sandbox_command(ctx: ToolContext, plan: _Plan, result: ExecResult) -> dict[str, object]:
    _record(ctx, "sandbox.finished", _result_payload(result))
    return result.to_dict()


def sandbox_tool_spec() -> ToolSpec:
    return ToolSpec(
        name="run_sandbox_command",
        description=(
            "Run one approved command in an isolated container: no network, a read-only workspace, "
            f"and limits on CPU, memory, processes, time and output. Commands: {_COMMAND_LIST}."
        ),
        args_model=RunSandboxCommandArgs,
        risk=RiskLevel.SANDBOX_EXEC,
        states=frozenset({RunState.IMPLEMENTING, RunState.REPAIRING}),
        handler=None,
        external=ExternalStep(begin=begin_sandbox_command, run=run_sandbox_container, finish=finish_sandbox_command),
    )


def register_sandbox_tools(registry: ToolRegistry) -> None:
    registry.register(sandbox_tool_spec())


def _record(ctx: ToolContext, event_type: str, payload: dict[str, object]) -> None:
    ctx.events.record(ctx.session, project_id=ctx.project.id, run_id=ctx.run.id, type=event_type, payload=payload)


def _spec_payload(spec: ExecSpec) -> dict[str, object]:
    # The host path is never recorded; the container name and the command are enough to audit.
    return {"command": spec.command, "image": spec.image, "container": spec.container_name, "timeout_seconds": spec.timeout_seconds}


def _result_payload(result: ExecResult) -> dict[str, object]:
    return {
        "command": result.command,
        "container": result.container,
        "status": result.status.value,
        "exit_code": result.exit_code,
        "duration_ms": result.duration_ms,
        "oom_killed": result.oom_killed,
        "stdout_truncated": result.stdout_truncated,
        "stderr_truncated": result.stderr_truncated,
        "cleanup": result.cleanup,
        "detail": result.detail,
    }
