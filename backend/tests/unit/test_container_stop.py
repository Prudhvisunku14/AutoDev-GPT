"""Proving a container ended. Docker's answers here are its real texts, captured from 29.9.2.

No Docker is involved: `ScriptedDocker` is a ProcessRunner that plays back one answer per call.
"""

from __future__ import annotations

import threading
from collections.abc import Sequence
from pathlib import Path
from typing import Any

import pytest

from app.coding.container_stop import ContainerTerminator
from app.coding.session_control import RestoreForbidden, SubmitVerdict
from app.coding.types import AttemptPhase, Termination
from app.sandbox.docker_cli import ContainerPresence, DockerCli, SandboxError
from app.sandbox.process import ProcessResult
from tests.coding_fakes import Scene

ID = "c" * 64
OTHER = "d" * 64
ROOT = Path("/not/used")
GONE = ProcessResult(returncode=1, stderr=f"Error response from daemon: No such container: {ID}\n".encode())
UNREACHABLE = ProcessResult(returncode=1, stderr=b'error during connect: Get "http://127.0.0.1:1/v1.56/containers/x/json": dial tcp 127.0.0.1:1: connectex: refused\n')
DENIED = ProcessResult(returncode=1, stderr=b"permission denied while trying to connect to the Docker daemon socket\n")
SOCKET_GONE = ProcessResult(returncode=1, stderr=b"Cannot connect to the Docker daemon at unix:///var/run/docker.sock. Is the docker daemon running?\n")
NO_SUCH_HOST = ProcessResult(returncode=1, stderr=b"error during connect: dial tcp: lookup docker.invalid: no such host\n")
TIMED_OUT = ProcessResult(returncode=None, timed_out=True)
RM_OK = ProcessResult(returncode=0, stdout=ID.encode())
RM_SILENT = ProcessResult(returncode=0)  # what `docker rm -f` really does for a container that does not exist
RM_FAILS = ProcessResult(returncode=1, stderr=b"Error response from daemon: removal of container is already in progress\n")


def inspected(status: str = "running", *, container_id: str = ID, running: str = "true", pid: int = 4242, exit_code: int = 0, oom: str = "false") -> ProcessResult:
    return ProcessResult(returncode=0, stdout=f"{container_id}|{status}|{running}|{pid}|{exit_code}|{oom}\n".encode())


EXITED = inspected("exited", running="false", pid=0, exit_code=137)


class ScriptedDocker:
    """Plays back answers in order. Running out is a test failure: nothing may ask more than was planned."""

    def __init__(self, *answers: ProcessResult | Exception) -> None:
        self.answers = list(answers)
        self.calls: list[list[str]] = []

    def run(self, argv: Sequence[str], *, timeout: float, cancel: threading.Event | None = None, capture_bytes: int = 65536) -> ProcessResult:
        self.calls.append(list(argv[1:]))
        answer = self.answers.pop(0)
        if isinstance(answer, Exception):
            raise answer
        return answer

    def verbs(self) -> list[str]:
        return ["inspect" if c[0] == "container" else c[0] for c in self.calls]


def _cli(*answers: ProcessResult | Exception) -> tuple[DockerCli, ScriptedDocker]:
    runner = ScriptedDocker(*answers)
    return DockerCli("docker", runner), runner


def _terminator(*answers: ProcessResult | Exception, attempts: int = 3) -> tuple[ContainerTerminator, ScriptedDocker]:
    cli, runner = _cli(*answers)
    return ContainerTerminator(cli, ID, attempts=attempts, sleep=lambda _: None), runner


# --- What one inspection means ---------------------------------------------------------------------


@pytest.mark.parametrize("status", ["running", "paused", "restarting", "removing"])
def test_a_container_that_may_have_a_process_is_running(status: str) -> None:
    cli, _ = _cli(inspected(status))
    seen = cli.inspect_container(ID, expected_id=ID)
    assert seen.presence is ContainerPresence.RUNNING and seen.container_id == ID


@pytest.mark.parametrize("status", ["exited", "dead", "created"])
def test_a_container_with_no_process_is_stopped(status: str) -> None:
    cli, _ = _cli(inspected(status, running="false", pid=0, exit_code=1, oom="true"))
    seen = cli.inspect_container(ID, expected_id=ID)
    assert seen.presence is ContainerPresence.STOPPED and seen.state is not None
    assert seen.state.exit_code == 1 and seen.state.oom_killed


def test_only_dockers_exact_line_for_this_reference_is_not_found() -> None:
    cli, _ = _cli(GONE)
    assert cli.inspect_container(ID).presence is ContainerPresence.NOT_FOUND


@pytest.mark.parametrize(
    "answer",
    [
        UNREACHABLE, DENIED, SOCKET_GONE, NO_SUCH_HOST, TIMED_OUT,
        ProcessResult(returncode=None, cancelled=True),
        ProcessResult(returncode=1),
        ProcessResult(returncode=1, stderr=b"No such container"),  # not Docker's whole line
        ProcessResult(returncode=1, stderr=f"Error response from daemon: No such container: {OTHER}\n".encode()),  # another container
        ProcessResult(returncode=1, stderr=f"warning: Error response from daemon: No such container: {ID}\n".encode()),
        ProcessResult(returncode=125, stderr=b"docker: 'container' is not a docker command\n"),
        ProcessResult(returncode=0),
        ProcessResult(returncode=0, stdout=b"not the format at all\n"),
        ProcessResult(returncode=0, stdout=b"||||||\n"),
        ProcessResult(returncode=0, stdout=f"{ID}|exited|false|0|0\n".encode()),  # a field short
        ProcessResult(returncode=0, stdout=f"{ID}|exited|false|0|0|false|extra\n".encode()),
        ProcessResult(returncode=0, stdout=f"short|exited|false|0|0|false\n".encode()),
        ProcessResult(returncode=0, stdout=f"{ID}|exited|maybe|0|0|false\n".encode()),
        ProcessResult(returncode=0, stdout=f"{ID}|exited|false|x|0|false\n".encode()),
        ProcessResult(returncode=0, stdout=f"{ID}|exited|false|0|x|false\n".encode()),
        ProcessResult(returncode=0, stdout=f"{ID}|exited|false|0|0|maybe\n".encode()),
        inspected("something-new"),
        inspected("exited", running="true", pid=0),  # stopped, says one field; running, says another
        inspected("exited", running="false", pid=77),
    ],
)  # fmt: skip
def test_anything_else_is_unknown_never_gone(answer: ProcessResult) -> None:
    cli, _ = _cli(answer)
    assert cli.inspect_container(ID, expected_id=ID).presence is ContainerPresence.UNKNOWN_ERROR


@pytest.mark.parametrize("failure", [FileNotFoundError("docker"), OSError("spawn failed"), TimeoutError(), RuntimeError("anything")])
def test_a_cli_that_cannot_be_run_is_unknown(failure: Exception) -> None:
    cli, _ = _cli(failure)
    seen = cli.inspect_container(ID)
    assert seen.presence is ContainerPresence.UNKNOWN_ERROR and "could not be asked" in seen.detail


def test_an_answer_about_another_container_proves_nothing() -> None:
    cli, _ = _cli(inspected("exited", container_id=OTHER, running="false", pid=0))
    seen = cli.inspect_container("reused-name", expected_id=ID)
    assert seen.presence is ContainerPresence.UNKNOWN_ERROR and "different container" in seen.detail


def test_the_sandbox_final_state_is_still_a_plain_state_or_none() -> None:
    cli, _ = _cli(EXITED)
    state = cli.final_state("box")
    assert state is not None and state.status == "exited" and state.exit_code == 137
    for answer in (GONE, UNREACHABLE, TIMED_OUT):
        cli, _ = _cli(answer)
        assert cli.final_state("box") is None


def test_tidying_up_keeps_its_meaning_and_never_raises() -> None:
    assert _cli(RM_OK)[0].remove(ID) is True
    assert _cli(GONE)[0].remove(ID) is True
    assert _cli(UNREACHABLE)[0].remove(ID) is False
    assert _cli(TIMED_OUT)[0].remove(ID) is False
    assert _cli(SandboxError("no docker"))[0].remove(ID) is False


# --- Terminating, by full ID -----------------------------------------------------------------------


def test_a_successful_stop_is_confirmed_by_a_separate_read() -> None:
    stopper, docker = _terminator(inspected(), RM_OK, GONE)
    result = stopper.terminate()
    assert result.confirmed and docker.verbs() == ["inspect", "rm", "inspect"]
    assert all(ID in call for call in docker.calls)  # the ID, never a name
    assert "before: RUNNING" in result.evidence and "after: NOT_FOUND" in result.evidence


def test_a_container_already_stopped_is_removed_and_confirmed() -> None:
    stopper, docker = _terminator(EXITED, RM_OK, GONE)
    assert stopper.terminate().confirmed and docker.verbs() == ["inspect", "rm", "inspect"]


def test_a_stopped_container_that_cannot_be_removed_still_has_no_process() -> None:
    stopper, _ = _terminator(EXITED, RM_FAILS, EXITED)
    result = stopper.terminate()
    assert result.confirmed and "removal of container is already in progress" in result.evidence


def test_a_container_already_gone_needs_no_stop() -> None:
    stopper, docker = _terminator(GONE)
    assert stopper.terminate().confirmed and docker.verbs() == ["inspect"]


def test_an_exit_code_of_zero_from_rm_proves_nothing() -> None:
    stopper, _ = _terminator(inspected(), RM_SILENT, inspected(), RM_SILENT, inspected(), RM_SILENT, inspected())
    result = stopper.terminate()
    assert not result.confirmed and "after: RUNNING" in result.evidence


def test_a_container_that_takes_a_moment_is_asked_again() -> None:
    stopper, _ = _terminator(inspected(), RM_OK, inspected(), RM_OK, GONE)
    assert stopper.terminate().confirmed


def test_a_daemon_that_cannot_be_reached_is_never_a_stop() -> None:
    stopper, _ = _terminator(*[UNREACHABLE] * 7)
    result = stopper.terminate()
    assert not result.confirmed and "UNKNOWN_ERROR" in result.evidence and "error during connect" in result.evidence


def test_an_inspect_timeout_is_never_a_stop() -> None:
    stopper, _ = _terminator(inspected(), RM_OK, TIMED_OUT, RM_OK, TIMED_OUT, RM_OK, TIMED_OUT)
    result = stopper.terminate()
    assert not result.confirmed and "did not answer in time" in result.evidence


def test_a_malformed_answer_is_never_a_stop() -> None:
    bad = ProcessResult(returncode=0, stdout=b"garbage")
    stopper, _ = _terminator(inspected(), RM_OK, bad, RM_OK, bad, RM_OK, bad)
    assert not stopper.terminate().confirmed


def test_a_different_container_is_never_a_stop() -> None:
    wrong = inspected("exited", container_id=OTHER, running="false", pid=0)
    stopper, _ = _terminator(inspected(), RM_OK, wrong, RM_OK, wrong, RM_OK, wrong)
    result = stopper.terminate()
    assert not result.confirmed and "different container" in result.evidence


def test_a_container_still_running_after_the_stop_is_not_confirmed() -> None:
    stopper, docker = _terminator(inspected(), RM_OK, inspected(), RM_OK, inspected(), RM_OK, inspected())
    result = stopper.terminate()
    assert not result.confirmed and docker.verbs().count("rm") == 3


@pytest.mark.parametrize(
    "answers",
    [
        (UNREACHABLE, UNREACHABLE, inspected(), UNREACHABLE, TIMED_OUT, RM_FAILS, DENIED),
        (TIMED_OUT, RM_FAILS, UNREACHABLE, RM_FAILS, inspected(), RM_FAILS, SOCKET_GONE),
        (inspected(), RM_FAILS, DENIED, UNREACHABLE, UNREACHABLE, RM_OK, TIMED_OUT),
    ],
)
def test_a_stop_and_an_inspection_failing_together_are_never_a_stop(answers: tuple[ProcessResult, ...]) -> None:
    stopper, _ = _terminator(*answers)
    assert not stopper.terminate().confirmed


def test_a_failed_stop_whose_container_is_then_gone_is_confirmed_by_the_read() -> None:
    stopper, _ = _terminator(inspected(), UNREACHABLE, UNREACHABLE, RM_OK, GONE)
    assert stopper.terminate().confirmed


def test_a_reader_that_recovers_after_a_failure_can_still_confirm() -> None:
    stopper, _ = _terminator(UNREACHABLE, RM_OK, GONE)
    assert stopper.terminate().confirmed


@pytest.mark.parametrize("identity", ["", "short", "reused-name", "C" * 64, "c" * 63, None])
def test_without_a_full_id_nothing_is_confirmed_or_even_asked(identity: Any) -> None:
    cli, docker = _cli()
    result = ContainerTerminator(cli, identity).terminate()
    assert not result.confirmed and docker.calls == [] and "full container ID" in result.evidence


# --- Workspace restoration ordering ----------------------------------------------------------------


def _stop_with(*answers: ProcessResult | Exception) -> tuple[Scene, Any, Any, ScriptedDocker]:
    """A real AttemptGuard and a real ContainerTerminator over scripted Docker answers."""
    scene = Scene()
    stopper, docker = _terminator(*answers)

    def terminate() -> Termination:
        scene.log.append("terminate")
        result = stopper.terminate()
        if result.confirmed:
            scene.engine.killed()
        return result

    scene.writer.terminate = terminate  # type: ignore[method-assign]
    guard = scene.guard(call_id="call_1")
    assert scene.submit(guard) is SubmitVerdict.ACCEPTED
    return scene, guard, guard.secure(scene.engine, "a timeout"), docker


def test_the_restore_happens_only_after_docker_confirmed_the_stop() -> None:
    scene, guard, report, _ = _stop_with(inspected(), RM_OK, GONE)
    assert report.safe_to_restore and report.process_terminated
    assert "after: NOT_FOUND" in report.stop_evidence
    guard.restore(scene.checkpoint, ROOT)
    assert scene.log.index("terminate") < scene.log.index("restore")


@pytest.mark.parametrize(
    "answers",
    [
        pytest.param([UNREACHABLE] * 11, id="daemon-unavailable"),
        pytest.param([TIMED_OUT] * 11, id="inspect-timeout"),
        pytest.param([DENIED] * 11, id="permission-denied"),
        pytest.param([inspected(), RM_OK] + [inspected(), RM_OK] * 4 + [inspected()], id="still-running"),
        pytest.param([inspected(), RM_OK] + [ProcessResult(returncode=0, stdout=b"odd"), RM_OK] * 4 + [ProcessResult(returncode=0, stdout=b"odd")], id="malformed"),
    ],
)
def test_a_docker_error_can_never_permit_a_restore(answers: list[ProcessResult]) -> None:
    scene = Scene(abort_stops=False)  # the abort is acknowledged and the writer carries on
    stopper, _ = _terminator(*answers, attempts=5)
    scene.writer.terminate = stopper.terminate  # type: ignore[method-assign]
    guard = scene.guard()
    scene.submit(guard)
    report = guard.secure(scene.engine, "a timeout")
    assert report.quarantined and not report.process_terminated and scene.engine.working
    with pytest.raises(RestoreForbidden):
        guard.restore(scene.checkpoint, ROOT)
    assert "restore" not in scene.log


def test_the_evidence_and_the_original_failure_survive_together() -> None:
    scene = Scene()
    stopper, _ = _terminator(*[UNREACHABLE] * 7)
    scene.writer.terminate = stopper.terminate  # type: ignore[method-assign]
    guard = scene.guard()
    scene.submit(guard)
    guard.secure(scene.engine, "no result within 600 seconds")
    saved = scene.store.only()
    assert saved.phase is AttemptPhase.QUARANTINED and saved.failure_reason == "no result within 600 seconds"
    assert saved.stop_evidence is not None and "error during connect" in saved.stop_evidence


def test_a_stop_that_raises_keeps_a_reason() -> None:
    scene = Scene()
    scene.writer.raises = True
    guard = scene.guard()
    scene.submit(guard)
    assert "RuntimeError" in guard.secure(scene.engine, "x").stop_evidence


def test_a_truthy_answer_that_is_not_a_termination_is_not_a_stop() -> None:
    scene = Scene()
    scene.writer.terminate = lambda: True  # type: ignore[assignment,method-assign,return-value]
    guard = scene.guard()
    scene.submit(guard)
    assert guard.secure(scene.engine, "x").quarantined


def test_the_identity_is_kept_for_recovery() -> None:
    scene = Scene()
    guard = scene.guard()
    guard.record.container_id = ID
    scene.store.save(guard.record)
    assert scene.store.only().container_id == ID
