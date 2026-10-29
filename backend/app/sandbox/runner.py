"""DockerSandbox: create, run, observe and always remove one container per approved job.

Lifecycle of one job: check the daemon, create the container with every hardening flag,
start it attached with a wall-clock limit, inspect its final state, and remove it in a
`finally` block. Removal happens on every path, including timeouts, cancellation and
exceptions, so a finished or failed job never leaves a container running. Containers left
behind by a crash carry a label and are removed by `sweep_orphans` at the next startup.
"""

from __future__ import annotations

import logging
import threading
import time
from pathlib import Path
from typing import Protocol

from app.config import Settings
from app.sandbox.docker_cli import ContainerState, DockerCli, SandboxError, build_create_args
from app.sandbox.policy import APPROVED_IMAGES
from app.sandbox.process import ProcessResult, SubprocessRunner
from app.sandbox.spec import ExecResult, ExecSpec, ExecStatus

logger = logging.getLogger("devpilot.sandbox")


class SandboxRunner(Protocol):
    def run(self, spec: ExecSpec, *, cancel: threading.Event | None = None) -> ExecResult: ...

    def sweep_orphans(self) -> int: ...


class DockerSandbox:
    def __init__(self, cli: DockerCli, *, settings: Settings) -> None:
        self._cli = cli
        self._settings = settings
        self._root = settings.workspaces_dir.resolve()
        self._approved_references = {img.reference for img in APPROVED_IMAGES.values()}

    @classmethod
    def from_settings(cls, settings: Settings) -> DockerSandbox:
        return cls(DockerCli(settings.docker_cli, SubprocessRunner()), settings=settings)

    @property
    def settings(self) -> Settings:
        return self._settings

    @property
    def cli(self) -> DockerCli:
        return self._cli

    def run(self, spec: ExecSpec, *, cancel: threading.Event | None = None) -> ExecResult:
        """Run one approved job. Raises SandboxUnavailable when Docker cannot be reached."""
        self._check_spec(spec)
        if cancel is not None and cancel.is_set():
            return ExecResult(status=ExecStatus.CANCELLED, command=spec.command, container=spec.container_name)
        self._cli.server_version()
        try:
            self._cli.create(build_create_args(spec, self._settings))
        except SandboxError as exc:
            return ExecResult(status=ExecStatus.ERROR, command=spec.command, container=spec.container_name, detail=str(exc))

        state: ContainerState | None = None
        proc: ProcessResult | None = None
        started = time.monotonic()
        try:
            proc = self._cli.start_attached(
                spec.container_name,
                timeout=spec.timeout_seconds,
                cancel=cancel,
                capture_bytes=self._settings.sandbox_max_output_bytes,
            )
            state = self._cli.inspect(spec.container_name)
        finally:
            duration_ms = int((time.monotonic() - started) * 1000)
            removed = self._cli.remove(spec.container_name)
            if not removed:
                logger.error("could not remove sandbox container %s; the next startup will sweep it", spec.container_name)

        return _result(spec, proc, state, duration_ms, "removed" if removed else "failed")

    def sweep_orphans(self) -> int:
        """Remove sandbox containers that a previous backend process left behind."""
        removed = 0
        for container_id in self._cli.labelled_containers():
            if self._cli.remove(container_id):
                removed += 1
        return removed

    def _check_spec(self, spec: ExecSpec) -> None:
        if spec.image not in self._approved_references:
            raise SandboxError("the image is not on the approved list")
        host = Path(spec.workspace_dir)
        if host.is_symlink() or host.is_junction():
            raise SandboxError("the workspace must not be a symlink or junction")
        if not host.resolve().is_relative_to(self._root) or host.resolve() == self._root:
            raise SandboxError("the workspace is outside the approved workspaces folder")


def _result(
    spec: ExecSpec,
    proc: ProcessResult | None,
    state: ContainerState | None,
    duration_ms: int,
    cleanup: str,
) -> ExecResult:
    status = _classify(proc, state)
    stdout = proc.stdout.decode("utf-8", errors="replace") if proc else ""
    stderr = proc.stderr.decode("utf-8", errors="replace") if proc else ""
    detail = None
    if status is ExecStatus.TIMEOUT:
        detail = f"the command ran longer than {spec.timeout_seconds:g} seconds and was stopped"
    elif status is ExecStatus.OUTPUT_LIMIT:
        detail = "the command printed more output than allowed and was stopped"
    elif status is ExecStatus.MEMORY_LIMIT:
        detail = "the container was stopped for exceeding its memory limit"
    elif status is ExecStatus.ERROR:
        detail = _first_stderr_line(proc) or "the container did not finish"
    return ExecResult(
        status=status,
        command=spec.command,
        container=spec.container_name,
        exit_code=state.exit_code if state is not None else None,
        duration_ms=duration_ms,
        stdout=stdout,
        stderr=stderr,
        stdout_truncated=bool(proc and proc.stdout_truncated),
        stderr_truncated=bool(proc and proc.stderr_truncated),
        oom_killed=bool(state and state.oom_killed),
        cleanup=cleanup,
        detail=detail,
    )


def _first_stderr_line(proc: ProcessResult | None) -> str:
    if proc is None:
        return ""
    for line in proc.stderr.decode("utf-8", errors="replace").splitlines():
        if line.strip():
            return line.strip()[:300]
    return ""


def _classify(proc: ProcessResult | None, state: ContainerState | None) -> ExecStatus:
    # Our own kills come first: after we kill the CLI the container may still report "running".
    if proc is not None and proc.cancelled:
        return ExecStatus.CANCELLED
    if proc is not None and proc.timed_out:
        return ExecStatus.TIMEOUT
    if proc is not None and proc.output_limit:
        return ExecStatus.OUTPUT_LIMIT
    if state is None:
        return ExecStatus.ERROR
    if state.oom_killed:
        return ExecStatus.MEMORY_LIMIT
    if state.status != "exited":
        return ExecStatus.ERROR
    return ExecStatus.SUCCEEDED if state.exit_code == 0 else ExecStatus.FAILED
