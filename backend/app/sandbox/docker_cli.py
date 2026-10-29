"""The Docker CLI adapter: the only code that knows docker's command-line syntax (ARCHITECTURE §7.2).

Every call is an argument list. No shell is involved, and no host environment variable
or secret is passed into a container. Each container is created with `--pull never`, so
an image that is not already present on the disk is never downloaded implicitly.
"""

from __future__ import annotations

import threading
from dataclasses import dataclass

from app.config import Settings
from app.sandbox.process import DEFAULT_CAPTURE_BYTES, ProcessResult, ProcessRunner
from app.sandbox.spec import WORKSPACE_TARGET, ExecSpec

CONTAINER_LABEL = "devpilot.sandbox"
# "nobody": numeric, so the image needs no passwd entry for it to work.
SANDBOX_USER = "65534:65534"
SANDBOX_TMPFS = "/tmp:rw,noexec,nosuid,nodev,size=64m"
CLI_TIMEOUT_SECONDS = 60.0
VERSION_TIMEOUT_SECONDS = 30.0
# Characters that would split or change a `--mount` value. A workspace path containing one is refused.
_UNSAFE_MOUNT_CHARS = frozenset(',"\'\r\n')


class SandboxError(Exception):
    """The sandbox could not do the job. The message is safe to show to GPT and in logs."""


class SandboxUnavailable(SandboxError):
    """Docker is not installed, not running or not reachable."""


@dataclass(frozen=True)
class ContainerState:
    status: str  # created, running, exited, ...
    exit_code: int
    oom_killed: bool


def build_create_args(spec: ExecSpec, settings: Settings) -> list[str]:
    """Arguments for `docker create`. Every hardening flag is here, in one place (SECURITY T1, T2)."""
    host = str(spec.workspace_dir)
    if any(ch in _UNSAFE_MOUNT_CHARS for ch in host):
        raise SandboxError("the workspace path contains characters that cannot be mounted safely")
    mount = f"type=bind,source={host},target={WORKSPACE_TARGET},readonly"
    return [
        "create",
        "--name", spec.container_name,
        "--pull", "never",
        "--label", f"{CONTAINER_LABEL}=true",
        "--label", f"devpilot.command={spec.command}",
        "--network", "none",
        "--cap-drop", "ALL",
        "--security-opt", "no-new-privileges",
        "--read-only",
        "--tmpfs", SANDBOX_TMPFS,
        "--user", SANDBOX_USER,
        "--memory", settings.sandbox_memory,
        "--memory-swap", settings.sandbox_memory,
        "--cpus", f"{settings.sandbox_cpus:g}",
        "--pids-limit", str(settings.sandbox_pids_limit),
        "--ulimit", "nofile=256:256",
        "--init",
        "--workdir", WORKSPACE_TARGET,
        "--env", "HOME=/tmp",
        "--mount", mount,
        spec.image,
        *spec.argv,
    ]


class DockerCli:
    def __init__(self, executable: str, runner: ProcessRunner) -> None:
        self._executable = executable
        self._runner = runner

    def server_version(self) -> str:
        """The daemon's version. Raises SandboxUnavailable unless the daemon answers."""
        result = self._call(["version", "--format", "{{.Server.Version}}"], timeout=VERSION_TIMEOUT_SECONDS)
        version = result.stdout.decode("utf-8", errors="replace").strip()
        if result.returncode != 0 or not version:
            raise SandboxUnavailable(f"Docker is not reachable: {_first_line(result.stderr) or 'no answer'}")
        return version

    def create(self, args: list[str]) -> None:
        result = self._call(args, timeout=CLI_TIMEOUT_SECONDS)
        if result.returncode != 0:
            raise SandboxError(f"could not create the container: {_first_line(result.stderr) or 'unknown error'}")

    def start_attached(
        self, name: str, *, timeout: float, cancel: threading.Event | None, capture_bytes: int = DEFAULT_CAPTURE_BYTES
    ) -> ProcessResult:
        """Start and wait, streaming the container's output. Killing this process leaves the container running."""
        return self._call(
            ["start", "-a", name], timeout=timeout, cancel=cancel, capture_bytes=capture_bytes
        )

    def inspect(self, name: str) -> ContainerState | None:
        result = self._call(
            ["inspect", "--format", "{{.State.Status}}|{{.State.ExitCode}}|{{.State.OOMKilled}}", name],
            timeout=CLI_TIMEOUT_SECONDS,
        )
        if result.returncode != 0:
            return None
        parts = result.stdout.decode("utf-8", errors="replace").strip().split("|")
        if len(parts) != 3 or not parts[1].lstrip("-").isdigit():
            return None
        return ContainerState(status=parts[0], exit_code=int(parts[1]), oom_killed=parts[2] == "true")

    def remove(self, name: str) -> bool:
        """Kill and remove a container. True when it is gone afterwards. Never raises."""
        try:
            result = self._call(["rm", "-f", name], timeout=CLI_TIMEOUT_SECONDS)
        except SandboxError:
            return False
        if result.returncode == 0:
            return True
        return b"No such container" in result.stderr

    def labelled_containers(self) -> list[str]:
        result = self._call(
            ["ps", "-aq", "--no-trunc", "--filter", f"label={CONTAINER_LABEL}=true"], timeout=CLI_TIMEOUT_SECONDS
        )
        if result.returncode != 0:
            raise SandboxError(f"could not list containers: {_first_line(result.stderr) or 'unknown error'}")
        return [line for line in result.stdout.decode("utf-8", errors="replace").split() if line]

    def _call(
        self,
        args: list[str],
        *,
        timeout: float,
        cancel: threading.Event | None = None,
        capture_bytes: int = DEFAULT_CAPTURE_BYTES,
    ) -> ProcessResult:
        try:
            return self._runner.run(
                [self._executable, *args], timeout=timeout, cancel=cancel, capture_bytes=capture_bytes
            )
        except FileNotFoundError as exc:
            raise SandboxUnavailable(
                f"the Docker CLI was not found at {self._executable!r}; set DOCKER_CLI in .env to the full path of docker.exe"
            ) from exc
        except OSError as exc:
            raise SandboxUnavailable(f"the Docker CLI could not be started ({type(exc).__name__})") from exc


def _first_line(data: bytes) -> str:
    for line in data.decode("utf-8", errors="replace").splitlines():
        if line.strip():
            return line.strip()[:300]
    return ""
