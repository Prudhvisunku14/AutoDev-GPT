"""DockerSandbox lifecycle against FakeDockerHost: outcomes, limits, cancellation and cleanup."""

from __future__ import annotations

import threading
from collections.abc import Iterator
from pathlib import Path

import pytest

from app.sandbox.docker_cli import DockerCli, SandboxError, SandboxUnavailable
from app.sandbox.policy import build_spec
from app.sandbox.runner import DockerSandbox
from app.sandbox.spec import ExecSpec, ExecStatus
from app.tools.adapters.workspace_fs import WorkspaceFS
from tests.docker_fake import FakeContainer, FakeDockerHost
from tests.helpers import make_settings

PROJECT_ID = "2" * 32


@pytest.fixture
def host() -> FakeDockerHost:
    return FakeDockerHost()


@pytest.fixture
def settings(tmp_path: Path):  # noqa: ANN201
    return make_settings(tmp_path, sandbox_max_output_bytes=1_024, sandbox_timeout_seconds=30)


@pytest.fixture
def spec(settings, tmp_path: Path) -> ExecSpec:  # noqa: ANN001
    fs = WorkspaceFS(settings.workspaces_dir, max_file_bytes=100_000, max_files=100)
    fs.create_project(PROJECT_ID)
    fs.write_text(PROJECT_ID, "calc.py", "print(2 + 3 * 4)\n")
    return build_spec("python-script", "calc.py", workspace=fs, project_id=PROJECT_ID, settings=settings)


@pytest.fixture
def sandbox(host: FakeDockerHost, settings) -> Iterator[DockerSandbox]:  # noqa: ANN001
    yield DockerSandbox(DockerCli("docker", host), settings=settings)


def test_success_runs_create_start_inspect_and_remove_in_order(host, sandbox, spec) -> None:  # noqa: ANN001
    result = sandbox.run(spec)
    assert result.status is ExecStatus.SUCCEEDED
    assert (result.exit_code, result.stdout, result.cleanup) == (0, "ok\n", "removed")
    assert result.duration_ms >= 0
    assert host.verbs() == ["version", "create", "start", "inspect", "rm"]
    assert spec.container_name not in host.containers


def test_a_non_zero_exit_is_a_failed_run_with_its_code_and_stderr(host, sandbox, spec) -> None:  # noqa: ANN001
    host.exit_code = 3
    host.stderr = b"Traceback (most recent call last):\n"
    result = sandbox.run(spec)
    assert result.status is ExecStatus.FAILED
    assert result.exit_code == 3
    assert result.stderr.startswith("Traceback")
    assert host.containers == {}


def test_a_timeout_is_reported_and_the_container_is_removed(host, sandbox, spec) -> None:  # noqa: ANN001
    host.start_mode = "timeout"
    result = sandbox.run(spec)
    assert result.status is ExecStatus.TIMEOUT
    assert "30 seconds" in (result.detail or "")
    assert result.cleanup == "removed" and host.containers == {}


def test_cancellation_before_start_creates_nothing(host, sandbox, spec) -> None:  # noqa: ANN001
    cancel = threading.Event()
    cancel.set()
    result = sandbox.run(spec, cancel=cancel)
    assert result.status is ExecStatus.CANCELLED
    assert host.verbs() == []


def test_cancellation_during_the_run_kills_and_removes(host, sandbox, spec) -> None:  # noqa: ANN001
    host.start_mode = "cancel"
    result = sandbox.run(spec, cancel=threading.Event())
    assert result.status is ExecStatus.CANCELLED
    assert result.cleanup == "removed" and host.containers == {}


def test_a_flood_of_output_is_stopped_and_removed(host, sandbox, spec) -> None:  # noqa: ANN001
    host.start_mode = "flood"
    result = sandbox.run(spec)
    assert result.status is ExecStatus.OUTPUT_LIMIT
    assert host.containers == {}


def test_output_over_the_capture_limit_is_truncated_but_the_run_completes(host, sandbox, spec) -> None:  # noqa: ANN001
    host.stdout = b"y" * 5_000
    result = sandbox.run(spec)
    assert result.status is ExecStatus.SUCCEEDED
    assert result.stdout_truncated and len(result.stdout) == 1_024


def test_an_oom_kill_is_reported_as_a_memory_limit(host, sandbox, spec) -> None:  # noqa: ANN001
    host.exit_code = 137
    host.oom_killed = True
    result = sandbox.run(spec)
    assert result.status is ExecStatus.MEMORY_LIMIT
    assert result.oom_killed and result.exit_code == 137


def test_a_container_that_never_ran_is_an_error_with_the_engine_message(host, sandbox, spec) -> None:  # noqa: ANN001
    host.start_mode = "fail"
    result = sandbox.run(spec)
    assert result.status is ExecStatus.ERROR
    assert "OCI runtime start failed" in (result.detail or "")
    assert host.containers == {}


def test_a_failed_create_is_an_error_and_nothing_is_started(host, sandbox, spec) -> None:  # noqa: ANN001
    host.create_error = "Unable to find image 'python:3.12-alpine' locally"
    result = sandbox.run(spec)
    assert result.status is ExecStatus.ERROR
    assert "Unable to find image" in (result.detail or "")
    assert "start" not in host.verbs()


def test_an_unreachable_daemon_is_unavailable_and_creates_nothing(host, sandbox, spec) -> None:  # noqa: ANN001
    host.reachable = False
    with pytest.raises(SandboxUnavailable, match="not reachable"):
        sandbox.run(spec)
    assert "create" not in host.verbs()


def test_a_missing_cli_says_which_setting_to_change(host, sandbox, spec) -> None:  # noqa: ANN001
    host.cli_missing = True
    with pytest.raises(SandboxUnavailable, match="DOCKER_CLI"):
        sandbox.run(spec)


def test_the_container_is_removed_even_when_the_start_step_raises(host, sandbox, spec) -> None:  # noqa: ANN001
    host.start_exception = SandboxUnavailable("the Docker CLI went away")
    with pytest.raises(SandboxUnavailable):
        sandbox.run(spec)
    assert "rm" in host.verbs()
    assert host.containers == {}


def test_a_failed_removal_is_reported_and_not_hidden(host, sandbox, spec) -> None:  # noqa: ANN001
    host.remove_fails = True
    result = sandbox.run(spec)
    assert result.cleanup == "failed"


def test_startup_sweep_removes_only_labelled_leftovers(host, sandbox) -> None:  # noqa: ANN001
    host.labelled = ["old-1", "old-2"]
    host.containers = {"old-1": FakeContainer(), "old-2": FakeContainer(), "other": FakeContainer()}
    assert sandbox.sweep_orphans() == 2
    assert set(host.containers) == {"other"}


def test_the_sweep_is_empty_when_nothing_is_left(host, sandbox) -> None:  # noqa: ANN001
    assert sandbox.sweep_orphans() == 0


def test_an_unexpected_os_error_from_the_cli_is_unavailable(host, sandbox, spec) -> None:  # noqa: ANN001
    host.start_exception = PermissionError("access denied")
    with pytest.raises(SandboxUnavailable, match="could not be started"):
        sandbox.run(spec)


def test_the_sandbox_is_built_from_settings_without_calling_docker(tmp_path: Path) -> None:
    settings = make_settings(tmp_path, docker_cli="C:/tools/docker.exe")
    sandbox = DockerSandbox.from_settings(settings)
    assert sandbox.settings is settings
    assert sandbox.cli is not None


# --- A run's disposable copy ----------------------------------------------------------------------


def _copy_spec(settings, tmp_path: Path) -> ExecSpec:  # noqa: ANN001
    fs = WorkspaceFS(settings.coding_work_dir, max_file_bytes=100_000, max_files=100)
    fs.create_project(PROJECT_ID)
    return build_spec("python-unittest", None, workspace=fs, project_id=PROJECT_ID, settings=settings)


def test_a_runs_disposable_copy_may_be_tested(host: FakeDockerHost, settings, tmp_path: Path) -> None:  # noqa: ANN001
    result = DockerSandbox(DockerCli("docker", host), settings=settings).run(_copy_spec(settings, tmp_path))
    assert result.status is ExecStatus.SUCCEEDED
    create = next(call for call in host.calls if call[0] == "create")
    assert any(a.endswith("target=/workspace,readonly") for a in create) and "none" in create


def test_the_copy_is_mounted_read_only_with_no_network_like_any_workspace(host: FakeDockerHost, settings, tmp_path: Path) -> None:  # noqa: ANN001
    DockerSandbox(DockerCli("docker", host), settings=settings).run(_copy_spec(settings, tmp_path))
    create = next(call for call in host.calls if call[0] == "create")
    assert create[create.index("--network") + 1] == "none" and "--read-only" in create


def test_the_work_root_itself_may_not_be_mounted(host: FakeDockerHost, settings, tmp_path: Path) -> None:  # noqa: ANN001
    spec = _copy_spec(settings, tmp_path)
    whole = ExecSpec(command=spec.command, image=spec.image, argv=spec.argv, workspace_dir=settings.coding_work_dir, timeout_seconds=1, container_name="c")
    with pytest.raises(SandboxError, match="outside the approved"):
        DockerSandbox(DockerCli("docker", host), settings=settings).run(whole)


def test_a_folder_outside_both_roots_is_refused(host: FakeDockerHost, settings, tmp_path: Path) -> None:  # noqa: ANN001
    spec = _copy_spec(settings, tmp_path)
    outside = tmp_path / "elsewhere"
    outside.mkdir()
    other = ExecSpec(command=spec.command, image=spec.image, argv=spec.argv, workspace_dir=outside, timeout_seconds=1, container_name="c")
    with pytest.raises(SandboxError, match="outside the approved"):
        DockerSandbox(DockerCli("docker", host), settings=settings).run(other)
