"""The process runner, tested with real child processes (this Python). Docker is not involved."""

from __future__ import annotations

import sys
import threading
import time

import pytest

from app.sandbox.process import SubprocessRunner

RUNNER = SubprocessRunner()
PY = sys.executable


def test_captures_stdout_and_the_exit_code() -> None:
    result = RUNNER.run([PY, "-c", "import sys; print('hi'); sys.exit(3)"], timeout=30)
    assert result.returncode == 3
    assert result.stdout.decode().splitlines() == ["hi"]  # Windows pipes turn \n into \r\n
    assert not (result.timed_out or result.cancelled or result.output_limit)


def test_arguments_are_passed_as_a_list_never_through_a_shell() -> None:
    payload = "a; echo b && whoami"
    result = RUNNER.run([PY, "-c", "import sys; print(sys.argv[1])", payload], timeout=30)
    assert result.stdout.decode().splitlines() == [payload]


def test_timeout_kills_the_process() -> None:
    started = time.monotonic()
    result = RUNNER.run([PY, "-c", "import time; time.sleep(60)"], timeout=0.5)
    assert result.timed_out and result.returncode is None
    assert time.monotonic() - started < 15


def test_cancel_kills_the_process() -> None:
    cancel = threading.Event()
    threading.Timer(0.3, cancel.set).start()
    result = RUNNER.run([PY, "-c", "import time; time.sleep(60)"], timeout=60, cancel=cancel)
    assert result.cancelled and result.returncode is None


def test_output_beyond_the_capture_limit_is_dropped_and_marked() -> None:
    result = RUNNER.run([PY, "-c", "print('x' * 5000)"], timeout=30, capture_bytes=1024)
    assert result.returncode == 0  # not killed: only a flood is killed
    assert len(result.stdout) == 1024 and result.stdout_truncated
    assert not result.stderr_truncated


def test_a_flood_of_output_kills_the_process() -> None:
    script = "import sys\nwhile True:\n    sys.stdout.write('x' * 1000)\n"
    result = RUNNER.run([PY, "-c", script], timeout=60, capture_bytes=1024)
    assert result.output_limit and result.returncode is None
    assert result.stdout_truncated


def test_a_missing_executable_raises_file_not_found() -> None:
    with pytest.raises(FileNotFoundError):
        RUNNER.run(["devpilot-no-such-executable-xyz"], timeout=5)
