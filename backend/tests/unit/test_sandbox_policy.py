"""The approval table: approved images and commands, and the request checks that run before any container."""

from __future__ import annotations

from pathlib import Path

import pytest

from app.sandbox.policy import (
    APPROVED_COMMANDS,
    ApprovedCommand,
    ApprovedImage,
    SandboxPolicyError,
    build_spec,
)
from app.tools.adapters.workspace_fs import WorkspaceFS
from tests.helpers import make_settings

PROJECT_ID = "0" * 32


@pytest.fixture
def workspace(tmp_path: Path) -> WorkspaceFS:
    fs = WorkspaceFS(tmp_path / "workspaces", max_file_bytes=100_000, max_files=100)
    fs.create_project(PROJECT_ID)
    fs.write_text(PROJECT_ID, "calc.py", "print(2 + 3 * 4)\n")
    fs.write_text(PROJECT_ID, "sub/tool.py", "print('ok')\n")
    fs.write_text(PROJECT_ID, "notes.txt", "not python\n")
    return fs


def _spec(tmp_path: Path, workspace: WorkspaceFS, name: str, target: str | None = None, **overrides: object):  # noqa: ANN202
    settings = make_settings(tmp_path, **overrides)
    return build_spec(name, target, workspace=workspace, project_id=PROJECT_ID, settings=settings)


@pytest.mark.parametrize("reference", ["python:3.12-alpine", f"python:3.12-alpine@sha256:{'a' * 64}"])
def test_images_must_name_a_version(reference: str) -> None:
    assert ApprovedImage(name="ok", reference=reference).reference == reference


@pytest.mark.parametrize("reference", ["python:latest", "python", "localhost:5000/python:3.12", "Python:3.12"])
def test_unpinned_or_unusual_image_references_are_refused(reference: str) -> None:
    with pytest.raises(ValueError):
        ApprovedImage(name="bad", reference=reference)


def test_version_command_runs_the_fixed_argument_list(tmp_path: Path, workspace: WorkspaceFS) -> None:
    spec = _spec(tmp_path, workspace, "python-version")
    assert spec.argv == ("python", "-I", "-V")
    assert spec.image == "python:3.12-alpine"
    assert spec.container_name.startswith("devpilot-sbx-") and len(spec.container_name) == len("devpilot-sbx-") + 16
    assert spec.workspace_dir == workspace.project_dir(PROJECT_ID)


def test_script_command_runs_a_workspace_file_inside_the_container(tmp_path: Path, workspace: WorkspaceFS) -> None:
    spec = _spec(tmp_path, workspace, "python-script", "calc.py")
    assert spec.argv == ("python", "-I", "-B", "/workspace/calc.py")


def test_backslashes_in_a_target_are_normalized(tmp_path: Path, workspace: WorkspaceFS) -> None:
    spec = _spec(tmp_path, workspace, "python-script", "sub\\tool.py")
    assert spec.argv[-1] == "/workspace/sub/tool.py"


def test_unknown_command_is_refused(tmp_path: Path, workspace: WorkspaceFS) -> None:
    with pytest.raises(SandboxPolicyError, match="not an approved command"):
        _spec(tmp_path, workspace, "rm")


@pytest.mark.parametrize(
    ("target", "message"),
    [
        (None, "needs a target file ending in .py"),
        ("missing.py", "does not exist"),
        ("notes.txt", "must end in .py"),
        ("../calc.py", "not allowed"),
        ("/etc/passwd.py", "absolute"),
        ("C:/Windows/x.py", "drive letters"),
        ("sub/../../calc.py", "not allowed"),
        ("calc.py:stream.py", "alternate data streams"),
        ("x" * 600 + ".py", "too long"),
    ],
)
def test_script_targets_are_checked_before_anything_runs(
    tmp_path: Path, workspace: WorkspaceFS, target: str | None, message: str
) -> None:
    with pytest.raises(SandboxPolicyError, match=message):
        _spec(tmp_path, workspace, "python-script", target)


def test_a_command_without_a_target_file_refuses_one(tmp_path: Path, workspace: WorkspaceFS) -> None:
    with pytest.raises(SandboxPolicyError, match="takes no target file"):
        _spec(tmp_path, workspace, "python-version", "calc.py")


def test_a_missing_project_is_a_policy_refusal(tmp_path: Path, workspace: WorkspaceFS) -> None:
    settings = make_settings(tmp_path)
    with pytest.raises(SandboxPolicyError, match="workspace"):
        build_spec("python-version", None, workspace=workspace, project_id="f" * 32, settings=settings)


def test_a_command_can_only_lower_the_timeout(tmp_path: Path, workspace: WorkspaceFS) -> None:
    short = ApprovedCommand(name="short", description="", image="python-runner", argv=("python",), timeout_seconds=5)
    long = ApprovedCommand(name="long", description="", image="python-runner", argv=("python",), timeout_seconds=9_999)
    commands = {"short": short, "long": long}
    settings = make_settings(tmp_path, sandbox_timeout_seconds=60)
    kwargs = {"workspace": workspace, "project_id": PROJECT_ID, "settings": settings, "commands": commands}
    assert build_spec("short", None, **kwargs).timeout_seconds == 5
    assert build_spec("long", None, **kwargs).timeout_seconds == 60


def test_the_approved_table_is_small_and_has_no_shell_or_network_entries() -> None:
    for command in APPROVED_COMMANDS.values():
        assert command.argv[0] == "python"
        assert "sh" not in command.argv and "bash" not in command.argv
        assert command.image in {"python-runner"}


def test_the_test_command_takes_no_argument_from_gpt_and_writes_no_bytecode(tmp_path: Path, workspace: WorkspaceFS) -> None:
    command = APPROVED_COMMANDS["python-unittest"]
    assert command.target_suffix is None and "-I" in command.argv and "-B" in command.argv and command.argv[3:6] == ("-m", "unittest", "discover")
    with pytest.raises(SandboxPolicyError, match="takes no target"):
        _spec(tmp_path, workspace, "python-unittest", "anything.py")
