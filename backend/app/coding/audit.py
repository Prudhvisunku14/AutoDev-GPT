"""Workspace snapshots, checkpoints and the audit of a coding task's changes (SECURITY T3, T4).

DevPilot does not trust the engine's own account of what it changed. Before a task it
records every file (hash and size) and copies it to a checkpoint. After the task it
compares the workspace with that record, checks each change against the rules below,
and on any violation, or any outcome other than COMPLETED, restores the checkpoint, so a
task's edits are kept or dropped as a whole.
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
from dataclasses import dataclass
from pathlib import Path

from app.coding.opencode_policy import is_protected
from app.tools.adapters.workspace_fs import PathRejected, check_relative_path

# First bytes of native executables (ELF, Windows PE, Mach-O). Generated code is source, not binaries.
_EXECUTABLE_MAGIC = (b"\x7fELF", b"MZ", b"\xcf\xfa\xed\xfe", b"\xce\xfa\xed\xfe", b"\xfe\xed\xfa\xcf", b"\xca\xfe\xba\xbe")
_HASH_CHUNK = 65_536


@dataclass(frozen=True)
class FileState:
    sha256: str
    size: int


@dataclass(frozen=True)
class Snapshot:
    files: dict[str, FileState]  # workspace-relative POSIX path -> state
    links: list[str]  # symlinks and junctions found (never followed)
    dirs: list[str]


@dataclass(frozen=True)
class Change:
    path: str
    kind: str  # added, modified or deleted
    size: int  # bytes after the change (0 for deleted)

    def to_dict(self) -> dict[str, object]:
        return {"path": self.path, "change": self.kind, "bytes": self.size}


@dataclass(frozen=True)
class AuditReport:
    changes: list[Change]
    violations: list[str]

    @property
    def clean(self) -> bool:
        return not self.violations


def take_snapshot(root: Path) -> Snapshot:
    files: dict[str, FileState] = {}
    links: list[str] = []
    dirs: list[str] = []
    for dirpath, dirnames, filenames in os.walk(root, followlinks=False):
        here = Path(dirpath)
        kept: list[str] = []
        for name in sorted(dirnames):
            path = here / name
            rel = path.relative_to(root).as_posix()
            if path.is_symlink() or path.is_junction():
                links.append(rel)
            else:
                kept.append(name)
                dirs.append(rel)
        dirnames[:] = kept
        for name in sorted(filenames):
            path = here / name
            rel = path.relative_to(root).as_posix()
            if path.is_symlink():
                links.append(rel)
                continue
            files[rel] = FileState(sha256=_hash(path), size=path.stat().st_size)
    return Snapshot(files=files, links=sorted(links), dirs=sorted(dirs))


def diff(before: Snapshot, after: Snapshot) -> list[Change]:
    changes: list[Change] = []
    for path in sorted(before.files.keys() | after.files.keys()):
        old, new = before.files.get(path), after.files.get(path)
        if old is None and new is not None:
            changes.append(Change(path, "added", new.size))
        elif old is not None and new is None:
            changes.append(Change(path, "deleted", 0))
        elif old is not None and new is not None and old.sha256 != new.sha256:
            changes.append(Change(path, "modified", new.size))
    return changes


def preexisting_violations(snapshot: Snapshot) -> list[str]:
    """Files that must not be in a workspace before OpenCode is pointed at it."""
    problems = [f"{p}: symlinks and junctions are not allowed" for p in snapshot.links]
    problems += [f"{p}: this path can change the coding engine's configuration" for p in snapshot.files if is_protected(p)]
    problems += [f"{p}/: this folder can change the coding engine's configuration" for p in snapshot.dirs if is_protected(p + "/x")]
    return problems


def audit(
    root: Path,
    before: Snapshot,
    after: Snapshot,
    *,
    max_file_bytes: int,
    max_files: int,
    secrets: list[str],
) -> AuditReport:
    changes = diff(before, after)
    violations: list[str] = []
    for link in sorted(set(after.links) - set(before.links)):
        violations.append(f"{link}: creating a symlink or junction is not allowed")
    for change in changes:
        path = change.path
        if is_protected(path):
            violations.append(f"{path}: protected path ({change.kind}); it could change the engine's configuration")
            continue
        try:
            check_relative_path(path)
        except PathRejected as exc:
            violations.append(f"{path}: invalid name ({exc})")
            continue
        if change.kind == "deleted":
            continue
        if change.size > max_file_bytes:
            violations.append(f"{path}: {change.size} bytes is over the {max_file_bytes}-byte limit")
            continue
        data = (root / path).read_bytes()
        if data.startswith(_EXECUTABLE_MAGIC):
            violations.append(f"{path}: native executables are not allowed")
        if any(secret.encode() in data for secret in secrets if secret):
            violations.append(f"{path}: contains a configured secret")
    for directory in sorted(set(after.dirs) - set(before.dirs)):
        if is_protected(directory + "/x"):
            violations.append(f"{directory}/: protected folder created")
    if len(after.files) > max_files:
        violations.append(f"the workspace holds {len(after.files)} files, over the {max_files}-file limit")
    return AuditReport(changes=changes, violations=violations)


class Checkpoint:
    """A copy of the workspace taken before a coding task, kept on disk so a crash can be undone."""

    def __init__(self, path: Path) -> None:
        self._path = path

    @property
    def path(self) -> Path:
        return self._path

    @staticmethod
    def location(base: Path, run_id: str, call_id: str) -> Path:
        # Hash the call id: it comes from OpenAI and is not trusted as a path component.
        return base / run_id / hashlib.sha256(call_id.encode()).hexdigest()[:32]

    @classmethod
    def create(cls, base: Path, run_id: str, call_id: str, root: Path, snapshot: Snapshot) -> Checkpoint:
        path = cls.location(base, run_id, call_id)
        if path.exists():
            shutil.rmtree(path)
        (path / "files").mkdir(parents=True)
        for rel in snapshot.files:
            target = path / "files" / rel
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(root / rel, target, follow_symlinks=False)
        manifest = {"files": {rel: state.sha256 for rel, state in snapshot.files.items()}, "dirs": snapshot.dirs}
        (path / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
        return cls(path)

    @classmethod
    def find(cls, base: Path, run_id: str, call_id: str) -> Checkpoint | None:
        path = cls.location(base, run_id, call_id)
        return cls(path) if (path / "manifest.json").is_file() else None

    def restore(self, root: Path) -> int:
        """Put the workspace back exactly as it was. Returns the number of paths changed."""
        manifest = json.loads((self._path / "manifest.json").read_text(encoding="utf-8"))
        saved: dict[str, str] = manifest["files"]
        saved_dirs = set(manifest["dirs"])
        current = take_snapshot(root)
        touched = 0
        for link in current.links:
            _remove_link(root / link)
            touched += 1
        for rel, state in current.files.items():
            if rel not in saved:
                (root / rel).unlink()
                touched += 1
            elif state.sha256 != saved[rel]:
                shutil.copyfile(self._path / "files" / rel, root / rel)
                touched += 1
        for rel in saved.keys() - current.files.keys():
            target = root / rel
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(self._path / "files" / rel, target)
            touched += 1
        # Remove folders the task created, deepest first; folders that existed before stay.
        for rel in sorted(set(take_snapshot(root).dirs) - saved_dirs, key=lambda d: d.count("/"), reverse=True):
            folder = root / rel
            if folder.is_dir() and not any(folder.iterdir()):
                folder.rmdir()
        return touched

    def differences(self, root: Path) -> list[str]:
        """Where the workspace differs from this checkpoint. Empty means an exact match, by content."""
        saved: dict[str, str] = json.loads((self._path / "manifest.json").read_text(encoding="utf-8"))["files"]
        current = take_snapshot(root)
        found = [f"{link}: a link" for link in current.links]
        found += [f"{rel}: missing" for rel in sorted(saved.keys() - current.files.keys())]
        found += [f"{rel}: not in the checkpoint" for rel in sorted(current.files.keys() - saved.keys())]
        found += [f"{rel}: different content" for rel, state in current.files.items() if rel in saved and state.sha256 != saved[rel]]
        return found

    def discard(self) -> None:
        shutil.rmtree(self._path, ignore_errors=True)


def _remove_link(path: Path) -> None:
    # Remove the link itself; never the target it points to.
    if path.is_junction():
        os.rmdir(path)
    else:
        path.unlink()


def _hash(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while chunk := handle.read(_HASH_CHUNK):
            digest.update(chunk)
    return digest.hexdigest()
