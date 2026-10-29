"""Real-container tests for the Docker sandbox. NOT RUN YET: they need a verified Docker engine.

Opt-in: RUN_DOCKER_TESTS=true. Needs the image python:3.12-alpine already on the disk
(`docker pull python:3.12-alpine` is run by the user, after the D: data location is verified).
Probe commands below are test-only and are never added to the approved table.
"""

from __future__ import annotations

import threading
from pathlib import Path

import pytest

from app.sandbox.docker_cli import DockerCli, SandboxUnavailable
from app.sandbox.policy import APPROVED_COMMANDS, ApprovedCommand, build_spec
from app.sandbox.process import SubprocessRunner
from app.sandbox.runner import DockerSandbox
from app.sandbox.spec import ExecStatus
from app.tools.adapters.workspace_fs import WorkspaceFS
from tests.helpers import make_settings

pytestmark = pytest.mark.docker

PROJECT_ID = "3" * 32

PROBES: dict[str, ApprovedCommand] = {
    name: ApprovedCommand(name=name, description="test probe", image="python-runner", argv=tuple(argv))
    for name, argv in {
        "probe-identity": (
            "python", "-I", "-c",
            "import os\nprint(os.getuid(), os.getgid())\nprint([l for l in open('/proc/self/status') if l.startswith('CapEff')][0].strip())",
        ),
        "probe-network": (
            "python", "-I", "-c",
            "import socket\ntry:\n    socket.create_connection(('1.1.1.1', 53), timeout=3)\n    print('connected')\nexcept OSError:\n    print('blocked')",
        ),
        "probe-readonly": ("python", "-I", "-c", "open('/workspace/written.txt', 'w').write('x')"),
        "probe-memory": ("python", "-I", "-c", "x = b'a' * (512 * 1024 * 1024)\nprint('allocated')"),
        "probe-sleep": ("python", "-I", "-c", "import time\ntime.sleep(60)"),
        "probe-output": ("python", "-I", "-c", "print('z' * 8000)"),
    }.items()
}


def make_sandbox(tmp_path: Path, **overrides: object) -> DockerSandbox:
    settings = make_settings(
        tmp_path,
        **{"sandbox_memory": "128m", "sandbox_timeout_seconds": 30, "sandbox_max_output_bytes": 1_024, **overrides},
    )
    cli = DockerCli(settings.docker_cli, SubprocessRunner())
    try:
        cli.server_version()
    except SandboxUnavailable as exc:
        pytest.fail(f"RUN_DOCKER_TESTS=true but Docker is not usable: {exc}")
    return DockerSandbox(cli, settings=settings)


@pytest.fixture
def sandbox(tmp_path: Path) -> DockerSandbox:
    return make_sandbox(tmp_path)


@pytest.fixture
def workspace(sandbox: DockerSandbox, tmp_path: Path) -> WorkspaceFS:
    fs = WorkspaceFS(tmp_path / "workspaces", max_file_bytes=100_000, max_files=100)
    fs.create_project(PROJECT_ID)
    fs.write_text(PROJECT_ID, "calc.py", "print(2 + 3 * 4)\n")
    return fs


def _run(sandbox: DockerSandbox, workspace: WorkspaceFS, name: str, target: str | None = None, cancel=None):  # noqa: ANN001, ANN201
    commands = {**APPROVED_COMMANDS, **PROBES}
    spec = build_spec(name, target, workspace=workspace, project_id=PROJECT_ID, settings=sandbox.settings, commands=commands)
    return sandbox.run(spec, cancel=cancel)


def _leftovers(sandbox: DockerSandbox) -> list[str]:
    return sandbox.cli.labelled_containers()


def test_python_version_runs_and_the_container_is_removed(sandbox, workspace) -> None:  # noqa: ANN001
    result = _run(sandbox, workspace, "python-version")
    assert result.status is ExecStatus.SUCCEEDED
    assert result.stdout.startswith("Python 3.12")
    assert result.duration_ms > 0 and result.cleanup == "removed"
    assert _leftovers(sandbox) == []


def test_the_workspace_script_runs_inside_the_container(sandbox, workspace) -> None:  # noqa: ANN001
    result = _run(sandbox, workspace, "python-script", "calc.py")
    assert result.status is ExecStatus.SUCCEEDED and result.stdout == "14\n"


def test_the_container_has_no_network(sandbox, workspace) -> None:  # noqa: ANN001
    result = _run(sandbox, workspace, "probe-network")
    assert result.stdout.strip() == "blocked"


def test_the_workspace_mount_is_read_only(sandbox, workspace) -> None:  # noqa: ANN001
    result = _run(sandbox, workspace, "probe-readonly")
    assert result.status is ExecStatus.FAILED and result.exit_code != 0
    assert "Read-only file system" in result.stderr or "Errno 30" in result.stderr
    assert not (workspace.project_dir(PROJECT_ID) / "written.txt").exists()


def test_the_process_is_non_root_with_no_capabilities(sandbox, workspace) -> None:  # noqa: ANN001
    result = _run(sandbox, workspace, "probe-identity")
    lines = result.stdout.splitlines()
    assert lines[0] == "65534 65534"
    assert lines[1].endswith("0000000000000000")


def test_the_memory_limit_is_enforced(sandbox, workspace) -> None:  # noqa: ANN001
    result = _run(sandbox, workspace, "probe-memory")
    assert result.status is ExecStatus.MEMORY_LIMIT and result.oom_killed
    assert _leftovers(sandbox) == []


def test_the_time_limit_kills_and_removes_the_container(tmp_path: Path, workspace) -> None:  # noqa: ANN001
    short = make_sandbox(tmp_path, sandbox_timeout_seconds=3)
    result = _run(short, workspace, "probe-sleep")
    assert result.status is ExecStatus.TIMEOUT
    assert result.duration_ms < 30_000
    assert _leftovers(short) == []


def test_output_over_the_limit_is_truncated(sandbox, workspace) -> None:  # noqa: ANN001
    result = _run(sandbox, workspace, "probe-output")
    assert result.status is ExecStatus.SUCCEEDED
    assert result.stdout_truncated and len(result.stdout) == 1_024


def test_cancellation_removes_the_container(sandbox, workspace) -> None:  # noqa: ANN001
    cancel = threading.Event()
    threading.Timer(2.0, cancel.set).start()
    result = _run(sandbox, workspace, "probe-sleep", cancel=cancel)
    assert result.status is ExecStatus.CANCELLED
    assert _leftovers(sandbox) == []
