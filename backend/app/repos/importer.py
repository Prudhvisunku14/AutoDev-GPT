"""Importing an approved public repository into a project's workspace, safely. It never runs anything it downloads.

An import is a download of the archive of one pinned commit, not a `git clone`: there is no `.git` folder, no hook,
no submodule and no configuration to run. The archive is read in memory, member by member, and refused as a whole
if any member is a link, a device, a path that escapes the folder, a `.git` path, a file that could reconfigure the
coding engine, or over a limit. Files go through the workspace jail, and an existing file is never overwritten.
The repository's URL, commit and licence are recorded, and its licence file is kept for attribution.
"""

from __future__ import annotations

import io
import json
import tarfile
from dataclasses import asdict, dataclass
from pathlib import PurePosixPath

from app.coding.opencode_policy import is_protected
from app.tools.adapters.workspace_fs import PathRejected, WorkspaceError, WorkspaceFS, check_relative_path

NOTICES_FILE, RECORD_FILE = "THIRD_PARTY_NOTICES.md", "DEVPILOT_IMPORT.json"
_LICENSE_NAMES = ("LICENSE", "LICENSE.md", "LICENSE.txt", "LICENCE", "COPYING", "NOTICE")


class ImportRefused(Exception):
    pass


@dataclass(frozen=True)
class ImportLimits:
    max_archive_bytes: int
    max_files: int
    max_file_bytes: int
    max_total_bytes: int


@dataclass(frozen=True)
class ImportRecord:
    full_name: str
    url: str
    commit: str
    license: str
    attribution: str
    source: str
    files: tuple[str, ...]
    license_files: tuple[str, ...]
    notices_file: str = NOTICES_FILE
    record_file: str = RECORD_FILE
    into: str = ""  # the component folder it went into; "" is the project's root

    def to_dict(self) -> dict[str, object]:
        return asdict(self)


def read_archive(data: bytes, limits: ImportLimits) -> dict[str, bytes]:
    """The archive's regular files under its single top folder. Anything unusual refuses the whole import."""
    if len(data) > limits.max_archive_bytes:
        raise ImportRefused(f"the archive is {len(data)} bytes, over the {limits.max_archive_bytes}-byte limit")
    try:
        archive = tarfile.open(fileobj=io.BytesIO(data), mode="r:gz")
    except (tarfile.TarError, OSError, EOFError) as exc:
        raise ImportRefused(f"not a readable gzip tar archive: {exc}") from None
    files: dict[str, bytes] = {}
    roots: set[str] = set()
    total = 0
    with archive:
        for member in archive:
            name = member.name.replace("\\", "/")
            parts = PurePosixPath(name).parts
            if not parts or name.startswith("/") or ":" in parts[0] or ".." in parts or "\x00" in name:
                raise ImportRefused(f"{member.name!r}: a path outside the repository folder")
            roots.add(parts[0])
            if len(roots) > 1:
                raise ImportRefused("the archive has more than one top folder")
            if member.issym() or member.islnk():
                raise ImportRefused(f"{member.name}: links are not imported")
            if member.isdir() or member.type == tarfile.XGLTYPE or member.type == tarfile.XHDTYPE:
                continue
            if not member.isfile():
                raise ImportRefused(f"{member.name}: only regular files are imported")
            rel = "/".join(parts[1:])
            if not rel:
                continue
            if ".git" in PurePosixPath(rel).parts:
                raise ImportRefused(f"{rel}: a .git path could carry hooks or configuration")
            if is_protected(rel):
                raise ImportRefused(f"{rel}: this path could change the coding engine's configuration")
            try:
                check_relative_path(rel)
            except PathRejected as exc:
                raise ImportRefused(f"{rel}: {exc}") from None
            if member.size > limits.max_file_bytes:
                raise ImportRefused(f"{rel}: {member.size} bytes, over the {limits.max_file_bytes}-byte limit")
            total += member.size
            if total > limits.max_total_bytes or len(files) >= limits.max_files:
                raise ImportRefused(f"the repository is over the limit of {limits.max_files} files or {limits.max_total_bytes} bytes")
            extracted = archive.extractfile(member)
            files[rel] = extracted.read() if extracted is not None else b""
    if not files:
        raise ImportRefused("the archive holds no files")
    return files


def import_files(fs: WorkspaceFS, project_id: str, files: dict[str, bytes], *, full_name: str, url: str, commit: str, license: str, attribution: str, source: str, into: str = "") -> ImportRecord:
    """Writes into the project's workspace, or one component folder of it, through its jail. Nothing existing is
    overwritten, and a failure undoes the import."""
    root = fs.project_dir(project_id)
    prefix = f"{into}/" if into else ""
    if into:
        try:
            check_relative_path(into)
        except PathRejected as exc:
            raise ImportRefused(f"{into!r}: {exc}") from None
    files = {prefix + rel: data for rel, data in files.items()}
    notices_file, record_file = prefix + NOTICES_FILE, prefix + RECORD_FILE
    clashes = sorted(rel for rel in [*files, notices_file, record_file] if (root / rel).exists())
    if clashes:
        raise ImportRefused(f"the project already has {', '.join(clashes[:5])}; an import never overwrites the user's files")
    names = {n.split(".")[0] for n in _LICENSE_NAMES}
    license_files = tuple(sorted(rel for rel in files if rel.split("/")[-1].upper().split(".")[0] in names and rel.count("/") == prefix.count("/")))
    record = ImportRecord(full_name, url, commit, license, attribution, source, tuple(sorted(files)), license_files, notices_file, record_file, into)
    notices = (
        f"# Third-party code\n\nThis project starts from [{full_name}]({url}) at commit `{commit}`, licensed under {license}.\n\n"
        f"Condition: {attribution}.\n\nLicence files kept from it: {', '.join(license_files) or 'none'}.\n"
    )
    written: list[str] = []
    try:
        for rel, data in sorted(files.items()):
            fs.write_bytes(project_id, rel, data)
            written.append(rel)
        fs.write_text(project_id, notices_file, notices)
        written.append(notices_file)
        fs.write_text(project_id, record_file, json.dumps(record.to_dict(), indent=2) + "\n")
        written.append(record_file)
    except WorkspaceError as exc:
        for rel in written:
            fs.delete(project_id, rel)
        raise ImportRefused(f"the workspace refused the import: {exc}") from None
    return record
