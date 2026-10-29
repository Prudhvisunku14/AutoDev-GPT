"""Phase 3 demonstration: a harmless Python command inside a sandbox container.

Default: a DRY RUN. It prints the approved commands and the exact `docker create` arguments,
and starts no container. `--run` goes through the same tool the orchestrator uses
(run_sandbox_command), starts a real container, and prints the outcome and the recorded
events. Use `--run` only after Docker is verified (docs/SETUP_WINDOWS.md). It needs no OpenAI
call and no API key.

    & $env:DEVPILOT_PYTHON backend\\scripts\\demo_sandbox.py          # dry run, free
    & $env:DEVPILOT_PYTHON backend\\scripts\\demo_sandbox.py --run    # starts a container
"""

from __future__ import annotations

import argparse
import json
import secrets
import sys
import tempfile
from pathlib import Path

from app.config import REPO_ROOT, Settings
from app.core.events import EventRecorder, event_payload, list_events
from app.core.redaction import Redactor
from app.db import repositories
from app.db.models import Project, Run, RunState
from app.db.session import create_db_engine, create_session_factory, init_db
from app.sandbox.docker_cli import SandboxUnavailable, build_create_args
from app.sandbox.policy import APPROVED_COMMANDS, build_spec
from app.sandbox.runner import DockerSandbox
from app.stacks.catalog import StackCatalog
from app.tools.adapters.sandbox import register_sandbox_tools
from app.tools.adapters.workspace_fs import WorkspaceFS
from app.tools.base import ToolContext, ToolResult
from app.tools.executor import PreparedCall, ToolExecutor
from app.tools.policy import PolicyEngine
from app.tools.registry import ToolRegistry

PROJECT_ID = "5" * 32
SCRIPT = "print(2 + 3 * 4)\nprint('sandbox arithmetic ok')\n"


def _settings(root: Path) -> Settings:
    """Limits and DOCKER_CLI come from .env when present; the token and paths are local to this run."""
    return Settings(
        _env_file=REPO_ROOT / ".env" if (REPO_ROOT / ".env").is_file() else None,  # type: ignore[call-arg]
        devpilot_api_token=secrets.token_urlsafe(32),  # throwaway, local only
        database_url=f"sqlite:///{(root / 'demo.db').as_posix()}",
        workspaces_dir=root / "workspaces",
        log_level="WARNING",
    )


def _dry_run(settings: Settings, workspace: WorkspaceFS) -> int:
    print("Approved commands:")
    for name in sorted(APPROVED_COMMANDS):
        print(f"  {name}: {APPROVED_COMMANDS[name].description}")
    for name, target in (("python-version", None), ("python-script", "calc.py")):
        spec = build_spec(name, target, workspace=workspace, project_id=PROJECT_ID, settings=settings)
        print(f"\n== {name}{' ' + target if target else ''}  (timeout {spec.timeout_seconds:g} s)")
        print("docker")
        for arg in build_create_args(spec, settings):
            print(f"  {arg}")
    print("\nDry run: no container was created and Docker was not called. Use --run to start them.")
    return 0


def _run_through_tool(settings: Settings, workspace: WorkspaceFS) -> int:
    root_engine = create_db_engine(settings)
    init_db(root_engine)
    sessions = create_session_factory(root_engine)
    redactor = Redactor(settings.secret_values())
    events = EventRecorder(redactor)
    registry = ToolRegistry()
    register_sandbox_tools(registry)
    executor = ToolExecutor(registry, PolicyEngine(), redactor, max_output_chars=settings.tool_output_max_chars)
    sandbox = DockerSandbox.from_settings(settings)

    with sessions.begin() as s:
        project = repositories.create_project(
            s,
            project_id=PROJECT_ID,
            name="Sandbox demo",
            requirement_text="Demonstrate the sandbox",
            workspace_path="unused",
        )
        run_id = repositories.create_run(s, project).id
        s.get(Run, run_id).state = RunState.IMPLEMENTING  # type: ignore[union-attr]

    results: list[ToolResult] = []
    for command, target in (("python-version", None), ("python-script", "calc.py")):
        with sessions.begin() as s:
            run = s.get(Run, run_id)
            project_row = s.get(Project, PROJECT_ID)
            assert run is not None and project_row is not None
            prepared = executor.prepare(run.state, "run_sandbox_command", json.dumps({"command": command, "target": target}))
            if isinstance(prepared, ToolResult):
                results.append(prepared)
                continue
            assert isinstance(prepared, PreparedCall)
            ctx = ToolContext(
                session=s,
                run=run,
                project=project_row,
                settings=settings,
                events=events,
                catalog=StackCatalog.load(),
                sandbox=sandbox,
                workspace=workspace,
            )
            results.append(executor.invoke(ctx, prepared))

    for command, result in zip(("python-version", "python-script"), results, strict=True):
        print(f"\n== {command}")
        if not result.ok or result.data is None:
            print(f"  refused: {result.error.summary if result.error else 'unknown'}")
            continue
        data = result.data
        print(f"  status: {data['status']}   exit code: {data['exit_code']}   duration: {data['duration_ms']} ms")
        print(f"  container: {data['container']}   cleanup: {data['cleanup']}")
        print(f"  stdout: {data['stdout'].rstrip()!r}")
        if data["stderr"]:
            print(f"  stderr: {data['stderr'].rstrip()!r}")
        if data["detail"]:
            print(f"  detail: {data['detail']}")

    with sessions() as s:
        print("\nRecorded events (SQLite events table):")
        for event in list_events(s, run_id=run_id, limit=50):
            if event.type.startswith("sandbox."):
                payload = event_payload(event)
                print(f"  #{event.seq} {event.type}: {json.dumps(payload, sort_keys=True)}")
    root_engine.dispose()
    return 0 if all(r.ok and r.data and r.data["status"] == "SUCCEEDED" for r in results) else 2


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--run", action="store_true", help="start real containers (needs a verified Docker engine)")
    args = parser.parse_args()

    root = Path(tempfile.mkdtemp(prefix="devpilot-sandbox-demo-"))
    settings = _settings(root)
    workspace = WorkspaceFS(settings.workspaces_dir, max_file_bytes=100_000, max_files=100)
    workspace.create_project(PROJECT_ID)
    workspace.write_text(PROJECT_ID, "calc.py", SCRIPT)
    if not args.run:
        return _dry_run(settings, workspace)
    try:
        return _run_through_tool(settings, workspace)
    except SandboxUnavailable as exc:
        print(f"Docker is not usable, nothing was run: {exc}")
        return 1


if __name__ == "__main__":
    sys.exit(main())
