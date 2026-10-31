"""A run's disposable workspace, and the one way its result reaches the real project (SECURITY T3, T4).

The real workspace is never mounted into the coding container and never written during a run. A run
works on a private copy under CODING_WORK_DIR. Each coding attempt changes only that copy, and the
sandbox tests that copy, read-only. Only a run that passed verification promotes its changes, through
`WorkspaceFS`, after checking that:
- the audit of the whole run is clean, and
- the files are exactly the files that were tested, and
- the real project still is what the copy was made from. Someone else's edit is never overwritten.

A promotion that fails part-way restores the real project from a checkpoint and compares the result.
A failed run just discards its copy: nothing else ever changed.
"""

from __future__ import annotations

import hashlib
import json
import shutil
from dataclasses import dataclass
from pathlib import Path

from app.coding.audit import Change, Checkpoint, FileState, Snapshot, audit, diff, preexisting_violations, take_snapshot
from app.tools.adapters.workspace_fs import PROJECT_ID_PATTERN, WorkspaceError, WorkspaceFS

_BASE_FILE = ".devpilot-base.json"  # beside the copy, never inside it
PROMOTION_CALL = "promotion"


class CandidateError(Exception):
    """The copy cannot be made or delivered. The message is safe to show to GPT and in logs."""


@dataclass(frozen=True)
class Promotion:
    changes: list[Change]


def tree_digest(snapshot: Snapshot) -> str:
    """Every path and its content. Two trees with the same digest hold the same files."""
    digest = hashlib.sha256()
    for path, state in sorted(snapshot.files.items()):
        digest.update(f"{path}\0{state.sha256}\n".encode("utf-8", "surrogateescape"))
    return digest.hexdigest()


class CandidateWorkspaces:
    """The folder holding one disposable copy per run. `fs` is a jail, like the real workspaces."""

    def __init__(self, fs: WorkspaceFS) -> None:
        self.fs = fs

    def path(self, run_id: str) -> Path:
        return self.fs.project_dir(run_id)

    def exists(self, run_id: str) -> bool:
        return self.fs.has_project(run_id)

    def open_or_create(self, run_id: str, source: Path) -> Path:
        """The run's copy, made on first use. Links are never followed, and a project that could
        configure the coding engine is refused instead of copied."""
        if self.exists(run_id):
            return self.path(run_id)
        snapshot = take_snapshot(source)
        problems = preexisting_violations(snapshot)
        if problems:
            raise CandidateError("the project cannot be given to the coding engine: " + "; ".join(problems[:5]))
        root = self.fs.create_project(run_id)
        for rel in snapshot.files:
            target = root / rel
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(source / rel, target, follow_symlinks=False)
        self._base_file(run_id).write_text(json.dumps({rel: [s.sha256, s.size] for rel, s in snapshot.files.items()}), encoding="utf-8")
        return root

    def sweep(self, keep: set[str]) -> list[str]:
        """Remove the copies of runs that are over. `keep` holds active and quarantined runs."""
        removed: list[str] = []
        for folder in self.fs.root.iterdir():
            if folder.is_dir() and PROJECT_ID_PATTERN.fullmatch(folder.name) and folder.name not in keep:
                self.discard(folder.name)
                removed.append(folder.name)
        return removed

    def discard(self, run_id: str) -> None:
        if self.exists(run_id):
            shutil.rmtree(self.path(run_id), ignore_errors=True)
        self._base_file(run_id).unlink(missing_ok=True)

    def base(self, run_id: str) -> Snapshot:
        """The real project as it was when the copy was made."""
        saved = json.loads(self._base_file(run_id).read_text(encoding="utf-8"))
        return Snapshot(files={rel: FileState(sha256=s[0], size=s[1]) for rel, s in saved.items()}, links=[], dirs=[])

    def digest(self, run_id: str) -> str:
        return tree_digest(take_snapshot(self.path(run_id)))

    def changes(self, run_id: str) -> list[Change]:
        return diff(self.base(run_id), take_snapshot(self.path(run_id)))

    def _base_file(self, run_id: str) -> Path:
        return self.fs.root / f"{run_id}{_BASE_FILE}"

    def promote(
        self,
        run_id: str,
        *,
        project_id: str,
        workspaces: WorkspaceFS,
        checkpoints_dir: Path,
        verified_digest: str,
        max_file_bytes: int,
        max_files: int,
        secrets: list[str],
    ) -> Promotion:
        """Copy a verified run's changes into the real project. Raises CandidateError and leaves the
        project exactly as it was, unless the restore itself fails, which is reported in the error."""
        source = self.path(run_id)
        after = take_snapshot(source)
        if tree_digest(after) != verified_digest:
            raise CandidateError("the files changed after they were verified, so they were not delivered")
        base = self.base(run_id)
        real_root = workspaces.project_dir(project_id)
        current = take_snapshot(real_root)
        if current.files != base.files or current.links:
            raise CandidateError("the project changed during the run, so nothing was overwritten")
        report = audit(source, base, after, max_file_bytes=max_file_bytes, max_files=max_files, secrets=secrets)
        if not report.clean:
            raise CandidateError("the audit refused the delivery: " + "; ".join(report.violations[:5]))
        checkpoint = Checkpoint.create(checkpoints_dir, run_id, PROMOTION_CALL, real_root, current)
        try:
            for change in report.changes:
                if change.kind == "deleted":
                    workspaces.delete(project_id, change.path)
                else:
                    workspaces.write_bytes(project_id, change.path, (source / change.path).read_bytes())
            if tree_digest(take_snapshot(real_root)) != verified_digest:
                raise CandidateError("the project does not match the verified files after copying")
        except (OSError, WorkspaceError, CandidateError) as exc:
            checkpoint.restore(real_root)
            left = checkpoint.differences(real_root)
            state = "and the project was restored" if not left else f"and the restored project differs at {len(left)} paths"
            raise CandidateError(f"delivering the files failed ({type(exc).__name__}) {state}") from exc
        checkpoint.discard()
        return Promotion(report.changes)
