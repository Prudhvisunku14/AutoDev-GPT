from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

from app.tools.adapters.workspace_fs import (
    PathRejected,
    WorkspaceFS,
    WorkspaceLimitExceeded,
    WorkspaceNotFound,
)

PID = "0123456789abcdef0123456789abcdef"


@pytest.fixture
def fs(tmp_path: Path) -> WorkspaceFS:
    workspace = WorkspaceFS(tmp_path / "workspaces", max_file_bytes=1_000, max_files=5)
    workspace.create_project(PID)
    return workspace


def _make_junction(target: Path, link: Path) -> None:
    """Create an NTFS junction (no admin rights needed), or skip off Windows."""
    if sys.platform != "win32":
        pytest.skip("junctions are Windows-only")
    import _winapi

    _winapi.CreateJunction(str(target), str(link))


def _make_symlink(target: Path, link: Path, *, is_dir: bool) -> None:
    try:
        os.symlink(target, link, target_is_directory=is_dir)
    except OSError as exc:  # Windows needs admin or Developer Mode
        pytest.skip(f"cannot create symlinks here: {exc}")


def test_write_then_read_round_trip(fs: WorkspaceFS) -> None:
    fs.write_text(PID, "app/main.py", "print('hi')\n")
    assert fs.read_text(PID, "app/main.py") == "print('hi')\n"
    assert fs.read_text(PID, "app\\main.py") == "print('hi')\n"  # Windows separators
    assert fs.read_text(PID, "./app/./main.py") == "print('hi')\n"


def test_overwrite_is_allowed(fs: WorkspaceFS) -> None:
    fs.write_text(PID, "a.txt", "one")
    fs.write_text(PID, "a.txt", "two")
    assert fs.read_text(PID, "a.txt") == "two"


def test_list_files_returns_sorted_posix_paths(fs: WorkspaceFS) -> None:
    for path in ("b.txt", "src/z.py", "src/a.py"):
        fs.write_text(PID, path, "x")
    assert fs.list_files(PID) == ["b.txt", "src/a.py", "src/z.py"]


@pytest.mark.parametrize(
    ("path", "reason"),
    [
        ("../escape.txt", "'..'"),
        ("a/../../escape.txt", "'..'"),
        ("a/..\\..\\escape.txt", "'..'"),
        ("/etc/passwd", "absolute"),
        ("\\Windows\\win.ini", "absolute"),
        ("C:\\Windows\\win.ini", "drive letters"),
        ("C:relative.txt", "drive letters"),
        ("\\\\server\\share\\x", "UNC"),
        ("//server/share/x", "UNC"),
        ("file.txt:hidden_stream", "alternate data streams"),
        ("", "empty"),
        ("   ", "empty"),
        ("./.", "empty"),
        ("nul\x00byte", "forbidden character"),
        ("what?.txt", "forbidden character"),
        ("a<b", "forbidden character"),
        ("CON", "reserved"),
        ("sub/nul.txt", "reserved"),
        ("com1.log", "reserved"),
        ("trailing.", "dot or space"),
        ("trailing ", "dot or space"),
        ("a" * 1025, "too long"),
    ],
)
def test_unsafe_paths_are_rejected(fs: WorkspaceFS, path: str, reason: str) -> None:
    with pytest.raises(PathRejected, match=reason):
        fs.resolve(PID, path)
    with pytest.raises(PathRejected):
        fs.write_text(PID, path, "x")


@pytest.mark.parametrize("project_id", ["../x", "ABCDEF0123456789abcdef0123456789", "short", "", "0" * 33])
def test_invalid_project_ids_are_rejected(fs: WorkspaceFS, project_id: str) -> None:
    with pytest.raises(PathRejected, match="invalid project id"):
        fs.resolve(project_id, "a.txt")


def test_unknown_workspace_is_not_found(fs: WorkspaceFS) -> None:
    with pytest.raises(WorkspaceNotFound):
        fs.list_files("f" * 32)


def test_creating_an_existing_workspace_fails(fs: WorkspaceFS) -> None:
    with pytest.raises(PathRejected, match="already exists"):
        fs.create_project(PID)


def test_file_size_limit_applies_to_writes_and_reads(fs: WorkspaceFS) -> None:
    with pytest.raises(WorkspaceLimitExceeded):
        fs.write_text(PID, "big.txt", "x" * 1_001)
    # A file that grew past the limit outside WorkspaceFS is still refused on read.
    (fs.root / PID / "grown.txt").write_bytes(b"x" * 2_000)
    with pytest.raises(WorkspaceLimitExceeded):
        fs.read_text(PID, "grown.txt")


def test_file_count_limit_blocks_new_files_but_allows_overwrites(fs: WorkspaceFS) -> None:
    for i in range(5):
        fs.write_text(PID, f"f{i}.txt", "x")
    with pytest.raises(WorkspaceLimitExceeded):
        fs.write_text(PID, "f5.txt", "x")
    fs.write_text(PID, "f0.txt", "updated")


def test_reading_missing_file_or_directory(fs: WorkspaceFS) -> None:
    fs.write_text(PID, "dir/file.txt", "x")
    with pytest.raises(WorkspaceNotFound):
        fs.read_text(PID, "missing.txt")
    with pytest.raises(WorkspaceNotFound):
        fs.read_text(PID, "dir")
    with pytest.raises(PathRejected, match="directory"):
        fs.write_text(PID, "dir", "x")


def test_non_utf8_file_is_rejected(fs: WorkspaceFS) -> None:
    (fs.root / PID / "bin.dat").write_bytes(b"\xff\xfe\x00")
    with pytest.raises(PathRejected, match="UTF-8"):
        fs.read_text(PID, "bin.dat")


def test_junction_pointing_outside_is_rejected(fs: WorkspaceFS, tmp_path: Path) -> None:
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "secret.txt").write_text("host secret", encoding="utf-8")
    _make_junction(outside, fs.root / PID / "link")

    with pytest.raises(PathRejected, match="symlink or junction"):
        fs.read_text(PID, "link/secret.txt")
    with pytest.raises(PathRejected, match="symlink or junction"):
        fs.write_text(PID, "link/new.txt", "x")
    assert not (outside / "new.txt").exists()
    assert fs.list_files(PID) == []  # junctions are neither followed nor listed


def test_junction_pointing_inside_is_also_rejected(fs: WorkspaceFS) -> None:
    fs.write_text(PID, "real/file.txt", "x")
    _make_junction(fs.root / PID / "real", fs.root / PID / "alias")
    with pytest.raises(PathRejected):
        fs.read_text(PID, "alias/file.txt")


def test_workspace_that_is_a_junction_is_rejected(tmp_path: Path) -> None:
    fs = WorkspaceFS(tmp_path / "ws", max_file_bytes=100, max_files=5)
    outside = tmp_path / "outside"
    outside.mkdir()
    _make_junction(outside, fs.root / PID)
    with pytest.raises(PathRejected, match="workspace must not be"):
        fs.list_files(PID)


def test_symlinked_file_is_rejected(fs: WorkspaceFS, tmp_path: Path) -> None:
    target = tmp_path / "host.txt"
    target.write_text("host", encoding="utf-8")
    _make_symlink(target, fs.root / PID / "link.txt", is_dir=False)
    with pytest.raises(PathRejected, match="symlink or junction"):
        fs.read_text(PID, "link.txt")
    assert fs.list_files(PID) == []


def test_symlinked_root_is_rejected(tmp_path: Path) -> None:
    real = tmp_path / "real"
    real.mkdir()
    _make_symlink(real, tmp_path / "linked", is_dir=True)
    with pytest.raises(PathRejected, match="workspaces root"):
        WorkspaceFS(tmp_path / "linked", max_file_bytes=10, max_files=1)
