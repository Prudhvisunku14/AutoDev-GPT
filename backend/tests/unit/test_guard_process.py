"""The guard: what it stops, in what order, and what it refuses to call clean. Real processes, no containers."""

from __future__ import annotations

import json
import subprocess
import sys
import textwrap
import time
from pathlib import Path
from typing import Any

import pytest

from app.coding import guard_process
from app.coding.guard_process import QUARANTINE_FILE, READY_FILE, REPORT_FILE, RESOLVED_FILE, GuardUnavailable, clean_environment, main, record, start_guard, stop_everything, wait_for_exit
from app.coding.launcher import EDGE_LABEL
from app.coding.opencode_container import CONTAINER_LABEL
from app.coding.types import Termination
from app.sandbox.docker_cli import CONTAINER_LABEL as SANDBOX_LABEL

BACKEND = Path(__file__).resolve().parents[2]
NETWORKS = ("devpilot-model", "devpilot-core")


class FakeCli:
    def __init__(self, *, writers: tuple[str, ...] = ("w1",), edges: tuple[str, ...] = ("e1",), sandboxes: tuple[str, ...] = (), listing_fails: bool = False, stays: bool = False) -> None:
        self.running = {CONTAINER_LABEL: list(writers), EDGE_LABEL: list(edges), SANDBOX_LABEL: list(sandboxes)}
        self.listing_fails = listing_fails
        self.stays = stays
        self.calls: list[str] = []

    def labelled_containers(self, label: str) -> list[str]:
        if self.listing_fails:
            raise OSError("no daemon")
        return list(self.running[label])

    def remove(self, cid: str) -> bool:
        self.calls.append(f"remove {cid}")
        if not self.stays:
            self.running[EDGE_LABEL] = [c for c in self.running[EDGE_LABEL] if c != cid]
        return True

    def remove_network(self, name: str) -> bool:
        self.calls.append(f"network {name}")
        return True


def _terminator(*, confirmed: bool = True, raises: bool = False) -> Any:
    def build(cli: FakeCli, cid: str) -> Any:
        class Stop:
            def terminate(self) -> Termination:
                cli.calls.append(f"stop {cid}")
                if raises:
                    raise RuntimeError("the stop broke")
                cli.running[CONTAINER_LABEL] = [c for c in cli.running[CONTAINER_LABEL] if c != cid or not confirmed]
                return Termination(confirmed, f"evidence for {cid}")

        return Stop()

    return build


def test_the_writer_goes_first_then_the_edges_then_the_networks() -> None:
    cli = FakeCli()
    outcome = stop_everything(cli, NETWORKS, terminator=_terminator())  # type: ignore[arg-type]
    assert cli.calls == ["stop w1", "remove e1", "network devpilot-model", "network devpilot-core"]
    assert outcome["status"] == "clean" and outcome["writers"][0] == {"container": "w1", "confirmed": True, "evidence": "evidence for w1"}


def test_nothing_running_is_clean() -> None:
    assert stop_everything(FakeCli(writers=(), edges=()), NETWORKS, terminator=_terminator())["status"] == "clean"  # type: ignore[arg-type]


def test_an_unconfirmed_stop_is_never_clean_and_the_networks_stay() -> None:
    cli = FakeCli()
    outcome = stop_everything(cli, NETWORKS, terminator=_terminator(confirmed=False))  # type: ignore[arg-type]
    assert outcome["status"] == "quarantined" and "could not be confirmed" in outcome["reason"]
    assert not [c for c in cli.calls if c.startswith("network")]


def test_the_gateway_still_goes_when_the_writer_would_not_so_no_more_model_requests_can_leave() -> None:
    cli = FakeCli()
    stop_everything(cli, NETWORKS, terminator=_terminator(confirmed=False))  # type: ignore[arg-type]
    assert "remove e1" in cli.calls


def test_a_stop_that_raises_is_an_unconfirmed_stop() -> None:
    outcome = stop_everything(FakeCli(), NETWORKS, terminator=_terminator(raises=True))  # type: ignore[arg-type]
    assert outcome["status"] == "quarantined" and outcome["writers"][0]["evidence"] == "the stop raised RuntimeError"


def test_docker_that_cannot_be_asked_proves_nothing_is_stopped() -> None:
    cli = FakeCli(listing_fails=True)
    outcome = stop_everything(cli, NETWORKS, terminator=_terminator())  # type: ignore[arg-type]
    assert outcome["status"] == "quarantined" and "could not say" in outcome["reason"] and cli.calls == []


def test_containers_still_listed_after_the_stop_are_not_clean() -> None:
    outcome = stop_everything(FakeCli(stays=True), NETWORKS, terminator=_terminator())  # type: ignore[arg-type]
    assert outcome["status"] == "quarantined" and "still listed" in outcome["reason"]


def test_the_test_sandboxes_are_removed_too() -> None:
    cli = FakeCli(sandboxes=("s1",))
    cli.running[SANDBOX_LABEL] = ["s1"]
    stop_everything(cli, NETWORKS, terminator=_terminator())  # type: ignore[arg-type]
    assert "remove s1" in cli.calls


def test_a_quarantine_leaves_a_marker_and_a_confirmed_stop_resolves_it(tmp_path: Path) -> None:
    record(tmp_path, "the watched process ended", {"status": "quarantined", "reason": "unconfirmed"})
    assert json.loads((tmp_path / QUARANTINE_FILE).read_text(encoding="utf-8"))["reason"] == "unconfirmed"
    record(tmp_path, "asked to stop everything", {"status": "clean", "reason": ""})
    assert not (tmp_path / QUARANTINE_FILE).exists() and (tmp_path / RESOLVED_FILE).is_file()
    assert json.loads((tmp_path / REPORT_FILE).read_text(encoding="utf-8"))["trigger"] == "asked to stop everything"


def test_a_clean_stop_leaves_no_marker(tmp_path: Path) -> None:
    record(tmp_path, "the watched process ended", {"status": "clean", "reason": ""})
    assert not (tmp_path / QUARANTINE_FILE).exists() and not (tmp_path / RESOLVED_FILE).exists()


@pytest.mark.parametrize("name", ["OPENAI_API_KEY", "DEVPILOT_GATEWAY_OPENAI_KEY", "DEVPILOT_API_TOKEN", "OPENCODE_SERVER_PASSWORD", "client_secret", "DEVPILOT_FILTER_TOKEN"])
def test_the_guard_is_given_no_credential(name: str) -> None:
    assert clean_environment({name: "x", "PATH": "p"}) == {"PATH": "p"}


def test_a_process_that_ended_is_seen_and_one_that_runs_is_not() -> None:
    quick = subprocess.Popen([sys.executable, "-c", "pass"])
    assert wait_for_exit(quick.pid, 20) is True
    quick.wait()
    slow = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"])
    try:
        assert wait_for_exit(slow.pid, 0.1) is False
    finally:
        slow.kill()
        slow.wait()


def test_a_process_past_its_deadline_is_killed(tmp_path: Path) -> None:
    slow = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
    try:
        assert "deadline passed" in guard_process._watch(slow.pid, 0.1)
        assert slow.wait(timeout=15) is not None
    finally:
        slow.kill()


def test_docker_that_is_not_there_leaves_a_marker_and_no_success(tmp_path: Path) -> None:
    assert main(["--state", str(tmp_path), "--once", "--docker", "docker-is-not-installed-here", "--network=devpilot-model"]) == 0
    assert json.loads((tmp_path / REPORT_FILE).read_text(encoding="utf-8"))["status"] == "quarantined"
    assert (tmp_path / QUARANTINE_FILE).is_file()


def test_the_guard_needs_something_to_do(tmp_path: Path) -> None:
    with pytest.raises(SystemExit):
        main(["--state", str(tmp_path)])


def _victim(state: Path, *ending: str) -> str:
    return textwrap.dedent(f"""
        import os, signal, sys
        from pathlib import Path
        from app.coding.guard_process import start_guard
        start_guard(Path({str(state)!r}), deadline=60, networks=("devpilot-model",), docker="docker-is-not-installed-here")
        print("watching", flush=True)
        {ending[0]}
    """)


def _report(state: Path) -> dict[str, Any]:
    deadline = time.monotonic() + 30
    while time.monotonic() < deadline:
        try:
            return dict(json.loads((state / REPORT_FILE).read_text(encoding="utf-8")))
        except (OSError, ValueError):
            time.sleep(0.1)
    raise AssertionError(f"no report: {(state / 'guard.log').read_text(encoding='utf-8') if (state / 'guard.log').exists() else 'no log'}")


@pytest.mark.parametrize("ending", ["sys.exit(0)", "os.kill(os.getpid(), signal.SIGTERM)"], ids=["a normal exit", "killed outright"])
def test_the_guard_outlives_the_process_that_started_it_however_that_ended(tmp_path: Path, ending: str) -> None:
    state = tmp_path / "guard"
    victim = tmp_path / "victim.py"
    victim.write_text(_victim(state, ending), encoding="utf-8")
    subprocess.run([sys.executable, "-B", str(victim)], cwd=BACKEND, env={**guard_process.clean_environment(__import__("os").environ), "PYTHONPATH": str(BACKEND)}, timeout=60, capture_output=True)
    report = _report(state)
    assert report["trigger"] == "the watched process ended" and report["status"] == "quarantined", "this docker cannot be reached, so nothing is proved stopped"
    assert (state / READY_FILE).is_file()


def test_no_guard_means_no_run(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    def refuse(*_: Any, **__: Any) -> Any:
        return subprocess.CompletedProcess([], 1, b"", b"")

    monkeypatch.setattr(guard_process.subprocess, "run", refuse)
    with pytest.raises(GuardUnavailable, match="did not start"):
        start_guard(tmp_path, deadline=1, networks=())


def test_a_guard_that_never_reports_means_no_run(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(guard_process.subprocess, "run", lambda *_, **__: subprocess.CompletedProcess([], 0, b"", b""))
    monkeypatch.setattr(guard_process, "_READY_SECONDS", 0.05)
    with pytest.raises(GuardUnavailable, match="watching"):
        start_guard(tmp_path, deadline=1, networks=())
