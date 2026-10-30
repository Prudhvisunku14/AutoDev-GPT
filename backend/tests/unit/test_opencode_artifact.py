"""Checks for the downloaded OpenCode binary: registry metadata, integrity, archive entries, ELF, manifest.

No network and no OpenCode: archives and ELF files are built in the test.
"""

from __future__ import annotations

import base64
import hashlib
import io
import struct
import tarfile
from dataclasses import replace
from pathlib import Path

import pytest

from app.coding.opencode_artifact import (
    BINARY_ENTRY,
    MUSL_INTERPRETER,
    ArtifactError,
    Dist,
    Hashes,
    Manifest,
    check_members,
    check_metadata,
    elf_info,
    extract,
    load_manifest,
    sha256_file,
    sha512_digests,
    tarball_url,
    verify_binary,
    verify_download,
    write_manifest,
)

PACKAGE = "opencode-linux-x64-musl"
VERSION = "1.19.5"
PAYLOAD = b"pretend tarball bytes"


def _sri(data: bytes) -> str:
    return "sha512-" + base64.b64encode(hashlib.sha512(data).digest()).decode()


def _meta(**dist: object) -> dict[str, object]:
    base: dict[str, object] = {
        "tarball": tarball_url(PACKAGE, VERSION),
        "integrity": _sri(PAYLOAD),
        "shasum": hashlib.sha1(PAYLOAD).hexdigest(),
        "fileCount": 2,
        "unpackedSize": 1234,
    }
    base.update(dist)
    return {"name": PACKAGE, "version": VERSION, "dist": base}


def _hashes(data: bytes) -> Hashes:
    hashes = Hashes()
    for i in range(0, len(data), 5):
        hashes.update(data[i : i + 5])
    return hashes


# --- Registry metadata ----------------------------------------------------------------------


def test_the_canonical_registry_document_is_accepted() -> None:
    dist = check_metadata(_meta(), PACKAGE, VERSION, pinned_integrity=_sri(PAYLOAD))
    assert dist.tarball == "https://registry.npmjs.org/opencode-linux-x64-musl/-/opencode-linux-x64-musl-1.19.5.tgz"
    assert (dist.file_count, dist.unpacked_size) == (2, 1234)


@pytest.mark.parametrize(
    "tarball",
    [
        "https://evil.example/opencode-linux-x64-musl-1.19.5.tgz",
        "http://registry.npmjs.org/opencode-linux-x64-musl/-/opencode-linux-x64-musl-1.19.5.tgz",
        "https://registry.npmjs.org/opencode-linux-x64-musl/-/opencode-linux-x64-musl-1.19.4.tgz",
        "https://registry.npmjs.org.evil.example/opencode-linux-x64-musl/-/opencode-linux-x64-musl-1.19.5.tgz",
        None,
    ],
)
def test_a_tarball_url_other_than_the_registrys_own_is_refused(tarball: object) -> None:
    with pytest.raises(ArtifactError, match="canonical"):
        check_metadata(_meta(tarball=tarball), PACKAGE, VERSION)


@pytest.mark.parametrize(
    "meta",
    [
        {"name": "opencode-ai", "version": VERSION, "dist": {}},
        {"name": PACKAGE, "version": "1.19.4", "dist": {}},
        {"name": PACKAGE, "version": VERSION},
        [],
    ],
)
def test_a_document_for_another_package_or_without_dist_is_refused(meta: object) -> None:
    with pytest.raises(ArtifactError):
        check_metadata(meta, PACKAGE, VERSION)


def test_a_document_without_a_sha512_integrity_is_refused() -> None:
    with pytest.raises(ArtifactError, match="integrity"):
        check_metadata(_meta(integrity=None), PACKAGE, VERSION)
    with pytest.raises(ArtifactError, match="SHA-512"):
        check_metadata(_meta(integrity="sha1-" + base64.b64encode(b"x" * 20).decode()), PACKAGE, VERSION)


def test_an_integrity_that_differs_from_the_pinned_value_is_refused() -> None:
    with pytest.raises(ArtifactError, match="pinned"):
        check_metadata(_meta(), PACKAGE, VERSION, pinned_integrity=_sri(b"something else"))


def test_a_malformed_shasum_is_refused() -> None:
    with pytest.raises(ArtifactError, match="shasum"):
        check_metadata(_meta(shasum="not-hex"), PACKAGE, VERSION)


def test_names_with_path_characters_are_refused() -> None:
    with pytest.raises(ArtifactError):
        tarball_url("../evil", VERSION)
    with pytest.raises(ArtifactError):
        tarball_url(PACKAGE, "1.19.5/../../x")


def test_sri_values_with_several_algorithms_keep_only_sha512() -> None:
    value = "sha1-" + base64.b64encode(b"x" * 20).decode() + " " + _sri(PAYLOAD)
    assert sha512_digests(value) == [hashlib.sha512(PAYLOAD).digest()]
    with pytest.raises(ArtifactError, match="base64"):
        sha512_digests("sha512-***")
    with pytest.raises(ArtifactError, match="SHA-512 digest"):
        sha512_digests("sha512-" + base64.b64encode(b"short").decode())


# --- Download integrity ---------------------------------------------------------------------


def test_complete_matching_bytes_pass() -> None:
    dist = check_metadata(_meta(), PACKAGE, VERSION)
    hashes = _hashes(PAYLOAD)
    verify_download(hashes, dist, content_length=len(PAYLOAD))
    assert hashes.sha256_hex == hashlib.sha256(PAYLOAD).hexdigest()


def test_an_incomplete_download_is_refused_even_before_hashing() -> None:
    dist = check_metadata(_meta(), PACKAGE, VERSION)
    with pytest.raises(ArtifactError, match="incomplete"):
        verify_download(_hashes(PAYLOAD[:-1]), dist, content_length=len(PAYLOAD))


def test_changed_bytes_are_refused() -> None:
    dist = check_metadata(_meta(), PACKAGE, VERSION)
    tampered = PAYLOAD[:-1] + b"X"
    with pytest.raises(ArtifactError, match="SHA-512"):
        verify_download(_hashes(tampered), dist, content_length=len(tampered))


def test_a_shasum_mismatch_is_refused_even_when_sha512_matches() -> None:
    dist = replace(check_metadata(_meta(), PACKAGE, VERSION), shasum="0" * 40)
    with pytest.raises(ArtifactError, match="shasum"):
        verify_download(_hashes(PAYLOAD), dist, content_length=None)


def test_an_empty_download_is_refused() -> None:
    dist = check_metadata(_meta(), PACKAGE, VERSION)
    with pytest.raises(ArtifactError, match="empty"):
        verify_download(Hashes(), dist, content_length=None)


# --- Archive entries ------------------------------------------------------------------------


def _member(name: str, *, kind: bytes = tarfile.REGTYPE, size: int = 10, mode: int = 0o755) -> tarfile.TarInfo:
    info = tarfile.TarInfo(name)
    info.type = kind
    info.size = size if kind == tarfile.REGTYPE else 0
    info.mode = mode
    if kind in (tarfile.SYMTYPE, tarfile.LNKTYPE):
        info.linkname = "/etc/passwd"
    return info


def _good() -> list[tarfile.TarInfo]:
    return [_member("package/package.json", mode=0o644), _member(BINARY_ENTRY)]


def test_the_expected_two_file_archive_passes() -> None:
    assert check_members(_good()) == []
    assert check_members([_member("package/bin", kind=tarfile.DIRTYPE), *_good()]) == []


@pytest.mark.parametrize(
    ("member", "reason"),
    [
        (_member("package/../../evil.sh"), "leaves"),
        (_member("/etc/cron.d/evil"), "absolute"),
        (_member("other/file"), "outside"),
        (_member("package\\..\\evil"), "unsafe"),
        (_member("package/link", kind=tarfile.SYMTYPE), "link"),
        (_member("package/hard", kind=tarfile.LNKTYPE), "link"),
        (_member("package/dev", kind=tarfile.CHRTYPE), "device"),
        (_member("package/fifo", kind=tarfile.FIFOTYPE), "FIFO"),
        (_member("package/suid", mode=0o4755), "setuid"),
        (_member("PACKAGE/BIN/OPENCODE"), "outside"),
        (_member("package/BIN/OPENCODE"), "duplicate"),
    ],
)
def test_unsafe_entries_are_refused(member: tarfile.TarInfo, reason: str) -> None:
    problems = check_members([*_good(), member])
    assert any(reason in p for p in problems), problems


def test_an_archive_without_the_binary_or_too_large_is_refused() -> None:
    assert any("no package/bin/opencode" in p for p in check_members([_member("package/package.json")]))
    assert any("unpacks to" in p for p in check_members(_good(), max_unpacked=15))
    assert any("entries" in p for p in check_members([*_good(), *[_member(f"package/f{i}") for i in range(70)]]))


def _archive(path: Path, entries: list[tuple[tarfile.TarInfo, bytes]]) -> Path:
    with tarfile.open(path, "w:gz") as tar:
        for info, data in entries:
            info.size = len(data) if info.isreg() else 0
            tar.addfile(info, io.BytesIO(data) if info.isreg() else None)
    return path


def test_a_safe_archive_is_unpacked_into_a_new_folder(tmp_path: Path) -> None:
    archive = _archive(
        tmp_path / "ok.tgz", [(_member("package/package.json", mode=0o644), b"{}"), (_member(BINARY_ENTRY), b"\x7fELF")]
    )
    binary, files, size = extract(archive, tmp_path / "out")
    assert binary == tmp_path / "out" / "package" / "bin" / "opencode"
    assert binary.read_bytes() == b"\x7fELF"
    assert (files, size) == (2, 6)


def test_an_archive_with_a_link_is_refused_and_nothing_is_written(tmp_path: Path) -> None:
    archive = _archive(
        tmp_path / "bad.tgz",
        [(_member(BINARY_ENTRY), b"x"), (_member("package/escape", kind=tarfile.SYMTYPE), b"")],
    )
    with pytest.raises(ArtifactError, match="refused"):
        extract(archive, tmp_path / "out")
    assert not (tmp_path / "out").exists()


def test_extraction_refuses_a_folder_that_is_not_empty(tmp_path: Path) -> None:
    archive = _archive(tmp_path / "ok.tgz", [(_member(BINARY_ENTRY), b"x")])
    (tmp_path / "out").mkdir()
    (tmp_path / "out" / "old").write_text("x")
    with pytest.raises(ArtifactError, match="not new and empty"):
        extract(archive, tmp_path / "out")


# --- ELF headers (parsed, never executed) ----------------------------------------------------


def _elf(*, interpreter: bytes | None, needed: list[str], runpath: str | None = None, dynamic: bool = True) -> bytes:
    base = 0x400000
    strtab = b"\x00"
    offsets = []
    for name in needed:
        offsets.append(len(strtab))
        strtab += name.encode() + b"\x00"
    runpath_at = None
    if runpath:
        runpath_at = len(strtab)
        strtab += runpath.encode() + b"\x00"
    phnum = 1 + (1 if dynamic else 0) + (1 if interpreter else 0)
    interp_at = 64 + 56 * phnum
    interp = (interpreter + b"\x00") if interpreter else b""
    strtab_at = interp_at + len(interp)
    dyn_at = strtab_at + len(strtab)
    entries = [(1, off) for off in offsets]
    if runpath_at is not None:
        entries.append((29, runpath_at))
    entries += [(5, base + strtab_at), (10, len(strtab)), (0, 0)]
    dyn = b"".join(struct.pack("<qQ", tag, value) for tag, value in entries) if dynamic else b""
    total = dyn_at + len(dyn)
    header = struct.pack("<16sHHIQQQIHHHHHH", b"\x7fELF\x02\x01\x01", 3, 62, 1, 0, 64, 0, 0, 64, 56, phnum, 0, 0, 0)
    phdrs = struct.pack("<IIQQQQQQ", 1, 5, 0, base, base, total, total, 0x1000)
    if dynamic:
        phdrs += struct.pack("<IIQQQQQQ", 2, 6, dyn_at, base + dyn_at, base + dyn_at, len(dyn), len(dyn), 8)
    if interpreter:
        phdrs += struct.pack("<IIQQQQQQ", 3, 4, interp_at, base + interp_at, base + interp_at, len(interp), len(interp), 1)
    return header + phdrs + interp + strtab + dyn


def test_the_loader_and_needed_libraries_are_read_from_the_headers() -> None:
    data = _elf(interpreter=MUSL_INTERPRETER.encode(), needed=["libstdc++.so.6", "libgcc_s.so.1", "libc.musl-x86_64.so.1"])
    info = elf_info(io.BytesIO(data))
    assert info.machine == 62
    assert info.interpreter == MUSL_INTERPRETER
    assert info.needed == ("libstdc++.so.6", "libgcc_s.so.1", "libc.musl-x86_64.so.1")
    assert info.runpath == ()


def test_runpath_and_static_binaries_are_reported() -> None:
    assert elf_info(io.BytesIO(_elf(interpreter=None, needed=["libc.so"], runpath="$ORIGIN/lib"))).runpath == ("$ORIGIN/lib",)
    static = elf_info(io.BytesIO(_elf(interpreter=None, needed=[], dynamic=False)))
    assert (static.interpreter, static.needed) == (None, ())


def test_non_elf_and_truncated_files_are_refused() -> None:
    with pytest.raises(ArtifactError, match="ELF"):
        elf_info(io.BytesIO(b"#!/bin/sh\necho hi\n" + b"\x00" * 64))
    data = _elf(interpreter=MUSL_INTERPRETER.encode(), needed=["libc.so"])
    with pytest.raises(ArtifactError, match="truncated"):
        elf_info(io.BytesIO(data[:100]))


# --- Manifest -------------------------------------------------------------------------------


def _manifest(root: Path, binary: Path) -> Manifest:
    return Manifest(
        package=PACKAGE,
        version=VERSION,
        metadata_url="https://registry.npmjs.org/opencode-linux-x64-musl/1.19.5",
        tarball_url=tarball_url(PACKAGE, VERSION),
        tarball_file="download/x.tgz",
        tarball_bytes=1,
        integrity=_sri(PAYLOAD),
        shasum=None,
        tarball_sha256="0" * 64,
        binary_file=binary.relative_to(root).as_posix(),
        binary_bytes=binary.stat().st_size,
        binary_sha256=sha256_file(binary),
        files=2,
        unpacked_bytes=3,
        registry_file_count=2,
        registry_unpacked_size=3,
        elf_interpreter=MUSL_INTERPRETER,
        elf_needed=("libc.musl-x86_64.so.1",),
        verified_at="2026-10-30T00:00:00Z",
    )


def test_the_binary_is_checked_against_its_manifest_before_use(tmp_path: Path) -> None:
    binary = tmp_path / "extracted" / "package" / "bin" / "opencode"
    binary.parent.mkdir(parents=True)
    binary.write_bytes(b"\x7fELF binary bytes")
    write_manifest(tmp_path / "manifest.json", _manifest(tmp_path, binary))
    manifest, path = verify_binary(tmp_path / "manifest.json")
    assert path == binary.resolve()
    assert load_manifest(tmp_path / "manifest.json") == manifest

    binary.write_bytes(b"\x7fELF binary byteZ")  # same size, different content
    with pytest.raises(ArtifactError, match="no longer matches"):
        verify_binary(tmp_path / "manifest.json")


def test_a_manifest_pointing_outside_its_folder_or_at_nothing_is_refused(tmp_path: Path) -> None:
    binary = tmp_path / "bin"
    binary.write_bytes(b"x")
    root = tmp_path / "cache"
    root.mkdir()
    manifest = replace(_manifest(tmp_path, binary), binary_file="../bin")
    write_manifest(root / "manifest.json", manifest)
    with pytest.raises(ArtifactError, match="outside"):
        verify_binary(root / "manifest.json")
    write_manifest(root / "manifest.json", replace(manifest, binary_file="missing"))
    with pytest.raises(ArtifactError, match="missing"):
        verify_binary(root / "manifest.json")
    (root / "manifest.json").write_text("{not json")
    with pytest.raises(ArtifactError, match="cannot be read"):
        verify_binary(root / "manifest.json")


def test_dist_is_a_plain_record() -> None:
    dist = Dist(PACKAGE, VERSION, tarball_url(PACKAGE, VERSION), _sri(PAYLOAD), None, None, None)
    assert dist.shasum is None
