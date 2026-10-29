"""run_sandbox_command through the real registry, policy and executor, with FakeSandbox underneath."""

from __future__ import annotations

import json
from collections.abc import Iterator
from typing import Any

import pytest

from app.db.models import Project, Run, RunState
from app.sandbox.fake import FakeSandbox
from app.sandbox.spec import ExecResult, ExecSpec, ExecStatus
from app.tools.adapters.sandbox import register_sandbox_tools
from app.tools.adapters.workspace_fs import WorkspaceFS
from app.tools.base import ErrorClass, ToolContext, ToolResult
from app.tools.executor import PreparedCall
from tests.harness import Harness
from tests.helpers import FAKE_OPENAI_KEY

TOOL = "run_sandbox_command"


@pytest.fixture
def env(tmp_path) -> Iterator[tuple[Harness, str, WorkspaceFS]]:  # noqa: ANN001
    h = Harness(tmp_path)
    register_sandbox_tools(h.registry)
    run_id = h.new_run()
    project_id = h.project(run_id).id
    workspace = WorkspaceFS(h.settings.workspaces_dir, max_file_bytes=100_000, max_files=100)
    workspace.create_project(project_id)
    workspace.write_text(project_id, "calc.py", "print(2 + 3 * 4)\n")
    _set_state(h, run_id, RunState.IMPLEMENTING)
    yield h, run_id, workspace
    h.close()


def _set_state(h: Harness, run_id: str, state: RunState) -> None:
    with h.sessions.begin() as s:
        run = s.get(Run, run_id)
        assert run is not None
        run.state = state


def _call(
    h: Harness,
    run_id: str,
    workspace: WorkspaceFS,
    args: dict[str, Any],
    *,
    sandbox: object | None,
) -> ToolResult:
    with h.sessions.begin() as s:
        run = s.get(Run, run_id)
        assert run is not None
        project = s.get(Project, run.project_id)
        assert project is not None
        prepared = h.executor.prepare(run.state, TOOL, json.dumps(args))
        if isinstance(prepared, ToolResult):
            return prepared
        assert isinstance(prepared, PreparedCall)
        ctx = ToolContext(
            session=s,
            run=run,
            project=project,
            settings=h.settings,
            events=h.events,
            catalog=h.catalog,
            sandbox=sandbox,  # type: ignore[arg-type]
            workspace=workspace,
        )
        return h.executor.invoke(ctx, prepared)


def _script(target: str = "calc.py") -> dict[str, Any]:
    return {"command": "python-script", "target": target}


def test_the_tool_is_offered_only_while_implementing(env) -> None:  # noqa: ANN001
    h, _, _ = env
    assert TOOL in [s.name for s in h.registry.specs_for(RunState.IMPLEMENTING)]
    assert TOOL not in [s.name for s in h.registry.specs_for(RunState.ANALYZING)]
    assert TOOL not in [s.name for s in h.registry.specs_for(RunState.PLANNING)]


def test_policy_refuses_the_tool_in_an_earlier_state(env) -> None:  # noqa: ANN001
    h, run_id, workspace = env
    _set_state(h, run_id, RunState.ANALYZING)
    result = _call(h, run_id, workspace, _script(), sandbox=FakeSandbox())
    assert not result.ok and result.error is not None
    assert result.error.error_class == ErrorClass.POLICY_REFUSED


def test_an_approved_script_runs_and_its_outcome_is_returned(env) -> None:  # noqa: ANN001
    h, run_id, workspace = env
    fake = FakeSandbox()
    result = _call(h, run_id, workspace, _script(), sandbox=fake)
    assert result.ok and result.data is not None
    assert result.data["status"] == "SUCCEEDED" and result.data["stdout"] == "ok\n"
    (spec,) = fake.specs
    assert isinstance(spec, ExecSpec) and spec.argv[-1] == "/workspace/calc.py"
    started = h.events_of(run_id, "sandbox.started")
    finished = h.events_of(run_id, "sandbox.finished")
    assert started[0]["command"] == "python-script" and "host" not in json.dumps(started[0]).lower()
    assert finished[0]["status"] == "SUCCEEDED" and finished[0]["exit_code"] == 0


def test_an_unknown_command_never_reaches_the_sandbox(env) -> None:  # noqa: ANN001
    h, run_id, workspace = env
    fake = FakeSandbox()
    result = _call(h, run_id, workspace, {"command": "rm -rf /", "target": None}, sandbox=fake)
    assert not result.ok and result.error is not None
    assert result.error.error_class == ErrorClass.INVALID_ARGS
    assert fake.specs == []


@pytest.mark.parametrize(
    ("args", "message"),
    [
        ({"command": "python-script", "target": None}, "needs a target file"),
        ({"command": "python-script", "target": "../../escape.py"}, "not allowed"),
        ({"command": "python-script", "target": "missing.py"}, "does not exist"),
        ({"command": "python-version", "target": "calc.py"}, "takes no target file"),
    ],
)
def test_requests_that_break_the_rules_are_refused_before_anything_runs(env, args, message) -> None:  # noqa: ANN001
    h, run_id, workspace = env
    fake = FakeSandbox()
    result = _call(h, run_id, workspace, args, sandbox=fake)
    assert not result.ok and result.error is not None
    assert result.error.error_class == ErrorClass.INVALID_ARGS
    assert message in result.error.summary
    assert fake.specs == []


def test_docker_being_unavailable_is_a_status_and_is_recorded(env) -> None:  # noqa: ANN001
    h, run_id, workspace = env
    result = _call(h, run_id, workspace, _script(), sandbox=FakeSandbox(unavailable=True))
    assert result.ok and result.data is not None
    assert result.data["status"] == "UNAVAILABLE"
    assert "not reachable" in result.data["detail"]
    assert h.events_of(run_id, "sandbox.finished")[0]["status"] == "UNAVAILABLE"


def test_a_timeout_is_returned_as_data_not_as_an_error(env) -> None:  # noqa: ANN001
    h, run_id, workspace = env

    def timed_out(spec: ExecSpec) -> ExecResult:
        return ExecResult(status=ExecStatus.TIMEOUT, command=spec.command, container=spec.container_name, cleanup="removed")

    result = _call(h, run_id, workspace, _script(), sandbox=FakeSandbox(timed_out))
    assert result.ok and result.data is not None
    assert result.data["status"] == "TIMEOUT" and result.data["cleanup"] == "removed"


def test_a_missing_sandbox_is_a_tool_error_and_nothing_runs(env) -> None:  # noqa: ANN001
    h, run_id, workspace = env
    result = _call(h, run_id, workspace, _script(), sandbox=None)
    assert not result.ok and result.error is not None
    assert result.error.error_class == ErrorClass.TOOL_ERROR
    assert "not configured" in result.error.summary


def test_a_secret_printed_by_the_code_never_reaches_gpt(env) -> None:  # noqa: ANN001
    h, run_id, workspace = env

    def leaky(spec: ExecSpec) -> ExecResult:
        return ExecResult(
            status=ExecStatus.SUCCEEDED,
            command=spec.command,
            container=spec.container_name,
            exit_code=0,
            stdout=f"key={FAKE_OPENAI_KEY}\n",
            cleanup="removed",
        )

    result = _call(h, run_id, workspace, _script(), sandbox=FakeSandbox(leaky))
    assert FAKE_OPENAI_KEY not in h.executor.render(result)
