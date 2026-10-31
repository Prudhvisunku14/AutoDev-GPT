"""FakeDockerHost: stands in for the Docker CLI in unit tests. It never starts a container.

It implements the ProcessRunner protocol, so DockerCli and DockerSandbox run unchanged.
Each call is recorded in `calls`, which lets tests check the order and the flags.
"""

from __future__ import annotations

import threading
from collections.abc import Sequence
from dataclasses import dataclass, field

from app.sandbox.process import DEFAULT_CAPTURE_BYTES, ProcessResult


@dataclass
class FakeContainer:
    status: str = "created"
    exit_code: int = 0
    oom: bool = False


@dataclass
class FakeDockerHost:
    calls: list[list[str]] = field(default_factory=list)
    containers: dict[str, FakeContainer] = field(default_factory=dict)
    reachable: bool = True
    cli_missing: bool = False
    create_error: str | None = None
    # What `docker start -a` does: exit | timeout | cancel | flood | fail
    start_mode: str = "exit"
    start_exception: Exception | None = None
    exit_code: int = 0
    oom_killed: bool = False
    stdout: bytes = b"ok\n"
    stderr: bytes = b""
    remove_fails: bool = False
    labelled: list[str] = field(default_factory=list)

    def verbs(self) -> list[str]:
        return [call[1] if call[0] == "container" else call[0] for call in self.calls]

    def run(
        self,
        argv: Sequence[str],
        *,
        timeout: float,
        cancel: threading.Event | None = None,
        capture_bytes: int = DEFAULT_CAPTURE_BYTES,
    ) -> ProcessResult:
        if self.cli_missing:
            raise FileNotFoundError(argv[0])
        args = list(argv[1:])
        self.calls.append(args)
        verb = args[0]
        if verb == "version":
            if not self.reachable:
                return ProcessResult(returncode=1, stderr=b"error during connect: the docker_engine pipe was not found")
            return ProcessResult(returncode=0, stdout=b"29.9.2\n")
        if verb == "create":
            return self._create(args)
        if verb == "start":
            return self._start(args[-1], capture_bytes)
        if verb == "container" and args[1] == "inspect":
            return self._inspect(args[-1])
        if verb == "rm":
            return self._rm(args[-1])
        if verb == "ps":
            return ProcessResult(returncode=0, stdout="".join(f"{cid}\n" for cid in self.labelled).encode())
        raise AssertionError(f"unexpected docker call: {args}")

    def _create(self, args: list[str]) -> ProcessResult:
        if self.create_error is not None:
            return ProcessResult(returncode=125, stderr=self.create_error.encode())
        name = args[args.index("--name") + 1]
        self.containers[name] = FakeContainer()
        return ProcessResult(returncode=0, stdout=f"{name}-id\n".encode())

    def _start(self, name: str, capture_bytes: int) -> ProcessResult:
        if self.start_exception is not None:
            raise self.start_exception
        container = self.containers.get(name)
        if container is None:
            return ProcessResult(returncode=1, stderr=f"No such container: {name}".encode())
        if self.start_mode == "timeout":
            container.status = "running"
            return ProcessResult(returncode=None, stdout=b"partial", timed_out=True)
        if self.start_mode == "cancel":
            container.status = "running"
            return ProcessResult(returncode=None, cancelled=True)
        if self.start_mode == "flood":
            container.status = "running"
            return ProcessResult(returncode=None, output_limit=True)
        if self.start_mode == "fail":
            return ProcessResult(returncode=125, stderr=b"OCI runtime start failed: exec format error")
        # The real runner truncates captured output; the fake does the same so the result matches.
        container.status = "exited"
        container.exit_code = self.exit_code
        container.oom = self.oom_killed
        return ProcessResult(
            returncode=self.exit_code,
            stdout=self.stdout[:capture_bytes],
            stderr=self.stderr[:capture_bytes],
            stdout_truncated=len(self.stdout) > capture_bytes,
            stderr_truncated=len(self.stderr) > capture_bytes,
        )

    def _inspect(self, name: str) -> ProcessResult:
        container = self.containers.get(name)
        if container is None:
            return ProcessResult(returncode=1, stderr=f"Error response from daemon: No such container: {name}\n".encode())
        alive = container.status == "running"
        fields = ("a" * 64, container.status, "true" if alive else "false", 4242 if alive else 0, container.exit_code, "true" if container.oom else "false")
        return ProcessResult(returncode=0, stdout=("|".join(map(str, fields)) + "\n").encode())

    def _rm(self, name: str) -> ProcessResult:
        if self.remove_fails:
            return ProcessResult(returncode=1, stderr=b"device or resource busy")
        if name not in self.containers:
            return ProcessResult(returncode=1, stderr=f"Error response from daemon: No such container: {name}\n".encode())
        del self.containers[name]
        return ProcessResult(returncode=0, stdout=f"{name}\n".encode())
