"""Long-running tools: no SQLite write lock while they run, cancellation, late results, crash recovery.

A test-only tool (`slow_tool`) blocks in its slow step until the test releases it, so the
test can act while it runs, as the API would.
"""

from __future__ import annotations

import sqlite3
import threading
from collections.abc import Iterator
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient
from pydantic import Field

from app.db.models import InvocationStatus, Run, RunState
from app.demo.calculator import ANALYSIS, CRITERIA, PLAN
from app.main import create_app
from app.orchestrator import loop as loop_module
from app.orchestrator.fake_llm import FakeLLM, call, fail
from app.orchestrator.llm_client import LLMErrorKind
from app.orchestrator.run_control import cancel_run
from app.orchestrator.state_machine import ORCHESTRATED_STATES
from app.sandbox.spec import ExecResult, ExecSpec, ExecStatus
from app.tools.adapters.sandbox import register_sandbox_tools
from app.tools.base import (
    ErrorClass,
    ExternalStep,
    RecoveryInfo,
    RiskLevel,
    ToolArgs,
    ToolContext,
    ToolError,
    ToolSpec,
)
from app.tools.registry import ToolRegistrationError, ToolRegistry
from tests.harness import Harness
from tests.helpers import API_TOKEN, FAKE_OPENAI_KEY

IMPLEMENTING = ORCHESTRATED_STATES | {RunState.IMPLEMENTING}
WAIT = 10.0


class SlowArgs(ToolArgs):
    label: str = Field(min_length=1)


class SimulatedCrash(BaseException):
    """Escapes every `except Exception`, like the process dying in the middle of a tool."""


@dataclass
class SlowTool:
    """Controls for the test tool. Each step records that it ran."""

    release: threading.Event = field(default_factory=threading.Event)
    running: threading.Event = field(default_factory=threading.Event)
    calls: list[str] = field(default_factory=list)
    saw_cancel: bool = False
    fail_begin: bool = False
    fail_run: Exception | None = None
    crash: bool = False
    recovered: list[RecoveryInfo] = field(default_factory=list)
    recover_raises: bool = False
    progress_extra: dict[str, object] = field(default_factory=dict)

    def begin(self, ctx: ToolContext, args: SlowArgs) -> str:
        self.calls.append("begin")
        if self.fail_begin:
            raise ToolError(ErrorClass.VALIDATION_FAILED, "begin refused")
        ctx.events.record(ctx.session, project_id=ctx.project.id, run_id=ctx.run.id, type="slow.begin", payload={})
        return args.label

    def run(self, plan: str, cancel: threading.Event | None, progress: Any) -> str:
        self.calls.append("run")
        progress("slow.progress", {"step": 1, **self.progress_extra})
        self.running.set()
        if self.crash:
            raise SimulatedCrash()
        if self.fail_run is not None:
            raise self.fail_run
        deadline = threading.Event()
        while not self.release.is_set():
            if cancel is not None and cancel.is_set():
                self.saw_cancel = True
                return "cancelled"
            deadline.wait(0.02)
        return f"done:{plan}"

    def finish(self, ctx: ToolContext, plan: str, outcome: str) -> dict[str, object]:
        self.calls.append("finish")
        ctx.events.record(ctx.session, project_id=ctx.project.id, run_id=ctx.run.id, type="slow.finish", payload={"outcome": outcome})
        return {"outcome": outcome}

    def recover(self, info: RecoveryInfo) -> str | None:
        self.recovered.append(info)
        if self.recover_raises:
            raise RuntimeError("cannot undo")
        return "The test side effects were undone."

    def spec(self) -> ToolSpec:
        return ToolSpec(
            name="slow_tool",
            description="Test-only slow tool.",
            args_model=SlowArgs,
            risk=RiskLevel.SANDBOX_EXEC,
            states=frozenset({RunState.IMPLEMENTING}),
            handler=None,
            external=ExternalStep(begin=self.begin, run=self.run, finish=self.finish, recover=self.recover),
        )


@pytest.fixture(autouse=True)
def _fast_cancel_watch(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(loop_module._CancelWatch, "POLL_SECONDS", 0.05)


@pytest.fixture
def tool() -> SlowTool:
    return SlowTool()


@pytest.fixture
def harness(tmp_path: Path, tool: SlowTool) -> Iterator[Harness]:
    llm = FakeLLM(
        call("submit_analysis", ANALYSIS),
        call("submit_plan", PLAN),
        call("submit_criteria", CRITERIA),
        call("slow_tool", {"label": "x"}),
        fail(LLMErrorKind.TRANSIENT),
    )
    h = Harness(tmp_path, llm, extra_tools=[tool.spec()], orchestrated_states=IMPLEMENTING)
    yield h
    tool.release.set()
    h.close()


def _start(h: Harness) -> tuple[str, threading.Thread]:
    run_id = h.new_run()
    thread = threading.Thread(target=h.orchestrator.advance, args=(run_id,), daemon=True)
    thread.start()
    return run_id, thread


def _db_path(h: Harness) -> str:
    path = h.settings.database_path
    assert path is not None
    return str(path)


def _write_lock_free(h: Harness) -> bool:
    """True if another connection can take SQLite's write lock within 0.5 s."""
    conn = sqlite3.connect(_db_path(h), timeout=0.5, isolation_level=None)
    try:
        conn.execute("BEGIN IMMEDIATE")
        conn.execute("ROLLBACK")
        return True
    except sqlite3.OperationalError as exc:
        assert "locked" in str(exc)
        return False
    finally:
        conn.close()


# --- No write lock during the slow step ------------------------------------------------


def test_the_database_stays_writable_while_a_long_tool_runs(harness: Harness, tool: SlowTool) -> None:
    run_id, thread = _start(harness)
    assert tool.running.wait(WAIT)
    assert _write_lock_free(harness)
    assert "slow.begin" in harness.event_types(run_id)  # the begin step was committed before the slow step
    assert "slow.progress" in harness.event_types(run_id)  # progress is visible while the tool still runs
    assert "slow.finish" not in harness.event_types(run_id)
    tool.release.set()
    thread.join(WAIT)
    assert not thread.is_alive()
    assert tool.calls == ["begin", "run", "finish"]
    (inv,) = [i for i in harness.invocations(run_id) if i.tool == "slow_tool"]
    assert inv.status is InvocationStatus.SUCCEEDED and inv.ok
    assert '"outcome": "done:x"' in (inv.output_json or "")


def test_a_handler_tool_by_contrast_holds_the_write_lock(tmp_path: Path) -> None:
    """Why the external step exists: an ordinary handler runs inside the write transaction."""
    release, running = threading.Event(), threading.Event()

    def blocking_handler(ctx: ToolContext, args: SlowArgs) -> dict[str, object]:
        running.set()
        release.wait(WAIT)
        return {}

    spec = ToolSpec(
        name="slow_tool",
        description="Test-only blocking handler.",
        args_model=SlowArgs,
        risk=RiskLevel.SANDBOX_EXEC,
        states=frozenset({RunState.IMPLEMENTING}),
        handler=blocking_handler,
    )
    llm = FakeLLM(
        call("submit_analysis", ANALYSIS), call("submit_plan", PLAN), call("submit_criteria", CRITERIA),
        call("slow_tool", {"label": "x"}), fail(LLMErrorKind.TRANSIENT),
    )  # fmt: skip
    h = Harness(tmp_path, llm, extra_tools=[spec], orchestrated_states=IMPLEMENTING)
    try:
        _, thread = _start(h)
        assert running.wait(WAIT)
        assert not _write_lock_free(h)
        release.set()
        thread.join(WAIT)
    finally:
        release.set()
        h.close()


def test_an_api_request_succeeds_while_a_long_tool_runs_and_cancels_it(harness: Harness, tool: SlowTool) -> None:
    run_id, thread = _start(harness)
    assert tool.running.wait(WAIT)
    app = create_app(harness.settings)  # same database file, separate engine: a concurrent API process
    with TestClient(app) as client:
        headers = {"Authorization": f"Bearer {API_TOKEN}"}
        assert client.get(f"/api/v1/runs/{run_id}", headers=headers).status_code == 200
        response = client.post(f"/api/v1/runs/{run_id}/cancel", headers=headers)
        assert response.status_code == 200, response.text
    thread.join(WAIT)
    assert not thread.is_alive()
    assert tool.saw_cancel
    assert harness.run(run_id).state is RunState.CANCELLED


# --- Cancellation and late results ---------------------------------------------------


def test_a_run_cancelled_during_the_slow_step_stops_it_and_discards_the_result(harness: Harness, tool: SlowTool) -> None:
    run_id, thread = _start(harness)
    assert tool.running.wait(WAIT)
    requests_before = len(harness.llm.requests)
    with harness.sessions.begin() as s:
        run = s.get(Run, run_id)
        assert run is not None
        cancel_run(s, run, harness.events)
    thread.join(WAIT)
    assert not thread.is_alive()
    assert tool.saw_cancel and tool.calls == ["begin", "run", "finish"]  # finish still records the outcome
    (inv,) = [i for i in harness.invocations(run_id) if i.tool == "slow_tool"]
    assert inv.status is InvocationStatus.DISCARDED and not inv.delivered
    assert harness.events_of(run_id, "tool.result_discarded")[0]["tool"] == "slow_tool"
    assert len(harness.llm.requests) == requests_before  # no GPT call after the cancel
    assert harness.run(run_id).state is RunState.CANCELLED


def test_backend_shutdown_during_the_slow_step_stops_it_and_makes_no_further_gpt_call(
    harness: Harness, tool: SlowTool
) -> None:
    run_id, thread = _start(harness)
    assert tool.running.wait(WAIT)
    requests_before = len(harness.llm.requests)
    harness.orchestrator.request_stop()
    thread.join(WAIT)
    assert not thread.is_alive()
    assert tool.saw_cancel
    assert len(harness.llm.requests) == requests_before
    run = harness.run(run_id)
    assert run.state is RunState.IMPLEMENTING and run.pause_reason == "backend_shutdown"


# --- Crash recovery and exactly-once execution ---------------------------------------


def test_a_crash_in_the_slow_step_is_recovered_once_and_never_re_run(harness: Harness, tool: SlowTool) -> None:
    tool.crash = True
    run_id = harness.new_run()
    with pytest.raises(SimulatedCrash):
        harness.orchestrator.advance(run_id)
    (inv,) = [i for i in harness.invocations(run_id) if i.tool == "slow_tool"]
    assert inv.status is InvocationStatus.STARTED  # what a dead process leaves behind

    harness.llm.extend(fail(LLMErrorKind.TRANSIENT))
    harness.advance(run_id)
    (inv,) = [i for i in harness.invocations(run_id) if i.tool == "slow_tool"]
    assert inv.status is InvocationStatus.INTERRUPTED
    assert tool.calls == ["begin", "run"]  # not run a second time
    (info,) = tool.recovered
    assert (info.run_id, info.call_id) == (run_id, inv.call_id)
    assert "The test side effects were undone." in (inv.output_json or "")
    assert harness.events_of(run_id, "tool.recovered")[0]["note"] == "The test side effects were undone."

    harness.llm.extend(fail(LLMErrorKind.TRANSIENT))
    harness.advance(run_id)
    assert len(tool.recovered) == 1  # recovery happens once


def test_a_failing_recovery_hook_is_reported_not_hidden(harness: Harness, tool: SlowTool) -> None:
    tool.crash = True
    tool.recover_raises = True
    run_id = harness.new_run()
    with pytest.raises(SimulatedCrash):
        harness.orchestrator.advance(run_id)
    harness.llm.extend(fail(LLMErrorKind.TRANSIENT))
    harness.advance(run_id)
    (inv,) = [i for i in harness.invocations(run_id) if i.tool == "slow_tool"]
    assert "could not be undone automatically" in (inv.output_json or "")


# --- Step failures --------------------------------------------------------------------


def test_a_refusal_in_begin_never_reaches_the_slow_step(harness: Harness, tool: SlowTool) -> None:
    tool.fail_begin = True
    run_id = harness.new_run()
    harness.advance(run_id)
    (inv,) = [i for i in harness.invocations(run_id) if i.tool == "slow_tool"]
    assert inv.status is InvocationStatus.FAILED and inv.error_class == ErrorClass.VALIDATION_FAILED
    assert tool.calls == ["begin"]
    assert "slow.begin" not in harness.event_types(run_id)  # begin's own writes were rolled back


@pytest.mark.parametrize(
    ("error", "expected"),
    [(ToolError(ErrorClass.POLICY_REFUSED, "refused while running"), ErrorClass.POLICY_REFUSED), (RuntimeError("bug"), ErrorClass.TOOL_ERROR)],
)
def test_a_failure_in_the_slow_step_becomes_a_tool_failure(harness: Harness, tool: SlowTool, error: Exception, expected: ErrorClass) -> None:
    tool.fail_run = error
    run_id = harness.new_run()
    harness.advance(run_id)
    (inv,) = [i for i in harness.invocations(run_id) if i.tool == "slow_tool"]
    assert inv.status is InvocationStatus.FAILED and inv.error_class == expected
    assert "finish" not in tool.calls
    assert "bug" not in (inv.output_json or "")  # internal error text is not shown to GPT


def test_executor_invoke_runs_all_three_steps_in_one_call(harness: Harness, tool: SlowTool) -> None:
    tool.release.set()
    run_id = harness.new_run()
    with harness.sessions.begin() as s:
        run = s.get(Run, run_id)
        assert run is not None
        run.state = RunState.IMPLEMENTING
    with harness.sessions.begin() as s:
        run = s.get(Run, run_id)
        assert run is not None
        ctx = ToolContext(session=s, run=run, project=run.project, settings=harness.settings, events=harness.events, catalog=harness.catalog)
        prepared = harness.executor.prepare(run.state, "slow_tool", '{"label": "y"}')
        result = harness.executor.invoke(ctx, prepared)  # type: ignore[arg-type]
    assert result.ok and result.data == {"outcome": "done:y"}
    assert tool.calls == ["begin", "run", "finish"]


def test_progress_from_the_slow_step_is_redacted_like_every_event(harness: Harness, tool: SlowTool) -> None:
    tool.release.set()
    tool.progress_extra = {"note": f"token {FAKE_OPENAI_KEY}", "api_key": "anything"}
    run_id = harness.new_run()
    harness.advance(run_id)
    (payload,) = harness.events_of(run_id, "slow.progress")
    assert FAKE_OPENAI_KEY not in str(payload)
    assert payload["api_key"] != "anything"


# --- The real sandbox tool on the transaction-free path -------------------------------


class BlockingSandbox:
    """A SandboxRunner that 'runs' until released or cancelled. Starts no container."""

    def __init__(self) -> None:
        self.running = threading.Event()
        self.release = threading.Event()
        self.saw_cancel = False

    def run(self, spec: ExecSpec, *, cancel: threading.Event | None = None) -> ExecResult:
        self.running.set()
        while not self.release.wait(0.02):
            if cancel is not None and cancel.is_set():
                self.saw_cancel = True
                return ExecResult(status=ExecStatus.CANCELLED, command=spec.command, container=spec.container_name, cleanup="removed")
        return ExecResult(status=ExecStatus.SUCCEEDED, command=spec.command, container=spec.container_name, exit_code=0, stdout="14\n", cleanup="removed")

    def sweep_orphans(self) -> int:
        return 0


def test_a_sandbox_run_leaves_the_database_writable_and_stops_when_the_run_is_cancelled(tmp_path: Path) -> None:
    sandbox = BlockingSandbox()
    llm = FakeLLM(
        call("submit_analysis", ANALYSIS), call("submit_plan", PLAN), call("submit_criteria", CRITERIA),
        call("run_sandbox_command", {"command": "python-script", "target": "calc.py"}), fail(LLMErrorKind.TRANSIENT),
    )  # fmt: skip
    h = Harness(tmp_path, llm, sandbox=sandbox, orchestrated_states=IMPLEMENTING)
    register_sandbox_tools(h.registry)
    try:
        run_id = h.new_run()
        project_id = h.project(run_id).id
        h.workspace.create_project(project_id)
        h.workspace.write_text(project_id, "calc.py", "print(2 + 3 * 4)\n")
        thread = threading.Thread(target=h.orchestrator.advance, args=(run_id,), daemon=True)
        thread.start()
        assert sandbox.running.wait(WAIT)
        assert _write_lock_free(h)
        assert h.events_of(run_id, "sandbox.started")[0]["command"] == "python-script"
        with h.sessions.begin() as s:
            run = s.get(Run, run_id)
            assert run is not None
            cancel_run(s, run, h.events)
        thread.join(WAIT)
        assert not thread.is_alive() and sandbox.saw_cancel
        assert h.events_of(run_id, "sandbox.finished")[0]["status"] == "CANCELLED"
        (inv,) = [i for i in h.invocations(run_id) if i.tool == "run_sandbox_command"]
        assert inv.status is InvocationStatus.DISCARDED
    finally:
        sandbox.release.set()
        h.close()


# --- Registration -----------------------------------------------------------------------


def test_a_tool_needs_a_handler_or_an_external_step_but_not_both(tool: SlowTool) -> None:
    registry = ToolRegistry()
    registry.register(tool.spec())
    both = ToolSpec(
        name="both", description="", args_model=SlowArgs, risk=RiskLevel.READ, states=None,
        handler=lambda ctx, args: {}, external=tool.spec().external,
    )  # fmt: skip
    with pytest.raises(ToolRegistrationError, match="both a handler and an external step"):
        registry.register(both)
    neither = ToolSpec(name="neither", description="", args_model=SlowArgs, risk=RiskLevel.READ, states=None, handler=None)
    with pytest.raises(ToolRegistrationError, match="has no handler"):
        registry.register(neither)
