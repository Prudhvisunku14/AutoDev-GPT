"""The official OpenCode binary: registry metadata, integrity, archive, ELF and manifest checks.

DevPilot never installs OpenCode on the host and never runs it there. The platform package is
downloaded once from the npm registry, checked against the registry's published SHA-512
integrity, unpacked only after every archive entry has been checked, and later mounted
read-only into a container. Before each mount the binary is checked against the manifest
written here. Nothing in this module executes the binary: the ELF reader only parses bytes.
"""

from __future__ import annotations

import base64
import hashlib
import json
import re
import struct
import tarfile
from dataclasses import asdict, dataclass
from pathlib import Path, PurePosixPath
from typing import Any, BinaryIO

REGISTRY = "https://registry.npmjs.org"
ARCHIVE_ROOT = "package"
BINARY_ENTRY = "package/bin/opencode"
MAX_ARCHIVE_BYTES = 300 * 1024 * 1024  # a download is abandoned beyond this
MAX_UNPACKED_BYTES = 400 * 1024 * 1024
MAX_ENTRIES = 64
MUSL_INTERPRETER = "/lib/ld-musl-x86_64.so.1"
_NAME = re.compile(r"^[a-z0-9][a-z0-9._-]{0,213}$")
_VERSION = re.compile(r"^\d+\.\d+\.\d+$")
_SHA1_HEX = re.compile(r"^[0-9a-f]{40}$")
_CHUNK = 1024 * 1024


class ArtifactError(Exception):
    """The artifact cannot be used. Nothing was unpacked or mounted."""


@dataclass(frozen=True)
class Dist:
    package: str
    version: str
    tarball: str
    integrity: str
    shasum: str | None
    file_count: int | None
    unpacked_size: int | None


def metadata_url(package: str, version: str) -> str:
    _check_name(package, version)
    return f"{REGISTRY}/{package}/{version}"


def tarball_url(package: str, version: str) -> str:
    _check_name(package, version)
    return f"{REGISTRY}/{package}/-/{package}-{version}.tgz"


def check_metadata(meta: Any, package: str, version: str, *, pinned_integrity: str | None = None) -> Dist:
    """The tarball to fetch, from the registry's version document. Anything unexpected is refused."""
    if not isinstance(meta, dict) or meta.get("name") != package or meta.get("version") != version:
        raise ArtifactError(f"the registry document is not {package}@{version}")
    dist = meta.get("dist")
    if not isinstance(dist, dict):
        raise ArtifactError("the registry document has no dist section")
    tarball = dist.get("tarball")
    if tarball != tarball_url(package, version):
        # The tarball must come from the registry itself, at its canonical path, never from a URL the document names.
        raise ArtifactError(f"the tarball URL is not the registry's canonical URL: {tarball!r}")
    integrity = dist.get("integrity")
    if not isinstance(integrity, str):
        raise ArtifactError("the registry document has no integrity value")
    sha512_digests(integrity)
    if pinned_integrity is not None and integrity != pinned_integrity:
        raise ArtifactError("the registry's integrity value differs from the pinned one")
    shasum = dist.get("shasum")
    if shasum is not None and not (isinstance(shasum, str) and _SHA1_HEX.match(shasum)):
        raise ArtifactError("the registry's shasum is malformed")
    return Dist(
        package=package,
        version=version,
        tarball=tarball,
        integrity=integrity,
        shasum=shasum,
        file_count=_int_or_none(dist.get("fileCount")),
        unpacked_size=_int_or_none(dist.get("unpackedSize")),
    )


def sha512_digests(integrity: str) -> list[bytes]:
    """The SHA-512 digests in a Subresource Integrity value. Other algorithms are ignored."""
    digests: list[bytes] = []
    for token in integrity.split():
        algorithm, _, value = token.partition("-")
        if algorithm != "sha512":
            continue
        try:
            digest = base64.b64decode(value.split("?", 1)[0], validate=True)
        except ValueError as exc:
            raise ArtifactError("the integrity value is not valid base64") from exc
        if len(digest) != 64:
            raise ArtifactError("the integrity value is not a SHA-512 digest")
        digests.append(digest)
    if not digests:
        raise ArtifactError("the integrity value has no SHA-512 digest")
    return digests


class Hashes:
    """Every digest of a download, computed while it streams."""

    def __init__(self) -> None:
        self._sha512 = hashlib.sha512()
        self._sha256 = hashlib.sha256()
        self._sha1 = hashlib.sha1(usedforsecurity=False)
        self.size = 0

    def update(self, chunk: bytes) -> None:
        self._sha512.update(chunk)
        self._sha256.update(chunk)
        self._sha1.update(chunk)
        self.size += len(chunk)

    @property
    def sha512(self) -> bytes:
        return self._sha512.digest()

    @property
    def sha256_hex(self) -> str:
        return self._sha256.hexdigest()

    @property
    def sha1_hex(self) -> str:
        return self._sha1.hexdigest()


def verify_download(hashes: Hashes, dist: Dist, *, content_length: int | None) -> None:
    """Raise unless the bytes are complete and match the published integrity."""
    if hashes.size == 0:
        raise ArtifactError("the download is empty")
    if content_length is not None and hashes.size != content_length:
        raise ArtifactError(f"the download is incomplete: {hashes.size} of {content_length} bytes")
    if hashes.sha512 not in sha512_digests(dist.integrity):
        raise ArtifactError("the download does not match the published SHA-512 integrity")
    if dist.shasum is not None and hashes.sha1_hex != dist.shasum:
        raise ArtifactError("the download does not match the published shasum")


def check_members(members: list[tarfile.TarInfo], *, max_unpacked: int = MAX_UNPACKED_BYTES) -> list[str]:
    """Problems with the archive's entries. Only plain files and folders under package/ pass."""
    problems: list[str] = []
    if len(members) > MAX_ENTRIES:
        problems.append(f"the archive has {len(members)} entries (limit {MAX_ENTRIES})")
    seen: set[str] = set()
    total = 0
    for member in members:
        name = member.name
        if "\\" in name or "\x00" in name or not name:
            problems.append(f"{name!r}: unsafe characters in the name")
            continue
        path = PurePosixPath(name)
        if path.is_absolute() or name.startswith("/"):
            problems.append(f"{name!r}: absolute path")
            continue
        if ".." in path.parts:
            problems.append(f"{name!r}: path leaves the archive root")
            continue
        if path.parts[0] != ARCHIVE_ROOT:
            problems.append(f"{name!r}: outside {ARCHIVE_ROOT}/")
            continue
        key = str(path).casefold()  # the cache is on Windows, where case does not distinguish files
        if key in seen:
            problems.append(f"{name!r}: duplicate entry")
        seen.add(key)
        if not (member.isreg() or member.isdir()):
            problems.append(f"{name!r}: not a plain file or folder (link, device or FIFO)")
            continue
        if member.mode & 0o7000:
            problems.append(f"{name!r}: setuid, setgid or sticky bit")
        if member.isreg():
            total += member.size
    if total > max_unpacked:
        problems.append(f"the archive unpacks to {total} bytes (limit {max_unpacked})")
    if not any(m.isreg() and str(PurePosixPath(m.name)) == BINARY_ENTRY for m in members):
        problems.append(f"the archive has no {BINARY_ENTRY}")
    return problems


def extract(archive: Path, dest: Path) -> tuple[Path, int, int]:
    """Unpack into a new, empty folder after every entry passes. Returns (binary, files, bytes)."""
    if dest.exists() and (not dest.is_dir() or any(dest.iterdir())):
        raise ArtifactError(f"the extraction folder is not new and empty: {dest}")
    with tarfile.open(archive, "r:gz") as tar:
        members = tar.getmembers()
        problems = check_members(members)
        if problems:
            raise ArtifactError("the archive was refused: " + "; ".join(problems[:5]))
        dest.mkdir(parents=True, exist_ok=True)
        # The "data" filter refuses links, absolute paths and special files again, as a second layer.
        tar.extractall(dest, members=members, filter="data")
    files = [m for m in members if m.isreg()]
    binary = dest.joinpath(*PurePosixPath(BINARY_ENTRY).parts)
    if binary.is_symlink() or not binary.is_file():
        raise ArtifactError("the binary is missing after extraction")
    return binary, len(files), sum(m.size for m in files)


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while chunk := handle.read(_CHUNK):
            digest.update(chunk)
    return digest.hexdigest()


@dataclass(frozen=True)
class ElfInfo:
    machine: int
    interpreter: str | None
    needed: tuple[str, ...]
    runpath: tuple[str, ...]


def elf_info(handle: BinaryIO) -> ElfInfo:
    """The loader and shared libraries an x86-64 ELF file asks for, read from its headers only."""
    header = _read(handle, 0, 64)
    if header[:4] != b"\x7fELF" or header[4] != 2 or header[5] != 1:
        raise ArtifactError("not a 64-bit little-endian ELF file")
    machine = struct.unpack_from("<H", header, 18)[0]
    phoff, phentsize, phnum = struct.unpack_from("<Q", header, 32)[0], *struct.unpack_from("<HH", header, 54)
    if phentsize != 56 or not 0 < phnum <= 128:
        raise ArtifactError("unexpected ELF program header table")
    segments = [struct.unpack_from("<IIQQQQQQ", _read(handle, phoff + i * 56, 56)) for i in range(phnum)]
    interpreter = None
    dynamic = None
    loads = []
    for p_type, _flags, offset, vaddr, _paddr, filesz, _memsz, _align in segments:
        if p_type == 1:  # PT_LOAD
            loads.append((vaddr, offset, filesz))
        elif p_type == 2:  # PT_DYNAMIC
            dynamic = (offset, filesz)
        elif p_type == 3 and filesz <= 4096:  # PT_INTERP
            interpreter = _read(handle, offset, filesz).split(b"\x00", 1)[0].decode("ascii", "replace")
    if dynamic is None:
        return ElfInfo(machine, interpreter, (), ())  # statically linked
    offset, size = dynamic
    if size > 64 * 1024:
        raise ArtifactError("unexpected ELF dynamic section size")
    raw = _read(handle, offset, size)
    entries = [struct.unpack_from("<qQ", raw, i) for i in range(0, len(raw) - 15, 16)]
    strtab = next((value for tag, value in entries if tag == 5), None)  # DT_STRTAB
    if strtab is None:
        raise ArtifactError("the ELF dynamic section has no string table")
    strtab_offset = next((off + strtab - va for va, off, sz in loads if va <= strtab < va + sz), None)
    if strtab_offset is None:
        raise ArtifactError("the ELF string table is outside every loaded segment")

    def name(at: int) -> str:
        handle.seek(strtab_offset + at)
        return handle.read(256).split(b"\x00", 1)[0].decode("ascii", "replace")

    needed: list[str] = []
    runpath: list[str] = []
    for tag, value in entries:
        if tag == 0:  # DT_NULL
            break
        if tag == 1:  # DT_NEEDED
            needed.append(name(value))
        elif tag in (15, 29):  # DT_RPATH, DT_RUNPATH
            runpath.append(name(value))
    return ElfInfo(machine, interpreter, tuple(needed), tuple(runpath))


@dataclass(frozen=True)
class Manifest:
    package: str
    version: str
    metadata_url: str
    tarball_url: str
    tarball_file: str  # relative to the manifest's folder
    tarball_bytes: int
    integrity: str  # the registry's SHA-512 SRI value, matched byte for byte
    shasum: str | None
    tarball_sha256: str
    binary_file: str  # relative to the manifest's folder
    binary_bytes: int
    binary_sha256: str
    files: int
    unpacked_bytes: int
    registry_file_count: int | None
    registry_unpacked_size: int | None
    elf_interpreter: str | None
    elf_needed: tuple[str, ...]
    verified_at: str


def write_manifest(path: Path, manifest: Manifest) -> None:
    path.write_text(json.dumps(asdict(manifest), indent=2) + "\n", encoding="utf-8")


def load_manifest(path: Path) -> Manifest:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        data["elf_needed"] = tuple(data["elf_needed"])
        return Manifest(**data)
    except (OSError, ValueError, KeyError, TypeError) as exc:
        raise ArtifactError(f"the manifest cannot be read: {path}") from exc


def verify_binary(manifest_path: Path) -> tuple[Manifest, Path]:
    """The binary named by the manifest, after checking its size and SHA-256 again."""
    manifest = load_manifest(manifest_path)
    root = manifest_path.parent.resolve()
    binary = (root / manifest.binary_file).resolve()
    if root not in binary.parents:
        raise ArtifactError("the manifest points outside its own folder")
    if binary.is_symlink() or not binary.is_file():
        raise ArtifactError(f"the binary is missing: {binary}")
    if binary.stat().st_size != manifest.binary_bytes or sha256_file(binary) != manifest.binary_sha256:
        raise ArtifactError("the binary no longer matches its manifest")
    return manifest, binary


def _read(handle: BinaryIO, offset: int, size: int) -> bytes:
    handle.seek(offset)
    data = handle.read(size)
    if len(data) != size:
        raise ArtifactError("the ELF file is truncated")
    return data


def _check_name(package: str, version: str) -> None:
    if not _NAME.match(package) or not _VERSION.match(version):
        raise ArtifactError(f"not a plain package name and version: {package}@{version}")


def _int_or_none(value: Any) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) and value >= 0 else None
