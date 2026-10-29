"""Runs one OS process with a wall-clock limit, a cancel signal and bounded output.

For the sandbox the process is the Docker CLI. Killing the CLI does not stop the container:
DockerSandbox always removes the container afterwards with `docker rm -f`.
"""

from __future__ import annotations

import subprocess
import threading
import time
from collections.abc import Sequence
from dataclasses import dataclass
from typing import IO, Protocol

DEFAULT_CAPTURE_BYTES = 65_536
# Output beyond this many times the capture size kills the process (a runaway print loop).
KILL_OUTPUT_FACTOR = 16
_CHUNK_BYTES = 4096
_POLL_SECONDS = 0.05
_KILL_WAIT_SECONDS = 10


@dataclass
class ProcessResult:
    returncode: int | None  # None when we killed the process
    stdout: bytes = b""
    stderr: bytes = b""
    stdout_truncated: bool = False
    stderr_truncated: bool = False
    timed_out: bool = False
    cancelled: bool = False
    output_limit: bool = False


class ProcessRunner(Protocol):
    def run(
        self,
        argv: Sequence[str],
        *,
        timeout: float,
        cancel: threading.Event | None = None,
        capture_bytes: int = DEFAULT_CAPTURE_BYTES,
    ) -> ProcessResult: ...


class _Capture:
    """Keeps up to `limit` bytes per stream and counts every byte, so a flood can be detected."""

    def __init__(self, limit: int) -> None:
        self._limit = limit
        self._kill_at = limit * KILL_OUTPUT_FACTOR
        self._lock = threading.Lock()
        self._seen = 0
        self.buffers = {"stdout": bytearray(), "stderr": bytearray()}
        self.truncated = {"stdout": False, "stderr": False}

    @property
    def over_limit(self) -> bool:
        with self._lock:
            return self._seen > self._kill_at

    def pump(self, name: str, stream: IO[bytes]) -> None:
        while True:
            chunk = stream.read1(_CHUNK_BYTES)
            if not chunk:
                return
            with self._lock:
                self._seen += len(chunk)
                buffer = self.buffers[name]
                room = self._limit - len(buffer)
                if room > 0:
                    buffer.extend(chunk[:room])
                if len(chunk) > max(room, 0):
                    self.truncated[name] = True


class SubprocessRunner:
    """The real runner. The argument list goes straight to the OS: no shell is involved."""

    def run(
        self,
        argv: Sequence[str],
        *,
        timeout: float,
        cancel: threading.Event | None = None,
        capture_bytes: int = DEFAULT_CAPTURE_BYTES,
    ) -> ProcessResult:
        proc = subprocess.Popen(
            list(argv), stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE, shell=False
        )
        assert proc.stdout is not None and proc.stderr is not None
        capture = _Capture(capture_bytes)
        readers = [
            threading.Thread(target=capture.pump, args=(name, stream), daemon=True)
            for name, stream in (("stdout", proc.stdout), ("stderr", proc.stderr))
        ]
        for reader in readers:
            reader.start()

        deadline = time.monotonic() + timeout
        stop = {"timed_out": False, "cancelled": False, "output_limit": False}
        while proc.poll() is None:
            if cancel is not None and cancel.is_set():
                stop["cancelled"] = True
            elif time.monotonic() >= deadline:
                stop["timed_out"] = True
            elif capture.over_limit:
                stop["output_limit"] = True
            else:
                time.sleep(_POLL_SECONDS)
                continue
            proc.kill()
            break
        proc.wait(timeout=_KILL_WAIT_SECONDS)
        for reader in readers:
            reader.join(timeout=_KILL_WAIT_SECONDS)

        killed = any(stop.values())
        return ProcessResult(
            returncode=None if killed else proc.returncode,
            stdout=bytes(capture.buffers["stdout"]),
            stderr=bytes(capture.buffers["stderr"]),
            stdout_truncated=capture.truncated["stdout"],
            stderr_truncated=capture.truncated["stderr"],
            **stop,
        )
