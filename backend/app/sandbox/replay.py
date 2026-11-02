"""ReplaySandbox: recorded test runs for the offline demonstration. **It never starts a process or a container.**

A recording holds what the approved command printed for one exact set of files. A run is answered only when the
command and every file byte match a recording, so the verdict belongs to the files that were really produced.
Anything else, including any code a real model writes, gets UNAVAILABLE ("needs the Docker sandbox"), and so
never PASSED. The report names it, so a replayed verdict cannot read as a Docker run.

The recordings come from `python -m app.demo.record`, which runs the sandbox's own command on the host over
files written in this repository, never over generated code; a unit test repeats that check on every run.
"""

from __future__ import annotations

import hashlib
import json
import threading
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from app.sandbox.spec import ExecResult, ExecSpec, ExecStatus

RECORDINGS_FILE = Path(__file__).resolve().parents[1] / "demo" / "recordings.json"
NOT_RECORDED = "no recorded result exists for these exact files; running them needs the Docker sandbox"


def file_digest(data: bytes) -> str:
    """Line endings are made LF first, so a checkout on Windows matches a recording made anywhere."""
    return hashlib.sha256(data.replace(b"\r\n", b"\n")).hexdigest()


def recording_key(command: str, argv: tuple[str, ...] | list[str], files: Mapping[str, bytes]) -> str:
    body = {"command": command, "argv": list(argv), "files": sorted((rel, file_digest(data)) for rel, data in files.items())}
    return hashlib.sha256(json.dumps(body, sort_keys=True).encode("utf-8")).hexdigest()


def tree_files(root: Path) -> dict[str, bytes]:
    """Every regular file under `root`. A link makes the tree unrecognisable: it is never followed."""
    files: dict[str, bytes] = {}
    for path in sorted(root.rglob("*")):
        if path.is_symlink():
            files[path.relative_to(root).as_posix()] = b"\0link"
        elif path.is_file():
            files[path.relative_to(root).as_posix()] = path.read_bytes()
    return files


@dataclass(frozen=True)
class Recording:
    name: str
    status: str
    exit_code: int | None
    stdout: str
    stderr: str


class ReplaySandbox:
    provenance = "replayed"

    def __init__(self, recordings: Mapping[str, Recording]) -> None:
        self._recordings = dict(recordings)
        self.specs: list[ExecSpec] = []
        self.replayed: list[str] = []

    @classmethod
    def load(cls, path: Path = RECORDINGS_FILE) -> ReplaySandbox:
        data: dict[str, Any] = json.loads(path.read_text(encoding="utf-8"))
        return cls({r["key"]: Recording(r["name"], r["status"], r["exit_code"], r["stdout"], r["stderr"]) for r in data["recordings"]})

    def run(self, spec: ExecSpec, *, cancel: threading.Event | None = None) -> ExecResult:
        self.specs.append(spec)
        if cancel is not None and cancel.is_set():
            return ExecResult(status=ExecStatus.CANCELLED, command=spec.command, container=spec.container_name)
        found = self._recordings.get(recording_key(spec.command, spec.argv, tree_files(spec.workspace_dir)))
        if found is None:
            return ExecResult.unavailable(spec, NOT_RECORDED)
        self.replayed.append(found.name)
        return ExecResult(
            status=ExecStatus(found.status), command=spec.command, container=f"replay:{spec.container_name}", exit_code=found.exit_code,
            stdout=found.stdout, stderr=found.stderr, cleanup="not_needed",
        )  # fmt: skip

    def sweep_orphans(self) -> int:
        return 0
