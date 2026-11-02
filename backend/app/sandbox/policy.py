"""The sandbox's approval table: which images may run and which commands they may run.

GPT never supplies an image, a flag or a shell string. It names a command from
APPROVED_COMMANDS and, where the command needs one, a workspace file. Python builds the
argument list (SECURITY T1, T9). Nothing here starts a shell.
"""

from __future__ import annotations

import math
import re
import secrets
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path, PurePosixPath

from app.config import Settings
from app.sandbox.spec import WORKSPACE_TARGET, ExecSpec
from app.tools.adapters.workspace_fs import PathRejected, WorkspaceError, WorkspaceFS

# name:tag, optionally followed by @sha256:<digest>. Registry ports are not accepted.
_REFERENCE = re.compile(r"[a-z0-9]+(?:[._/-][a-z0-9]+)*:[A-Za-z0-9][A-Za-z0-9._-]{0,127}(?:@sha256:[a-f0-9]{64})?")
# A local image's full ID: immutable, because it is the hash of the image itself. A locally built image has no registry
# digest, so this is how it is pinned (as APPROVED_RUNTIME_IMAGES pins the OpenCode runtime). Never shortened.
IMAGE_ID = re.compile(r"sha256:[a-f0-9]{64}")
MEMORY = re.compile(r"^[1-9][0-9]{0,5}[bkmg]$")  # docker's form, as the settings take it
_UNITS = {"b": 1, "k": 1024, "m": 1024**2, "g": 1024**3}
MAX_TARGET_LENGTH = 512


class SandboxPolicyError(ValueError):
    """A request the approval table refuses. The message is safe to show to GPT."""


def valid_reference(reference: str) -> bool:
    """name:tag (optionally @sha256:<digest>), never `latest`, or a full image ID. A name like `sha256` or `sha512` is
    read as a digest, never as an image with a tag, so anything but a full sha256 ID in that form is refused."""
    if IMAGE_ID.fullmatch(reference):
        return True
    if re.match(r"sha\d*:", reference) or not _REFERENCE.fullmatch(reference):
        return False
    return reference.split("@", 1)[0].rsplit(":", 1)[1] != "latest"


@dataclass(frozen=True)
class ApprovedImage:
    name: str  # the key that commands refer to
    reference: str  # exact reference: name:tag with a version, never `latest`, or a full image ID (sha256:<64 hex>)

    def __post_init__(self) -> None:
        if self.reference.split("@", 1)[0].endswith(":latest"):
            raise ValueError(f"image {self.name!r}: the 'latest' tag is not allowed; pin a version")
        if not valid_reference(self.reference):
            raise ValueError(f"image {self.name!r}: reference must look like name:tag or be a full image ID (sha256:<64 hex>)")

    @property
    def immutable(self) -> bool:
        """True when the reference names exactly one image's content: an image ID or a registry digest."""
        return bool(IMAGE_ID.fullmatch(self.reference)) or "@sha256:" in self.reference


@dataclass(frozen=True)
class Limits:
    """What one job asks for. The settings cap every value: a job may ask for less, never more (SECURITY T2)."""

    memory: str | None = None  # docker's form, e.g. 512m
    cpus: float | None = None
    pids: int | None = None
    timeout_seconds: float | None = None

    def __post_init__(self) -> None:
        problems = []
        if self.memory is not None and not (isinstance(self.memory, str) and MEMORY.fullmatch(self.memory)):
            problems.append(f"memory {self.memory!r} is not like 512m or 1g")
        for name, value in (("cpus", self.cpus), ("timeout_seconds", self.timeout_seconds)):
            if value is not None and (isinstance(value, bool) or not isinstance(value, int | float) or not math.isfinite(value) or value <= 0):
                problems.append(f"{name} {value!r} is not a positive number")
        if self.pids is not None and (isinstance(self.pids, bool) or not isinstance(self.pids, int) or not 16 <= self.pids <= 4096):
            problems.append(f"pids {self.pids!r} is not a whole number from 16 to 4096")
        if problems:
            raise SandboxPolicyError("invalid limits: " + "; ".join(problems))


def memory_bytes(memory: str) -> int:
    if not MEMORY.fullmatch(memory):
        raise SandboxPolicyError(f"memory {memory!r} is not like 512m or 1g")
    return int(memory[:-1]) * _UNITS[memory[-1]]


def capped(limits: Limits, settings: Settings) -> Limits:
    """Each limit the job asked for, lowered to the settings' value where it asked for more. The timeout always has a value."""
    memory = limits.memory
    if memory is not None and memory_bytes(memory) > memory_bytes(settings.sandbox_memory):
        memory = settings.sandbox_memory
    return Limits(
        memory=memory,
        cpus=None if limits.cpus is None else min(limits.cpus, settings.sandbox_cpus),
        pids=None if limits.pids is None else min(limits.pids, settings.sandbox_pids_limit),
        timeout_seconds=min(limits.timeout_seconds or settings.sandbox_timeout_seconds, settings.sandbox_timeout_seconds),
    )


def limits_problem(spec: ExecSpec, settings: Settings) -> str | None:
    """Why a finished job asks for more than the settings allow, or None. The runner checks every job with it."""
    try:
        Limits(spec.memory, spec.cpus, spec.pids, spec.timeout_seconds)
        if spec.memory is not None and memory_bytes(spec.memory) > memory_bytes(settings.sandbox_memory):
            return f"memory {spec.memory} is above the sandbox's {settings.sandbox_memory}"
    except SandboxPolicyError as exc:
        return str(exc)
    if spec.cpus is not None and spec.cpus > settings.sandbox_cpus:
        return f"{spec.cpus:g} CPUs is above the sandbox's {settings.sandbox_cpus:g}"
    if spec.pids is not None and spec.pids > settings.sandbox_pids_limit:
        return f"{spec.pids} processes is above the sandbox's {settings.sandbox_pids_limit}"
    if spec.timeout_seconds > settings.sandbox_timeout_seconds:
        return f"{spec.timeout_seconds:g} s is above the sandbox's {settings.sandbox_timeout_seconds:g} s"
    return None


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
        ApprovedCommand(
            name="python-unittest",
            description="run the project's unittest tests (test*.py); the workspace is read-only and there is no network",
            image="python-runner",
            argv=("python", "-I", "-B", "-m", "unittest", "discover", "-v", "-s", ".", "-p", "test*.py"),
            timeout_seconds=120.0,
        ),
    )
}


def approved_image(reference: str) -> ApprovedImage:
    """The approved entry for exactly this reference. Nothing else runs: not a tag that resembles an approved one, not a
    short or upper-case ID, not another algorithm's digest."""
    for image in APPROVED_IMAGES.values():
        if image.reference == reference:
            return image
    raise SandboxPolicyError("the image is not on the approved list")


def build_job(command: str, image: str, argv: Sequence[str], workspace_dir: Path, settings: Settings, limits: Limits = Limits()) -> ExecSpec:
    """The one place a container job is made. The image must be approved, the argument list is fixed by the caller's
    table (never a shell), and every limit is the smaller of what the job asks for and the settings."""
    approved_image(image)
    args = tuple(argv)
    if not args or not all(isinstance(a, str) and a and "\0" not in a for a in args):
        raise SandboxPolicyError(f"{command}: the argument list must be non-empty strings")
    final = capped(limits, settings)
    assert final.timeout_seconds is not None
    return ExecSpec(
        command=command, image=image, argv=args, workspace_dir=workspace_dir, timeout_seconds=final.timeout_seconds,
        container_name=f"devpilot-sbx-{secrets.token_hex(8)}", memory=final.memory, cpus=final.cpus, pids=final.pids,
    )  # fmt: skip


def build_spec(
    name: str,
    target: str | None,
    *,
    workspace: WorkspaceFS,
    project_id: str,
    settings: Settings,
    commands: Mapping[str, ApprovedCommand] = APPROVED_COMMANDS,
) -> ExecSpec:
    """Validate one request against the approval table and return the container job. An approved command keeps the
    settings' memory, CPU and process limits; only its timeout may be lower."""
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
    return build_job(name, image.reference, argv, workspace_dir, settings, Limits(timeout_seconds=command.timeout_seconds))


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
