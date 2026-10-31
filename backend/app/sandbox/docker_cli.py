"""The Docker CLI adapter: the only code that knows docker's command-line syntax (ARCHITECTURE §7.2).

Every call is an argument list. No shell is involved, and no host environment variable
or secret is passed into a container. Each container is created with `--pull never`, so
an image that is not already present on the disk is never downloaded implicitly.

`inspect_container` keeps four outcomes apart, because they mean different things to anything that
must know whether a process is still alive. Only Docker's own exact "No such container" line is
NOT_FOUND: an unreachable daemon, a timeout or an odd answer is UNKNOWN_ERROR, never "gone". The
exit code of `docker rm -f` proves nothing: on 29.9.2 it exits 0 and prints nothing for a container
that does not exist.
"""

from __future__ import annotations

import re
import threading
from dataclasses import dataclass
from enum import StrEnum

from app.config import Settings
from app.sandbox.process import DEFAULT_CAPTURE_BYTES, ProcessResult, ProcessRunner
from app.sandbox.spec import WORKSPACE_TARGET, ExecSpec

CONTAINER_LABEL = "devpilot.sandbox"
# "nobody": numeric, so the image needs no passwd entry for it to work.
SANDBOX_USER = "65534:65534"
SANDBOX_TMPFS = "/tmp:rw,noexec,nosuid,nodev,size=64m"
CLI_TIMEOUT_SECONDS = 60.0
VERSION_TIMEOUT_SECONDS = 30.0
INSPECT_FORMAT = "{{.Id}}|{{.State.Status}}|{{.State.Running}}|{{.State.Pid}}|{{.State.ExitCode}}|{{.State.OOMKilled}}"
FULL_ID = re.compile(r"^[0-9a-f]{64}$")
# A process may be alive in each of these. "removing" is not gone until Docker says so.
_ALIVE_STATES = frozenset({"running", "paused", "restarting", "removing"})
_STOPPED_STATES = frozenset({"exited", "dead", "created"})
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


class ContainerPresence(StrEnum):
    RUNNING = "RUNNING"  # it exists, and a process may be alive
    STOPPED = "STOPPED"  # it exists, and Docker reports no process
    NOT_FOUND = "NOT_FOUND"  # Docker itself says this container does not exist
    UNKNOWN_ERROR = "UNKNOWN_ERROR"  # nothing can be concluded, whatever the reason


@dataclass(frozen=True)
class Inspection:
    presence: ContainerPresence
    container_id: str | None = None
    state: ContainerState | None = None
    detail: str = ""  # safe to log: Docker's own first line, or why the answer was not believed


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

    def inspect_container(self, reference: str, *, expected_id: str | None = None) -> Inspection:
        """What Docker can say about one container. Never raises, and never guesses.

        `expected_id` is the full ID the caller created: an answer about any other container proves
        nothing about that one, because a name can be reused."""
        try:
            result = self._call(["container", "inspect", "--format", INSPECT_FORMAT, reference], timeout=CLI_TIMEOUT_SECONDS)
        except Exception as exc:  # whatever went wrong, it says nothing about the container
            return Inspection(ContainerPresence.UNKNOWN_ERROR, detail=f"docker could not be asked ({type(exc).__name__})")
        if result.timed_out or result.cancelled or result.returncode is None:
            return Inspection(ContainerPresence.UNKNOWN_ERROR, detail="docker did not answer in time")
        if result.returncode != 0:
            line = _first_line(result.stderr)
            if line == f"Error response from daemon: No such container: {reference}":
                return Inspection(ContainerPresence.NOT_FOUND, detail=line)
            return Inspection(ContainerPresence.UNKNOWN_ERROR, detail=line or f"docker exited {result.returncode}")
        return _parse_inspection(result.stdout, expected_id)

    def final_state(self, name: str) -> ContainerState | None:
        """How a finished container ended, for reporting. None means no state could be read, which
        says nothing about whether it exists: use `inspect_container` for that."""
        return self.inspect_container(name).state

    def remove(self, name: str) -> bool:
        """Kill and remove a container. True when Docker reports no error. Never raises.

        Good enough for tidying up. It does not prove anything stopped: see `ContainerTerminator`."""
        return self.remove_detail(name)[0]

    def remove_detail(self, reference: str) -> tuple[bool, str]:
        """As `remove`, with what Docker said. Never raises."""
        try:
            result = self._call(["rm", "-f", reference], timeout=CLI_TIMEOUT_SECONDS)
        except SandboxError as exc:
            return False, str(exc)
        if result.returncode == 0:
            return True, "docker rm reported no error"
        line = _first_line(result.stderr)
        return line == f"Error response from daemon: No such container: {reference}", line or "docker rm did not finish"

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


def _parse_inspection(stdout: bytes, expected_id: str | None) -> Inspection:
    """Docker's answer, accepted only if every field is what it should be."""
    unknown = ContainerPresence.UNKNOWN_ERROR
    parts = stdout.decode("utf-8", errors="replace").strip().split("|")
    if len(parts) != 6:
        return Inspection(unknown, detail="docker's answer has the wrong shape")
    container_id, status, running, pid, exit_code, oom = parts
    if not FULL_ID.fullmatch(container_id) or running not in ("true", "false") or oom not in ("true", "false"):
        return Inspection(unknown, detail="docker's answer has the wrong shape")
    if not pid.isdigit() or not exit_code.lstrip("-").isdigit():
        return Inspection(unknown, detail="docker's answer has the wrong shape")
    if expected_id is not None and container_id != expected_id:
        return Inspection(unknown, container_id=container_id, detail="a different container answered to this reference")
    state = ContainerState(status=status, exit_code=int(exit_code), oom_killed=oom == "true")
    if status in _ALIVE_STATES:
        return Inspection(ContainerPresence.RUNNING, container_id, state, f"state {status}")
    if status in _STOPPED_STATES and running == "false" and pid == "0":
        return Inspection(ContainerPresence.STOPPED, container_id, state, f"state {status}")
    return Inspection(unknown, container_id, state, f"state {status} is unknown or contradicts running={running} pid={pid}")


def _first_line(data: bytes) -> str:
    for line in data.decode("utf-8", errors="replace").splitlines():
        if line.strip():
            return line.strip()[:300]
    return ""
