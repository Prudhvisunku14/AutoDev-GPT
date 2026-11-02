"""ReplaySandbox answers only for the exact files it recorded, never starts a process, and says it is a replay."""

from __future__ import annotations

import json
import subprocess
import sys
import threading
from pathlib import Path

import pytest

from app.demo.fixtures import GOOD, TREES
from app.sandbox import replay
from app.sandbox.policy import APPROVED_COMMANDS
from app.sandbox.replay import NOT_RECORDED, RECORDINGS_FILE, ReplaySandbox, recording_key
from app.sandbox.spec import ExecSpec, ExecStatus
from app.verification.report import REPLAY_SANDBOX
from app.verification.results import parse_unittest

COMMAND = APPROVED_COMMANDS["python-unittest"]
RECORDINGS = json.loads(RECORDINGS_FILE.read_text(encoding="utf-8"))["recordings"]


def write(root: Path, files: dict[str, bytes]) -> Path:
    for rel, data in files.items():
        (root / rel).parent.mkdir(parents=True, exist_ok=True)
        (root / rel).write_bytes(data)
    return root


def spec(root: Path) -> ExecSpec:
    return ExecSpec(COMMAND.name, "python:3.12-alpine", COMMAND.argv, root, 120.0, "devpilot-sbx-test")


@pytest.mark.parametrize("name", list(TREES))
def test_every_recording_is_what_unittest_prints_for_those_files(tmp_path: Path, name: str) -> None:
    """The sandbox's own command, on the host, on repository fixtures only, as tests/unit/test_verification.py does."""
    root = write(tmp_path, TREES[name])
    done = subprocess.run([sys.executable, *COMMAND.argv[1:]], cwd=root, capture_output=True, text=True, timeout=60)
    saved = next(r for r in RECORDINGS if r["name"] == name)
    assert saved["key"] == recording_key(COMMAND.name, COMMAND.argv, TREES[name])
    assert saved["exit_code"] == done.returncode and saved["status"] == (ExecStatus.SUCCEEDED if done.returncode == 0 else ExecStatus.FAILED).value
    now, then = parse_unittest(done.stderr), parse_unittest(saved["stderr"])
    assert (now.ran, now.failures, now.errors, now.passed_names, now.failed_names) == (then.ran, then.failures, then.errors, then.passed_names, then.failed_names)


def test_there_is_one_recording_per_fixture_tree_and_no_other() -> None:
    assert sorted(r["name"] for r in RECORDINGS) == sorted(TREES)


def test_the_exact_files_are_answered_from_their_recording(tmp_path: Path) -> None:
    result = ReplaySandbox.load().run(spec(write(tmp_path, GOOD)))
    assert result.status is ExecStatus.SUCCEEDED and result.exit_code == 0 and "Ran 5 tests" in result.stderr and result.container.startswith("replay:")


def test_windows_line_endings_are_the_same_files(tmp_path: Path) -> None:
    crlf = {rel: data.replace(b"\n", b"\r\n") for rel, data in GOOD.items()}
    assert ReplaySandbox.load().run(spec(write(tmp_path, crlf))).status is ExecStatus.SUCCEEDED


@pytest.mark.parametrize("change", ["one byte", "an extra file", "a missing file"])
def test_any_other_files_need_the_docker_sandbox(tmp_path: Path, change: str) -> None:
    files = dict(GOOD)
    if change == "one byte":
        files["calculator.py"] = files["calculator.py"].replace(b"a + b", b"a + b ")
    elif change == "an extra file":
        files["notes.txt"] = b"x"
    else:
        files.pop("README.md")
    result = ReplaySandbox.load().run(spec(write(tmp_path, files)))
    assert result.status is ExecStatus.UNAVAILABLE and result.detail == NOT_RECORDED


def test_another_command_on_the_same_files_is_not_answered(tmp_path: Path) -> None:
    other = APPROVED_COMMANDS["python-version"]
    result = ReplaySandbox.load().run(ExecSpec(other.name, "img", other.argv, write(tmp_path, GOOD), 10.0, "c"))
    assert result.status is ExecStatus.UNAVAILABLE


def test_it_never_starts_a_process(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    def refuse(*_args: object, **_kwargs: object) -> None:
        raise AssertionError("a process was started")

    monkeypatch.setattr(subprocess, "Popen", refuse)
    assert ReplaySandbox.load().run(spec(write(tmp_path, GOOD))).status is ExecStatus.SUCCEEDED
    assert "subprocess" not in Path(replay.__file__).read_text(encoding="utf-8")


def test_a_cancelled_run_is_cancelled(tmp_path: Path) -> None:
    stop = threading.Event()
    stop.set()
    assert ReplaySandbox.load().run(spec(write(tmp_path, GOOD)), cancel=stop).status is ExecStatus.CANCELLED


def test_the_report_calls_it_a_replay_and_not_docker() -> None:
    from app.verification import report

    assert REPLAY_SANDBOX == ReplaySandbox.__name__ and "replayed from recorded runs, not Docker" in Path(report.__file__).read_text(encoding="utf-8")
