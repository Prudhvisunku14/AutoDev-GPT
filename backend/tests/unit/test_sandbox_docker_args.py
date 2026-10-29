"""The `docker create` command line: every isolation control is checked here, with no Docker present."""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path

import pytest

from app.sandbox.docker_cli import DockerCli, SandboxError, build_create_args
from app.sandbox.policy import build_spec
from app.sandbox.runner import DockerSandbox
from app.sandbox.spec import ExecSpec
from app.tools.adapters.workspace_fs import WorkspaceFS
from tests.docker_fake import FakeDockerHost
from tests.helpers import make_settings

PROJECT_ID = "1" * 32


@pytest.fixture
def settings(tmp_path: Path):  # noqa: ANN201
    return make_settings(tmp_path)


@pytest.fixture
def workspace(settings) -> WorkspaceFS:  # noqa: ANN001
    fs = WorkspaceFS(settings.workspaces_dir, max_file_bytes=100_000, max_files=100)
    fs.create_project(PROJECT_ID)
    fs.write_text(PROJECT_ID, "calc.py", "print(2 + 3 * 4)\n")
    return fs


@pytest.fixture
def args(settings, workspace) -> list[str]:  # noqa: ANN001
    spec = build_spec("python-script", "calc.py", workspace=workspace, project_id=PROJECT_ID, settings=settings)
    return build_create_args(spec, settings)


def _value(args: list[str], flag: str) -> str:
    return args[args.index(flag) + 1]


def test_containers_are_created_without_network_and_without_privileges(args: list[str]) -> None:
    assert _value(args, "--network") == "none"
    assert _value(args, "--cap-drop") == "ALL"
    assert _value(args, "--security-opt") == "no-new-privileges"
    assert "--read-only" in args
    assert _value(args, "--user") == "65534:65534"
    assert "--privileged" not in args


def test_images_are_never_pulled_implicitly(args: list[str]) -> None:
    assert _value(args, "--pull") == "never"


def test_resource_limits_come_from_settings(args: list[str], settings) -> None:  # noqa: ANN001
    assert _value(args, "--memory") == settings.sandbox_memory
    assert _value(args, "--memory-swap") == settings.sandbox_memory  # equal: no swap beyond the memory limit
    assert _value(args, "--cpus") == "1"
    assert _value(args, "--pids-limit") == str(settings.sandbox_pids_limit)
    assert _value(args, "--ulimit") == "nofile=256:256"


def test_temporary_space_is_a_non_executable_tmpfs(args: list[str]) -> None:
    tmpfs = _value(args, "--tmpfs")
    assert tmpfs.startswith("/tmp:") and "noexec" in tmpfs and "nosuid" in tmpfs and "nodev" in tmpfs


def test_only_one_environment_variable_is_passed(args: list[str]) -> None:
    assert [args[i + 1] for i, a in enumerate(args) if a == "--env"] == ["HOME=/tmp"]


def test_the_only_mount_is_the_workspace_and_it_is_read_only(args: list[str], workspace: WorkspaceFS) -> None:
    mounts = [args[i + 1] for i, a in enumerate(args) if a == "--mount"]
    assert len(mounts) == 1
    mount = mounts[0]
    assert f"source={workspace.project_dir(PROJECT_ID)}" in mount
    assert "target=/workspace" in mount and mount.endswith(",readonly")


@pytest.mark.parametrize(
    "dangerous",
    ["--privileged", "--cap-add", "--device", "--volume", "-v", "--publish", "-p", "--volumes-from", "--add-host", "host", "unconfined"],
)
def test_no_dangerous_option_can_appear(args: list[str], dangerous: str) -> None:
    assert not any(a == dangerous or a.startswith(f"{dangerous}=") for a in args)


def test_the_docker_socket_and_host_namespaces_are_never_mounted_or_shared(args: list[str]) -> None:
    joined = " ".join(args)
    assert "docker.sock" not in joined and "docker_engine" not in joined
    assert "--pid" not in args and "--ipc" not in args and "--uts" not in args
    assert "--network=host" not in args


def test_the_image_and_argv_come_last(args: list[str]) -> None:
    assert args[-5:] == ["python:3.12-alpine", "python", "-I", "-B", "/workspace/calc.py"]


def test_a_workspace_path_that_could_split_the_mount_is_refused(settings) -> None:  # noqa: ANN001
    spec = ExecSpec(
        command="python-version",
        image="python:3.12-alpine",
        argv=("python", "-I", "-V"),
        workspace_dir=Path("C:/work,readonly=false"),
        timeout_seconds=30,
        container_name="devpilot-sbx-0000000000000000",
    )
    with pytest.raises(SandboxError, match="cannot be mounted safely"):
        build_create_args(spec, settings)


def _sandbox(settings) -> tuple[DockerSandbox, FakeDockerHost]:  # noqa: ANN001
    host = FakeDockerHost()
    return DockerSandbox(DockerCli("docker", host), settings=settings), host


def test_an_unapproved_image_is_refused_before_docker_is_called(settings, workspace: WorkspaceFS) -> None:  # noqa: ANN001
    sandbox, host = _sandbox(settings)
    spec = build_spec("python-version", None, workspace=workspace, project_id=PROJECT_ID, settings=settings)
    with pytest.raises(SandboxError, match="approved list"):
        sandbox.run(replace(spec, image="alpine:3.20"))
    assert host.calls == []


def test_a_workspace_outside_the_approved_folder_is_refused_before_docker_is_called(
    settings, tmp_path: Path
) -> None:  # noqa: ANN001
    sandbox, host = _sandbox(settings)
    outside = tmp_path / "elsewhere"
    outside.mkdir()
    spec = ExecSpec(
        command="python-version",
        image="python:3.12-alpine",
        argv=("python", "-I", "-V"),
        workspace_dir=outside,
        timeout_seconds=30,
        container_name="devpilot-sbx-0000000000000001",
    )
    with pytest.raises(SandboxError, match="outside the approved workspaces folder"):
        sandbox.run(spec)
    assert host.calls == []


def test_the_workspace_root_itself_is_not_a_valid_mount(settings) -> None:  # noqa: ANN001
    sandbox, host = _sandbox(settings)
    settings.workspaces_dir.mkdir(parents=True, exist_ok=True)
    spec = ExecSpec(
        command="python-version",
        image="python:3.12-alpine",
        argv=("python", "-I", "-V"),
        workspace_dir=settings.workspaces_dir,
        timeout_seconds=30,
        container_name="devpilot-sbx-0000000000000002",
    )
    with pytest.raises(SandboxError, match="outside the approved workspaces folder"):
        sandbox.run(spec)
    assert host.calls == []
