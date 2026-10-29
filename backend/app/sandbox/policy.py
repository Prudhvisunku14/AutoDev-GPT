"""The sandbox's approval table: which images may run and which commands they may run.

GPT never supplies an image, a flag or a shell string. It names a command from
APPROVED_COMMANDS and, where the command needs one, a workspace file. Python builds the
argument list (SECURITY T1, T9). Nothing here starts a shell.
"""

from __future__ import annotations

import re
import secrets
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import PurePosixPath

from app.config import Settings
from app.sandbox.spec import WORKSPACE_TARGET, ExecSpec
from app.tools.adapters.workspace_fs import PathRejected, WorkspaceError, WorkspaceFS

# name:tag, optionally followed by @sha256:<digest>. Registry ports are not accepted.
_REFERENCE = re.compile(r"[a-z0-9]+(?:[._/-][a-z0-9]+)*:[A-Za-z0-9][A-Za-z0-9._-]{0,127}(?:@sha256:[a-f0-9]{64})?")
MAX_TARGET_LENGTH = 512


class SandboxPolicyError(ValueError):
    """A request the approval table refuses. The message is safe to show to GPT."""


@dataclass(frozen=True)
class ApprovedImage:
    name: str  # the key that commands refer to
    reference: str  # exact reference; a version tag is required, never `latest`

    def __post_init__(self) -> None:
        if not _REFERENCE.fullmatch(self.reference):
            raise ValueError(f"image {self.name!r}: reference must look like name:tag")
        tag = self.reference.split("@", 1)[0].rsplit(":", 1)[1]
        if tag == "latest":
            raise ValueError(f"image {self.name!r}: the 'latest' tag is not allowed; pin a version")


@dataclass(frozen=True)
class ApprovedCommand:
    name: str
    description: str  # shown to GPT in the tool description
    image: str  # key into APPROVED_IMAGES
    argv: tuple[str, ...]  # fixed; the workspace file, if any, is appended after it
    target_suffix: str | None = None  # set when the command takes one workspace file, e.g. ".py"
    timeout_seconds: float | None = None  # may only lower the settings limit, never raise it


APPROVED_IMAGES: dict[str, ApprovedImage] = {
    img.name: img
    for img in (
        # Official Python image on Alpine: small, and it has a non-root `nobody` user.
        ApprovedImage(name="python-runner", reference="python:3.12-alpine"),
    )
}

APPROVED_COMMANDS: dict[str, ApprovedCommand] = {
    cmd.name: cmd
    for cmd in (
        ApprovedCommand(
            name="python-version",
            description="print the Python version of the sandbox image",
            image="python-runner",
            argv=("python", "-I", "-V"),
        ),
        ApprovedCommand(
            name="python-script",
            description="run a Python file from the workspace; the workspace is read-only and there is no network",
            image="python-runner",
            argv=("python", "-I", "-B"),
            target_suffix=".py",
        ),
    )
}


def build_spec(
    name: str,
    target: str | None,
    *,
    workspace: WorkspaceFS,
    project_id: str,
    settings: Settings,
    commands: Mapping[str, ApprovedCommand] = APPROVED_COMMANDS,
) -> ExecSpec:
    """Validate one request against the approval table and return the container job."""
    command = commands.get(name)
    if command is None:
        raise SandboxPolicyError(f"{name!r} is not an approved command; approved: {', '.join(sorted(commands))}")
    image = APPROVED_IMAGES[command.image]
    argv = list(command.argv)
    try:
        if command.target_suffix is None:
            if target is not None:
                raise SandboxPolicyError(f"{name} takes no target file")
        else:
            if target is None:
                raise SandboxPolicyError(f"{name} needs a target file ending in {command.target_suffix}")
            argv.append(f"{WORKSPACE_TARGET}/{_workspace_file(target, command.target_suffix, workspace, project_id)}")
        workspace_dir = workspace.project_dir(project_id)
    except WorkspaceError as exc:
        raise SandboxPolicyError(f"workspace: {exc}") from exc
    timeout = settings.sandbox_timeout_seconds
    if command.timeout_seconds is not None:
        timeout = min(command.timeout_seconds, timeout)
    return ExecSpec(
        command=name,
        image=image.reference,
        argv=tuple(argv),
        workspace_dir=workspace_dir,
        timeout_seconds=timeout,
        container_name=f"devpilot-sbx-{secrets.token_hex(8)}",
    )


def _workspace_file(target: str, suffix: str, workspace: WorkspaceFS, project_id: str) -> str:
    """A workspace-relative path that exists, is inside the jail and has the required suffix."""
    if len(target) > MAX_TARGET_LENGTH:
        raise SandboxPolicyError("target file path is too long")
    if not target.lower().endswith(suffix):
        raise SandboxPolicyError(f"target file must end in {suffix}")
    try:
        path = workspace.resolve(project_id, target)  # jail, symlink and reserved-name checks
    except PathRejected as exc:
        raise SandboxPolicyError(f"target file: {exc}") from exc
    if not path.is_file():
        raise SandboxPolicyError("target file does not exist in the workspace")
    return PurePosixPath(target.replace("\\", "/")).as_posix()
