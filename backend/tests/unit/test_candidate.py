"""The disposable copy and the one way into the real project. Real files, no Docker."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from app.coding.audit import take_snapshot
from app.coding.candidate import CandidateError, CandidateWorkspaces, tree_digest
from app.selection.selector import NoProvider, ProviderOption, ProviderSelector
from app.tools.adapters.workspace_fs import PathRejected, WorkspaceFS, WorkspaceNotFound

PROJECT = "a" * 32
RUN = "b" * 32
LIMITS: dict[str, Any] = {"max_file_bytes": 10_000, "max_files": 100, "secrets": ["the-configured-secret"]}


class Scene:
    def __init__(self, tmp_path: Path) -> None:
        self.real = WorkspaceFS(tmp_path / "workspaces", max_file_bytes=10_000, max_files=100)
        self.real.create_project(PROJECT)
        self.candidates = CandidateWorkspaces(WorkspaceFS(tmp_path / "work", max_file_bytes=10_000, max_files=100))
        self.checkpoints = tmp_path / "checkpoints"

    @property
    def project(self) -> Path:
        return self.real.project_dir(PROJECT)

    def copy(self) -> Path:
        return self.candidates.open_or_create(RUN, self.project)

    def promote(self, digest: str | None = None) -> Any:
        return self.candidates.promote(
            RUN, project_id=PROJECT, workspaces=self.real, checkpoints_dir=self.checkpoints,
            verified_digest=digest or self.candidates.digest(RUN), **LIMITS,
        )  # fmt: skip


@pytest.fixture
def scene(tmp_path: Path) -> Scene:
    return Scene(tmp_path)


def test_the_copy_holds_the_project_and_is_a_different_folder(scene: Scene) -> None:
    scene.real.write_text(PROJECT, "pkg/old.py", "x = 1\n")
    root = scene.copy()
    assert (root / "pkg/old.py").read_text(encoding="utf-8") == "x = 1\n" and root != scene.project


def test_changing_the_copy_never_changes_the_project(scene: Scene) -> None:
    scene.real.write_text(PROJECT, "old.py", "x = 1\n")
    before = take_snapshot(scene.project)
    root = scene.copy()
    (root / "old.py").write_text("changed\n", encoding="utf-8")
    (root / "new.py").write_text("added\n", encoding="utf-8")
    assert take_snapshot(scene.project) == before


def test_a_second_call_reuses_the_copy_so_tasks_accumulate(scene: Scene) -> None:
    root = scene.copy()
    (root / "first.py").write_text("x\n", encoding="utf-8")
    assert (scene.copy() / "first.py").is_file()


@pytest.mark.parametrize("name", ["opencode.json", "AGENTS.md", ".opencode/plugin.js", ".git/config", ".env"])
def test_a_project_that_could_configure_the_engine_is_refused(scene: Scene, name: str) -> None:
    target = scene.project / name
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text("{}", encoding="utf-8")
    with pytest.raises(CandidateError, match="cannot be given"):
        scene.copy()
    assert not scene.candidates.exists(RUN)


def test_promotion_delivers_additions_changes_and_deletions(scene: Scene) -> None:
    scene.real.write_text(PROJECT, "keep.py", "same\n")
    scene.real.write_text(PROJECT, "edit.py", "old\n")
    scene.real.write_text(PROJECT, "gone.py", "bye\n")
    root = scene.copy()
    (root / "edit.py").write_text("new\n", encoding="utf-8")
    (root / "gone.py").unlink()
    (root / "pkg").mkdir()
    (root / "pkg/added.py").write_text("added\n", encoding="utf-8")
    promotion = scene.promote()
    assert {(c.path, c.kind) for c in promotion.changes} == {("edit.py", "modified"), ("gone.py", "deleted"), ("pkg/added.py", "added")}
    assert tree_digest(take_snapshot(scene.project)) == tree_digest(take_snapshot(root))


def test_files_changed_after_verification_are_not_delivered(scene: Scene) -> None:
    root = scene.copy()
    (root / "a.py").write_text("verified\n", encoding="utf-8")
    digest = scene.candidates.digest(RUN)
    (root / "a.py").write_text("swapped in later\n", encoding="utf-8")
    with pytest.raises(CandidateError, match="after they were verified"):
        scene.promote(digest)
    assert not (scene.project / "a.py").exists()


def test_a_project_changed_during_the_run_is_never_overwritten(scene: Scene) -> None:
    root = scene.copy()
    (root / "a.py").write_text("generated\n", encoding="utf-8")
    scene.real.write_text(PROJECT, "a.py", "the user's own edit\n")
    with pytest.raises(CandidateError, match="changed during the run"):
        scene.promote()
    assert (scene.project / "a.py").read_text(encoding="utf-8") == "the user's own edit\n"


def test_the_audit_runs_again_at_delivery(scene: Scene) -> None:
    root = scene.copy()
    (root / "opencode.json").write_text("{}", encoding="utf-8")
    with pytest.raises(CandidateError, match="audit refused"):
        scene.promote()
    assert not (scene.project / "opencode.json").exists()


def test_a_configured_secret_is_not_delivered(scene: Scene) -> None:
    root = scene.copy()
    (root / "a.py").write_text("key = 'the-configured-secret'\n", encoding="utf-8")
    with pytest.raises(CandidateError, match="audit refused"):
        scene.promote()


def test_a_delivery_that_fails_part_way_restores_the_project(scene: Scene, monkeypatch: pytest.MonkeyPatch) -> None:
    scene.real.write_text(PROJECT, "old.py", "original\n")
    before = take_snapshot(scene.project)
    root = scene.copy()
    (root / "old.py").write_text("changed\n", encoding="utf-8")
    (root / "z_last.py").write_text("x\n", encoding="utf-8")
    real_write = scene.real.write_bytes

    def fail_on_the_last(project_id: str, rel_path: str, data: bytes) -> Path:
        if rel_path == "z_last.py":
            raise OSError("disk full")
        return real_write(project_id, rel_path, data)

    monkeypatch.setattr(scene.real, "write_bytes", fail_on_the_last)
    with pytest.raises(CandidateError, match="restored"):
        scene.promote()
    assert take_snapshot(scene.project).files == before.files


def test_discarding_leaves_nothing_behind(scene: Scene) -> None:
    scene.copy()
    scene.candidates.discard(RUN)
    assert not scene.candidates.exists(RUN) and list(scene.candidates.fs.root.iterdir()) == []


def test_the_sweep_keeps_active_and_quarantined_runs(scene: Scene) -> None:
    other = "c" * 32
    scene.copy()
    scene.candidates.open_or_create(other, scene.project)
    (scene.candidates.fs.root / "attempts").mkdir()
    assert scene.candidates.sweep({RUN}) == [other]
    assert scene.candidates.exists(RUN) and not scene.candidates.exists(other) and (scene.candidates.fs.root / "attempts").is_dir()


def test_the_copy_is_jailed_like_a_workspace(scene: Scene) -> None:
    scene.copy()
    with pytest.raises(PathRejected):
        scene.candidates.fs.resolve(RUN, "../escape.py")


# --- WorkspaceFS additions ---------------------------------------------------------------------------


def test_bytes_are_written_with_the_same_jail_and_limits(scene: Scene) -> None:
    scene.real.write_bytes(PROJECT, "a.bin", b"\x00\x01")
    assert (scene.project / "a.bin").read_bytes() == b"\x00\x01"
    with pytest.raises(PathRejected):
        scene.real.write_bytes(PROJECT, "../x", b"x")


def test_delete_removes_a_file_inside_the_jail_only(scene: Scene) -> None:
    scene.real.write_text(PROJECT, "a.py", "x")
    scene.real.delete(PROJECT, "a.py")
    assert not (scene.project / "a.py").exists()
    with pytest.raises(WorkspaceNotFound):
        scene.real.delete(PROJECT, "a.py")
    with pytest.raises(PathRejected):
        scene.real.delete(PROJECT, "../x.py")


# --- Selection --------------------------------------------------------------------------------------


class _Launcher:
    def __init__(self, reason: str | None = None) -> None:
        self.reason = reason

    def unavailable_reason(self) -> str | None:
        return self.reason


def _option(name: str, reason: str | None = None, capabilities: tuple[str, ...] = ("code_generation",), simulated: bool = False) -> ProviderOption:
    return ProviderOption(name, _Launcher(reason), frozenset(capabilities), simulated=simulated)  # type: ignore[arg-type]


def test_the_first_available_provider_is_chosen_and_the_others_are_recorded() -> None:
    selection = ProviderSelector([_option("real", "no approved image"), _option("simulated", simulated=True)]).choose("code_generation")
    assert selection.provider == "simulated" and selection.simulated is True and selection.excluded == {"real": "no approved image"}


def test_a_provider_is_not_chosen_for_work_it_does_not_do() -> None:
    selection = ProviderSelector([_option("docs", capabilities=("documentation",)), _option("code")]).choose("code_generation")
    assert selection.provider == "code" and "does not do" in selection.excluded["docs"]


def test_no_provider_says_why_for_each() -> None:
    with pytest.raises(NoProvider, match="real: no approved image") as caught:
        ProviderSelector([_option("real", "no approved image")]).choose("code_generation")
    assert caught.value.excluded == {"real": "no approved image"}


def test_nothing_configured_is_said_plainly() -> None:
    with pytest.raises(NoProvider, match="none is configured"):
        ProviderSelector([]).choose("code_generation")
