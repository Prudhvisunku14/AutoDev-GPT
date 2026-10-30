"""Workspace snapshots, the change audit, and checkpoint restore (real files in a temp folder)."""

from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

from app.coding.audit import Checkpoint, FileState, Snapshot, audit, diff, preexisting_violations, take_snapshot
from tests.helpers import FAKE_OPENAI_KEY


@pytest.fixture
def root(tmp_path: Path) -> Path:
    workspace = tmp_path / "ws"
    (workspace / "pkg").mkdir(parents=True)
    (workspace / "calculator.py").write_text("def add(a, b):\n    return a + b\n", encoding="utf-8")
    (workspace / "pkg" / "util.py").write_text("X = 1\n", encoding="utf-8")
    (workspace / "README.md").write_text("calc\n", encoding="utf-8")
    return workspace


def _audit(root: Path, before, *, max_file_bytes: int = 10_000, max_files: int = 50, secrets=()):  # noqa: ANN001, ANN202
    return audit(root, before, take_snapshot(root), max_file_bytes=max_file_bytes, max_files=max_files, secrets=list(secrets))


def _symlink(target: Path, link: Path) -> None:
    try:
        os.symlink(target, link)
    except OSError as exc:
        pytest.skip(f"cannot create symlinks here: {exc}")


def _junction(target: Path, link: Path) -> None:
    if sys.platform != "win32":
        pytest.skip("junctions are Windows-only")
    import _winapi

    _winapi.CreateJunction(str(target), str(link))


# --- Snapshots and diffs -------------------------------------------------------------


def test_a_snapshot_records_every_file_with_hash_and_size(root: Path) -> None:
    snap = take_snapshot(root)
    assert set(snap.files) == {"calculator.py", "pkg/util.py", "README.md"}
    assert snap.files["pkg/util.py"].size == len((root / "pkg" / "util.py").read_bytes())
    assert len(snap.files["pkg/util.py"].sha256) == 64
    assert snap.dirs == ["pkg"] and snap.links == []


def test_the_diff_lists_added_modified_and_deleted_files(root: Path) -> None:
    before = take_snapshot(root)
    (root / "calculator.py").write_text("def add(a, b):\n    return b + a\n", encoding="utf-8")
    (root / "README.md").unlink()
    (root / "test_calculator.py").write_text("import unittest\n", encoding="utf-8")
    changes = {c.path: c.kind for c in diff(before, take_snapshot(root))}
    assert changes == {"calculator.py": "modified", "README.md": "deleted", "test_calculator.py": "added"}


def test_rewriting_a_file_with_identical_content_is_not_a_change(root: Path) -> None:
    before = take_snapshot(root)
    (root / "pkg" / "util.py").write_bytes((root / "pkg" / "util.py").read_bytes())
    assert diff(before, take_snapshot(root)) == []


# --- The audit -----------------------------------------------------------------------


def test_ordinary_source_changes_pass(root: Path) -> None:
    before = take_snapshot(root)
    (root / "test_calculator.py").write_text("import unittest\n", encoding="utf-8")
    report = _audit(root, before)
    assert report.clean and [c.to_dict() for c in report.changes] == [
        {"path": "test_calculator.py", "change": "added", "bytes": len((root / "test_calculator.py").read_bytes())}
    ]


@pytest.mark.parametrize(
    "path",
    ["opencode.json", ".opencode/plugin/evil.js", "sub/AGENTS.md", ".git/hooks/pre-commit", ".env", ".devpilot/junit.xml"],
)
def test_writing_a_protected_path_is_a_violation(root: Path, path: str) -> None:
    before = take_snapshot(root)
    target = root / path
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text("{}", encoding="utf-8")
    report = _audit(root, before)
    assert any(v.startswith(f"{path}: protected path") for v in report.violations), report.violations


def test_creating_a_protected_folder_is_a_violation_even_when_empty(root: Path) -> None:
    before = take_snapshot(root)
    (root / ".opencode").mkdir()
    report = _audit(root, before)
    assert ".opencode/: protected folder created" in report.violations


def test_deleting_a_protected_file_is_a_violation(root: Path) -> None:
    (root / ".gitignore").write_text("x\n", encoding="utf-8")
    (root / "AGENTS.md").write_text("notes\n", encoding="utf-8")
    before = take_snapshot(root)
    (root / "AGENTS.md").unlink()
    assert any(v.startswith("AGENTS.md: protected path (deleted)") for v in _audit(root, before).violations)


def test_a_file_over_the_size_limit_is_a_violation(root: Path) -> None:
    before = take_snapshot(root)
    (root / "big.py").write_bytes(b"x" * 2_000)
    assert any("over the 1000-byte limit" in v for v in _audit(root, before, max_file_bytes=1_000).violations)


@pytest.mark.parametrize("magic", [b"\x7fELF\x02\x01", b"MZ\x90\x00", b"\xcf\xfa\xed\xfe\x07"])
def test_native_executables_are_violations(root: Path, magic: bytes) -> None:
    before = take_snapshot(root)
    (root / "tool.bin").write_bytes(magic + b"\x00" * 64)
    assert "tool.bin: native executables are not allowed" in _audit(root, before).violations


def test_a_file_containing_a_configured_secret_is_a_violation_without_naming_it(root: Path) -> None:
    before = take_snapshot(root)
    (root / "settings.py").write_text(f"KEY = '{FAKE_OPENAI_KEY}'\n", encoding="utf-8")
    report = _audit(root, before, secrets=[FAKE_OPENAI_KEY, ""])
    assert "settings.py: contains a configured secret" in report.violations
    assert all(FAKE_OPENAI_KEY not in v for v in report.violations)


def test_too_many_files_is_a_violation(root: Path) -> None:
    before = take_snapshot(root)
    for i in range(3):
        (root / f"m{i}.py").write_text("", encoding="utf-8")
    assert any("over the 4-file limit" in v for v in _audit(root, before, max_files=4).violations)


def test_a_name_windows_cannot_hold_is_a_violation(root: Path) -> None:
    # Such names can come from the Linux container side but cannot be created here, so the
    # snapshots are built directly: the name check runs before any file is read.
    before = Snapshot(files={"CON.py": FileState("0" * 64, 1)}, links=[], dirs=[])
    after = Snapshot(files={}, links=[], dirs=[])
    report = audit(root, before, after, max_file_bytes=10_000, max_files=50, secrets=[])
    assert any(v.startswith("CON.py: invalid name") for v in report.violations), report.violations


def test_creating_a_symlink_is_a_violation(root: Path, tmp_path: Path) -> None:
    before = take_snapshot(root)
    _symlink(tmp_path / "outside.txt", root / "link.txt")
    report = _audit(root, before)
    assert "link.txt: creating a symlink or junction is not allowed" in report.violations


def test_creating_a_junction_is_a_violation_and_is_not_followed(root: Path, tmp_path: Path) -> None:
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "secret.txt").write_text("s\n", encoding="utf-8")
    before = take_snapshot(root)
    _junction(outside, root / "escape")
    after = take_snapshot(root)
    assert "escape/secret.txt" not in after.files
    report = audit(root, before, after, max_file_bytes=10_000, max_files=50, secrets=[])
    assert "escape: creating a symlink or junction is not allowed" in report.violations


def test_a_workspace_that_already_holds_engine_config_is_refused_up_front(root: Path) -> None:
    (root / "opencode.json").write_text("{}", encoding="utf-8")
    (root / ".opencode").mkdir()
    problems = preexisting_violations(take_snapshot(root))
    assert "opencode.json: this path can change the coding engine's configuration" in problems
    assert ".opencode/: this folder can change the coding engine's configuration" in problems
    assert preexisting_violations(take_snapshot(root.parent / "missing")) == []


# --- Checkpoints ---------------------------------------------------------------------


def test_restore_undoes_additions_changes_deletions_and_new_folders(root: Path, tmp_path: Path) -> None:
    before = take_snapshot(root)
    original = {p: (root / p).read_bytes() for p in before.files}
    checkpoint = Checkpoint.create(tmp_path / "cp", "run1", "call_abc", root, before)

    (root / "calculator.py").write_text("broken", encoding="utf-8")
    (root / "README.md").unlink()
    (root / "new" / "deep").mkdir(parents=True)
    (root / "new" / "deep" / "x.py").write_text("x", encoding="utf-8")
    (root / "opencode.json").write_text("{}", encoding="utf-8")

    touched = checkpoint.restore(root)
    after = take_snapshot(root)
    assert touched == 4
    assert after.files == before.files
    assert {p: (root / p).read_bytes() for p in after.files} == original
    assert not (root / "new").exists()
    assert (root / "pkg").is_dir()  # folders that existed before stay


def test_restore_removes_a_created_symlink_but_not_its_target(root: Path, tmp_path: Path) -> None:
    target = tmp_path / "keep.txt"
    target.write_text("keep\n", encoding="utf-8")
    before = take_snapshot(root)
    checkpoint = Checkpoint.create(tmp_path / "cp", "run1", "call_link", root, before)
    _symlink(target, root / "link.txt")
    checkpoint.restore(root)
    assert not (root / "link.txt").exists() and not (root / "link.txt").is_symlink()
    assert target.read_text(encoding="utf-8") == "keep\n"


def test_restore_removes_a_created_junction_but_not_its_target(root: Path, tmp_path: Path) -> None:
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "data.txt").write_text("d\n", encoding="utf-8")
    before = take_snapshot(root)
    checkpoint = Checkpoint.create(tmp_path / "cp", "run1", "call_junction", root, before)
    _junction(outside, root / "escape")
    checkpoint.restore(root)
    assert not (root / "escape").exists()
    assert (outside / "data.txt").read_text(encoding="utf-8") == "d\n"


def test_a_checkpoint_is_found_by_run_and_call_and_can_be_discarded(root: Path, tmp_path: Path) -> None:
    base = tmp_path / "cp"
    checkpoint = Checkpoint.create(base, "run1", "call_1", root, take_snapshot(root))
    found = Checkpoint.find(base, "run1", "call_1")
    assert found is not None and found.path == checkpoint.path
    assert Checkpoint.find(base, "run1", "other") is None
    checkpoint.discard()
    assert Checkpoint.find(base, "run1", "call_1") is None


def test_the_call_id_never_becomes_a_raw_path_component(tmp_path: Path) -> None:
    location = Checkpoint.location(tmp_path, "run1", "../../escape")
    assert location.parent == tmp_path / "run1"
    assert ".." not in location.name and len(location.name) == 32


def test_creating_a_checkpoint_twice_replaces_the_old_copy(root: Path, tmp_path: Path) -> None:
    base = tmp_path / "cp"
    Checkpoint.create(base, "run1", "call_1", root, take_snapshot(root))
    (root / "extra.py").write_text("e", encoding="utf-8")
    second = Checkpoint.create(base, "run1", "call_1", root, take_snapshot(root))
    (root / "extra.py").unlink()
    second.restore(root)
    assert (root / "extra.py").read_text(encoding="utf-8") == "e"
