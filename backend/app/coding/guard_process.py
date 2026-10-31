"""A separate process that stops the coding containers when whatever started them is gone.

Cleanup inside the process it protects cannot be relied on. Under PowerShell a Ctrl-C reaches Python, which
enters its `finally`, and PowerShell then terminates it part-way through (measured). A kill or a crash gives
no warning at all. Until the next backend start the writer would carry on, and its gateway would keep
spending. So this runs beside the backend, outside its process tree and its console, and acts when the
process it watches ends, or when that process outlives its deadline.

It only stops things. It never restores a workspace and never deletes a folder: what an attempt left, its
ledger above all, is for `recover_attempts` at the next start, which charges the attempt and restores or
quarantines it. The order is `OpenCodeSession.close()`: the writer first, confirmed by a separate read (a
`docker rm` exit code proves nothing), then the edges, which also ends any model request, then the networks.

If a stop cannot be confirmed the result is not clean, whatever else worked. The networks and every file
stay, and a marker says why, so nothing starts on top of it until a confirmed stop resolves it.

Run it as a module: `app/coding/types.py` would shadow the standard library's for a script in this folder.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import signal
import subprocess
import sys
import time
from collections.abc import Callable, Iterable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from app.coding.container_stop import ContainerTerminator
from app.coding.launcher import EDGE_LABEL
from app.coding.opencode_container import CONTAINER_LABEL
from app.coding.types import Termination
from app.sandbox.docker_cli import CONTAINER_LABEL as SANDBOX_LABEL
from app.sandbox.docker_cli import DockerCli
from app.sandbox.process import SubprocessRunner

READY_FILE = "guard.ready"
REPORT_FILE = "guard-report.json"
QUARANTINE_FILE = "quarantine.json"
RESOLVED_FILE = "quarantine.resolved.json"
_BACKEND = Path(__file__).resolve().parents[2]
_READY_SECONDS = 30.0
_KILL_WAIT_SECONDS = 10.0
_SECRET_NAME = re.compile(r"KEY|TOKEN|PASSWORD|SECRET", re.IGNORECASE)
_DETACHED_PROCESS, _NEW_PROCESS_GROUP, _BREAKAWAY_FROM_JOB = 0x00000008, 0x00000200, 0x01000000
_SYNCHRONIZE, _WAIT_TIMEOUT = 0x00100000, 258


class GuardUnavailable(Exception):
    """The guard did not start, so nothing that needs it may start either."""


def stop_everything(cli: DockerCli, networks: Iterable[str], *, terminator: Callable[[DockerCli, str], Any] = ContainerTerminator) -> dict[str, Any]:
    """Stop what is running. `clean` only when every writer is confirmed gone and nothing is left."""
    outcome: dict[str, Any] = {"status": "quarantined", "reason": "", "writers": [], "edges": [], "networks": []}
    try:
        writers = cli.labelled_containers(CONTAINER_LABEL)
    except Exception as exc:  # whatever went wrong, it says nothing about what is running
        outcome["reason"] = f"Docker could not say what is running ({type(exc).__name__})"
        return outcome
    for container_id in writers:
        result: Termination = _stop(terminator, cli, container_id)
        outcome["writers"].append({"container": container_id, "confirmed": result.confirmed, "evidence": result.evidence[-500:]})
    for label in (EDGE_LABEL, SANDBOX_LABEL):  # a gateway goes even if a writer would not: no more model requests
        for container_id in _listed(cli, label):
            outcome["edges"].append({"container": container_id, "removed": cli.remove(container_id)})
    if not all(w["confirmed"] for w in outcome["writers"]):
        outcome["reason"] = "a coding container could not be confirmed stopped"
        return outcome
    for name in networks:
        outcome["networks"].append({"network": name, "removed": _remove_network(cli, name)})
    left = [c for label in (CONTAINER_LABEL, EDGE_LABEL) for c in _listed(cli, label)]
    if left:
        outcome["reason"] = f"{len(left)} containers are still listed after the stop"
        return outcome
    outcome["status"] = "clean"
    return outcome


def _stop(terminator: Callable[[DockerCli, str], Any], cli: DockerCli, container_id: str) -> Termination:
    try:
        return terminator(cli, container_id).terminate()
    except Exception as exc:  # a stop that failed is an unconfirmed stop, whatever the reason
        return Termination(False, f"the stop raised {type(exc).__name__}")


def _listed(cli: DockerCli, label: str) -> list[str]:
    try:
        return cli.labelled_containers(label)
    except Exception:
        return []


def _remove_network(cli: DockerCli, name: str) -> bool:
    try:
        return cli.remove_network(name)
    except Exception:
        return False


def record(state: Path, trigger: str, outcome: dict[str, Any]) -> None:
    """Keep what happened. An unconfirmed stop leaves a marker; a confirmed one resolves it."""
    report = {"trigger": trigger, "finished_at": datetime.now(UTC).isoformat(), **outcome}
    text = json.dumps(report, indent=1)
    (state / REPORT_FILE).write_text(text, encoding="utf-8")
    marker = state / QUARANTINE_FILE
    if outcome["status"] != "clean":
        marker.write_text(text, encoding="utf-8")
    elif marker.exists():
        marker.replace(state / RESOLVED_FILE)


def wait_for_exit(pid: int, seconds: float) -> bool:
    """True when the process has ended, False when it is still running after `seconds`."""
    if sys.platform == "win32":
        import ctypes

        kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel.OpenProcess.restype = ctypes.c_void_p
        kernel.WaitForSingleObject.argtypes = (ctypes.c_void_p, ctypes.c_uint32)
        kernel.CloseHandle.argtypes = (ctypes.c_void_p,)
        handle = kernel.OpenProcess(_SYNCHRONIZE, False, pid)
        if not handle:
            return True
        try:
            return bool(kernel.WaitForSingleObject(handle, int(seconds * 1000)) != _WAIT_TIMEOUT)
        finally:
            kernel.CloseHandle(handle)
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        try:
            os.kill(pid, 0)
        except OSError:
            return True
        time.sleep(0.5)
    return False


def _watch(pid: int, deadline: float) -> str:
    if wait_for_exit(pid, deadline):
        return "the watched process ended"
    try:
        os.kill(pid, signal.SIGTERM)  # on Windows this is TerminateProcess
    except OSError:
        pass
    wait_for_exit(pid, _KILL_WAIT_SECONDS)
    return f"the {deadline:.0f} s deadline passed and the watched process was killed"


def keep_awake() -> None:
    """Ask Windows not to enter standby, which froze a 109 s run for 46 minutes. Ends with this process; no setting changes."""
    if sys.platform == "win32":
        import ctypes

        continuous, system, display = 0x80000000, 0x00000001, 0x00000002
        ctypes.WinDLL("kernel32").SetThreadExecutionState(continuous | system | display)


def on_mains_power() -> bool | None:
    """True or False on a laptop, None where it cannot be told."""
    if sys.platform != "win32":
        return None
    import ctypes

    class PowerStatus(ctypes.Structure):
        _fields_ = (("line", ctypes.c_ubyte), ("flag", ctypes.c_ubyte), ("percent", ctypes.c_ubyte), ("saver", ctypes.c_ubyte), ("life", ctypes.c_ulong), ("full", ctypes.c_ulong))

    status = PowerStatus()
    if not ctypes.WinDLL("kernel32").GetSystemPowerStatus(ctypes.byref(status)) or status.line == 255:
        return None
    return status.line == 1


def clean_environment(environ: dict[str, str] | os._Environ[str]) -> dict[str, str]:
    """The guard is given no credential, and it needs none: Docker's own login is on disk."""
    return {name: value for name, value in environ.items() if not _SECRET_NAME.search(name)}


def _spawn_detached(arguments: list[str], state: Path) -> None:
    """A new process with no console. The process that calls this exits at once, so the guard's parent is
    gone, and a host that kills a process tree cannot find its way to it."""
    command = [sys.executable, "-B", "-m", "app.coding.guard_process", *arguments]
    with (state / "guard.log").open("a", encoding="utf-8") as log:
        options: dict[str, Any] = {"stdin": subprocess.DEVNULL, "stdout": log, "stderr": log, "cwd": _BACKEND, "env": clean_environment(os.environ), "close_fds": True}
        if sys.platform != "win32":
            subprocess.Popen(command, start_new_session=True, **options)
            return
        try:
            subprocess.Popen(command, creationflags=_DETACHED_PROCESS | _NEW_PROCESS_GROUP | _BREAKAWAY_FROM_JOB, **options)
        except PermissionError:  # a job that does not allow breaking away
            subprocess.Popen(command, creationflags=_DETACHED_PROCESS | _NEW_PROCESS_GROUP, **options)


def start_guard(state: Path, *, deadline: float, networks: Iterable[str], docker: str = "docker") -> None:
    """Start a guard for this process and wait until it is watching. Raises GuardUnavailable."""
    state.mkdir(parents=True, exist_ok=True)
    ready = state / READY_FILE
    ready.unlink(missing_ok=True)
    arguments = ["--watch", str(os.getpid()), "--state", str(state), "--deadline", str(deadline), "--docker", docker, *[f"--network={n}" for n in networks]]
    done = subprocess.run([sys.executable, "-B", "-m", "app.coding.guard_process", "--detach", *arguments], cwd=_BACKEND, env=clean_environment(os.environ), capture_output=True, timeout=_READY_SECONDS)
    if done.returncode != 0:
        raise GuardUnavailable(f"the guard did not start (exit {done.returncode})")
    limit = time.monotonic() + _READY_SECONDS
    while not ready.is_file():
        if time.monotonic() > limit:
            raise GuardUnavailable("the guard did not report that it is watching")
        time.sleep(0.1)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Stops the coding containers when the process that started them has gone")
    parser.add_argument("--state", required=True, type=Path)
    parser.add_argument("--network", action="append", default=[])
    parser.add_argument("--docker", default="docker")
    parser.add_argument("--watch", type=int)
    parser.add_argument("--deadline", type=float, default=3600.0)
    parser.add_argument("--detach", action="store_true", help="start the guard apart from this process, and return")
    parser.add_argument("--once", action="store_true", help="stop what is running now, watching nothing")
    args = parser.parse_args(argv)
    args.state.mkdir(parents=True, exist_ok=True)
    if args.detach:
        _spawn_detached([a for a in (argv if argv is not None else sys.argv[1:]) if a != "--detach"], args.state)
        return 0
    if args.once:
        trigger = "asked to stop everything"
    elif args.watch is None:
        parser.error("--watch or --once is required")
    else:
        (args.state / READY_FILE).write_text(str(os.getpid()), encoding="utf-8")
        trigger = _watch(args.watch, args.deadline)
    record(args.state, trigger, stop_everything(DockerCli(args.docker, SubprocessRunner()), args.network))
    return 0


if __name__ == "__main__":
    sys.exit(main())
