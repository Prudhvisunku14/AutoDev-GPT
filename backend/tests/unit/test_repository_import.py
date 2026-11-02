"""Safe import: the archive is read in memory, anything unusual refuses the whole import, and nothing is ever run."""

from __future__ import annotations

import gzip
import io
import json
import tarfile
from pathlib import Path

import pytest

from app.repos import importer
from app.repos.fixtures import archive_bytes
from app.repos.importer import ImportLimits, ImportRefused, import_files, read_archive
from app.tools.adapters.workspace_fs import WorkspaceFS

LIMITS = ImportLimits(max_archive_bytes=1_000_000, max_files=50, max_file_bytes=50_000, max_total_bytes=500_000)
FILES = {"README.md": "# r\n", "LICENSE": "MIT License\n", "app/main.py": "print('never run')\n"}


def raw_tar(*members: tuple[tarfile.TarInfo, bytes | None]) -> bytes:
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w") as tar:
        for info, data in members:
            tar.addfile(info, io.BytesIO(data) if data is not None else None)
    return gzip.compress(buffer.getvalue())


def member(name: str, data: bytes = b"x", kind: bytes = tarfile.REGTYPE) -> tuple[tarfile.TarInfo, bytes | None]:
    info = tarfile.TarInfo(name)
    info.type = kind
    if kind == tarfile.REGTYPE:
        info.size = len(data)
        return info, data
    return info, None


def test_a_github_shaped_archive_is_read_without_its_top_folder() -> None:
    files = read_archive(archive_bytes("owner-repo-abc1234", FILES), LIMITS)
    assert files == {k: v.encode() for k, v in FILES.items()}


@pytest.mark.parametrize(
    ("archive", "reason"),
    [
        (archive_bytes("top", FILES, links={"evil": "/etc/passwd"}), "links are not imported"),
        (raw_tar(member("top/../escape.py")), "outside the repository folder"),
        (raw_tar(member("/abs/path.py")), "outside the repository folder"),
        (raw_tar(member("top/.git/hooks/post-checkout", b"#!/bin/sh\nrm -rf ~\n")), ".git path"),
        (raw_tar(member("top/opencode.json", b"{}")), "coding engine"),
        (raw_tar(member("top/a.py"), member("other/b.py")), "more than one top folder"),
        (raw_tar(member("top/dev", kind=tarfile.CHRTYPE)), "only regular files"),
        (raw_tar(member("top/big.bin", b"x" * 60_000)), "over the 50000-byte limit"),
        (b"not a tarball", "not a readable gzip tar archive"),
    ],
)
def test_an_unsafe_archive_is_refused_as_a_whole(archive: bytes, reason: str) -> None:
    with pytest.raises(ImportRefused, match=reason.replace(".", r"\.")):
        read_archive(archive, LIMITS)


def test_too_many_files_or_too_large_an_archive_is_refused() -> None:
    many = archive_bytes("top", {f"f{n}.txt": "x" for n in range(60)})
    with pytest.raises(ImportRefused, match="over the limit of 50 files"):
        read_archive(many, LIMITS)
    with pytest.raises(ImportRefused, match="over the 10-byte limit"):
        read_archive(many, ImportLimits(10, 50, 50_000, 500_000))


def workspace(tmp_path: Path) -> tuple[WorkspaceFS, str]:
    fs = WorkspaceFS(tmp_path / "workspaces", max_file_bytes=100_000, max_files=100)
    fs.create_project("a" * 32)
    return fs, "a" * 32


def test_an_import_records_its_source_and_keeps_the_licence(tmp_path: Path) -> None:
    fs, project = workspace(tmp_path)
    record = import_files(fs, project, read_archive(archive_bytes("top", FILES), LIMITS), full_name="o/r", url="https://github.com/o/r", commit="c" * 40,
                          license="MIT", attribution="keep the LICENSE file", source="fixture")  # fmt: skip
    root = fs.project_dir(project)
    assert record.license_files == ("LICENSE",) and (root / "app" / "main.py").is_file()
    saved = json.loads((root / importer.RECORD_FILE).read_text(encoding="utf-8"))
    assert saved["commit"] == "c" * 40 and saved["license"] == "MIT" and "o/r" in (root / importer.NOTICES_FILE).read_text(encoding="utf-8")


def test_an_import_never_overwrites_the_users_files(tmp_path: Path) -> None:
    fs, project = workspace(tmp_path)
    fs.write_text(project, "README.md", "the user's notes\n")
    with pytest.raises(ImportRefused, match="never overwrites"):
        import_files(fs, project, read_archive(archive_bytes("top", FILES), LIMITS), full_name="o/r", url="u", commit="c" * 40, license="MIT", attribution="a", source="fixture")
    assert fs.read_text(project, "README.md") == "the user's notes\n" and not (fs.project_dir(project) / "app").exists()


def test_a_failed_import_is_undone(tmp_path: Path) -> None:
    fs = WorkspaceFS(tmp_path / "workspaces", max_file_bytes=100_000, max_files=2)
    fs.create_project("b" * 32)
    with pytest.raises(ImportRefused, match="workspace refused"):
        import_files(fs, "b" * 32, read_archive(archive_bytes("top", FILES), LIMITS), full_name="o/r", url="u", commit="c" * 40, license="MIT", attribution="a", source="fixture")
    assert fs.list_files("b" * 32) == []


def test_the_importer_cannot_run_anything() -> None:
    source = Path(importer.__file__).read_text(encoding="utf-8")
    assert not any(word in source for word in ("subprocess", "os.system", "extractall", "exec(", "eval("))
