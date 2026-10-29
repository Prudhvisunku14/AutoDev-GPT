"""WorkspaceFS: the only file access to generated projects (SECURITY T4).

Every path is jailed inside `workspaces/<project_id>/`. Rejected:
- `..` components, absolute paths, drive letters and drive-relative paths, UNC paths
- NTFS alternate data streams (any `:`), NUL and other characters Windows forbids
- Windows reserved device names (CON, NUL, COM1, ...) and names ending in a dot or space
- any path component that is a symlink or junction, even one pointing inside the jail

Limits on file size and file count apply to reads and writes.
"""

from __future__ import annotations

import os
import re
from pathlib import Path

PROJECT_ID_PATTERN = re.compile(r"^[0-9a-f]{32}$")
MAX_PATH_LENGTH = 1024

_FORBIDDEN_CHARS = frozenset('<>"|?*') | {chr(c) for c in range(32)}
_RESERVED_NAMES = frozenset(
    {"CON", "PRN", "AUX", "NUL", "CONIN$", "CONOUT$"}
    | {f"COM{i}" for i in range(10)}
    | {f"LPT{i}" for i in range(10)}
)


class WorkspaceError(Exception):
    """Base class. The message is safe to return to API clients and to GPT."""


class PathRejected(WorkspaceError):
    pass


class WorkspaceLimitExceeded(WorkspaceError):
    pass


class WorkspaceNotFound(WorkspaceError):
    pass


class WorkspaceFS:
    def __init__(self, root: Path, *, max_file_bytes: int, max_files: int) -> None:
        root.mkdir(parents=True, exist_ok=True)
        if _is_link(root):
            raise PathRejected("workspaces root must not be a symlink or junction")
        self._root = root.resolve(strict=True)
        self._max_file_bytes = max_file_bytes
        self._max_files = max_files

    @property
    def root(self) -> Path:
        return self._root

    def create_project(self, project_id: str) -> Path:
        path = self._project_path(project_id)
        try:
            path.mkdir()
        except FileExistsError as exc:
            raise PathRejected("workspace already exists") from exc
        return path

    def project_dir(self, project_id: str) -> Path:
        path = self._project_path(project_id)
        if _is_link(path):
            raise PathRejected("workspace must not be a symlink or junction")
        if not path.is_dir():
            raise WorkspaceNotFound("workspace does not exist")
        return path

    def resolve(self, project_id: str, rel_path: str) -> Path:
        """Return the absolute path for `rel_path`, or raise PathRejected."""
        base = self.project_dir(project_id)
        current = base
        for part in _validated_parts(rel_path):
            current = current / part
            if _is_link(current):
                raise PathRejected("path goes through a symlink or junction")
        # Defense in depth: the fully resolved path must still be inside the workspace.
        if not current.resolve(strict=False).is_relative_to(base.resolve(strict=True)):
            raise PathRejected("path escapes the workspace")
        return current

    def write_text(self, project_id: str, rel_path: str, content: str) -> Path:
        data = content.encode("utf-8")
        if len(data) > self._max_file_bytes:
            raise WorkspaceLimitExceeded(f"file exceeds {self._max_file_bytes} bytes")
        target = self.resolve(project_id, rel_path)
        if target.is_dir():
            raise PathRejected("path is a directory")
        if not target.exists() and len(self.list_files(project_id)) >= self._max_files:
            raise WorkspaceLimitExceeded(f"workspace already holds {self._max_files} files")
        target.parent.mkdir(parents=True, exist_ok=True)
        # Re-check after creating parents, in case a link appeared meanwhile.
        target = self.resolve(project_id, rel_path)
        target.write_bytes(data)
        return target

    def read_text(self, project_id: str, rel_path: str) -> str:
        target = self.resolve(project_id, rel_path)
        if not target.is_file():
            raise WorkspaceNotFound("file does not exist")
        if target.stat().st_size > self._max_file_bytes:
            raise WorkspaceLimitExceeded(f"file exceeds {self._max_file_bytes} bytes")
        try:
            return target.read_text(encoding="utf-8")
        except UnicodeDecodeError as exc:
            raise PathRejected("file is not UTF-8 text") from exc

    def list_files(self, project_id: str) -> list[str]:
        """Relative POSIX paths of regular files. Links are never followed or listed."""
        base = self.project_dir(project_id)
        found: list[str] = []
        for dirpath, dirnames, filenames in os.walk(base, followlinks=False):
            here = Path(dirpath)
            # os.walk does not treat junctions as links, so prune them explicitly.
            dirnames[:] = sorted(d for d in dirnames if not _is_link(here / d))
            for name in sorted(filenames):
                file_path = here / name
                if not _is_link(file_path):
                    found.append(file_path.relative_to(base).as_posix())
        return found

    def _project_path(self, project_id: str) -> Path:
        if not isinstance(project_id, str) or not PROJECT_ID_PATTERN.fullmatch(project_id):
            raise PathRejected("invalid project id")
        return self._root / project_id


def _validated_parts(rel_path: str) -> list[str]:
    if not isinstance(rel_path, str) or not rel_path.strip():
        raise PathRejected("path is empty")
    if len(rel_path) > MAX_PATH_LENGTH:
        raise PathRejected("path is too long")
    normalized = rel_path.replace("\\", "/")
    if normalized.startswith("/"):
        raise PathRejected("absolute and UNC paths are not allowed")
    if ":" in normalized:
        raise PathRejected("drive letters and alternate data streams are not allowed")
    parts = [p for p in normalized.split("/") if p not in ("", ".")]
    if not parts:
        raise PathRejected("path is empty")
    for part in parts:
        if part == "..":
            raise PathRejected("'..' is not allowed")
        if any(ch in _FORBIDDEN_CHARS for ch in part):
            raise PathRejected("path contains a forbidden character")
        if part.endswith((".", " ")):
            raise PathRejected("names may not end with a dot or space")
        if part.split(".")[0].upper() in _RESERVED_NAMES:
            raise PathRejected("reserved device names are not allowed")
    return parts


def _is_link(path: Path) -> bool:
    return path.is_symlink() or path.is_junction()
