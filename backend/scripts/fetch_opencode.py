"""Download and verify the official OpenCode Linux binary. It never runs the binary.

Steps (each failure stops the script and removes partial files):
1. Fetch the version document from the npm registry and check it: package, version, the
   registry's canonical tarball URL, and the SHA-512 integrity pinned below (recorded in Phase 4B).
2. Download that tarball (no redirects, size cap), hashing it while it streams.
3. Refuse it unless it is complete and matches the published SHA-512 integrity and shasum.
4. Check every archive entry (no links, devices, absolute paths or `..`), then unpack.
5. Read the binary's ELF headers (loader and libraries; nothing is executed).
6. Write manifest.json, which the contract check verifies again before mounting the binary.

    & $env:DEVPILOT_PYTHON -I backend\\scripts\\fetch_opencode.py              # download and verify
    & $env:DEVPILOT_PYTHON -I backend\\scripts\\fetch_opencode.py --verify-only  # re-hash the kept binary
"""

from __future__ import annotations

import argparse
import json
import shutil
import sys
from datetime import UTC, datetime
from pathlib import Path

import httpx

from app.coding.opencode_artifact import (
    MAX_ARCHIVE_BYTES,
    ArtifactError,
    Hashes,
    Manifest,
    check_metadata,
    elf_info,
    extract,
    metadata_url,
    sha256_file,
    verify_binary,
    verify_download,
    write_manifest,
)
from app.coding.opencode_contract import CONTRACT_VERSION

PACKAGE = "opencode-linux-x64-musl"
# Published by registry.npmjs.org for opencode-linux-x64-musl@1.19.5, recorded on 2026-10-30 (Phase 4B).
PINNED_INTEGRITY = "sha512-y6nUbH3ViajN37KaVVy0/KnWv/i48Bx5QPv9Ul9ONJya1ossV7CHVb+ZNFc61M5GqlVtjCvZV7T0eBMqhJG7mg=="
DEFAULT_CACHE = Path(r"D:\DevPilotCache\opencode")  # on D: (CLAUDE.md: nothing large on C:)
METADATA_MAX_BYTES = 1024 * 1024
TIMEOUT = httpx.Timeout(30.0, read=120.0)


def _fetch_metadata(client: httpx.Client, url: str) -> bytes:
    with client.stream("GET", url, headers={"Accept": "application/json"}) as response:
        if response.status_code != 200:
            raise ArtifactError(f"the registry answered {response.status_code} for {url}")
        body = bytearray()
        for chunk in response.iter_bytes():
            body.extend(chunk)
            if len(body) > METADATA_MAX_BYTES:
                raise ArtifactError("the registry document is unexpectedly large")
    return bytes(body)


def _download(client: httpx.Client, url: str, part: Path) -> tuple[Hashes, int | None]:
    hashes = Hashes()
    with client.stream("GET", url) as response:
        if response.status_code != 200:
            raise ArtifactError(f"the registry answered {response.status_code} for the tarball")
        length = response.headers.get("content-length")
        content_length = int(length) if length and length.isdigit() else None
        if content_length is not None and content_length > MAX_ARCHIVE_BYTES:
            raise ArtifactError(f"the tarball is {content_length} bytes (limit {MAX_ARCHIVE_BYTES})")
        with part.open("xb") as handle:
            for chunk in response.iter_raw():  # raw: the bytes exactly as published, no content decoding
                hashes.update(chunk)
                if hashes.size > MAX_ARCHIVE_BYTES:
                    raise ArtifactError(f"the tarball exceeds {MAX_ARCHIVE_BYTES} bytes")
                handle.write(chunk)
    return hashes, content_length


def _folder_bytes(root: Path) -> int:
    return sum(p.stat().st_size for p in root.rglob("*") if p.is_file() and not p.is_symlink())


def fetch(cache: Path) -> int:
    root = cache / CONTRACT_VERSION
    download = root / "download"
    extracted = root / "extracted"
    manifest_path = root / "manifest.json"
    if manifest_path.exists():
        print(f"Already downloaded: {manifest_path}. Use --verify-only to check it again.")
        return 1
    for folder in (download, extracted):
        if folder.exists() and any(folder.iterdir()):
            print(f"Refusing to reuse a folder that is not empty: {folder}")
            return 1
    download.mkdir(parents=True, exist_ok=True)

    meta_url = metadata_url(PACKAGE, CONTRACT_VERSION)
    part: Path | None = None
    try:
        with httpx.Client(timeout=TIMEOUT, follow_redirects=False) as client:
            raw = _fetch_metadata(client, meta_url)
            (download / "metadata.json").write_bytes(raw)
            dist = check_metadata(json.loads(raw), PACKAGE, CONTRACT_VERSION, pinned_integrity=PINNED_INTEGRITY)
            print(f"Registry document: {meta_url}")
            print(f"  tarball:   {dist.tarball}")
            print(f"  integrity: {dist.integrity}")
            print(f"  shasum:    {dist.shasum}")
            print(f"  files: {dist.file_count}, unpacked size: {dist.unpacked_size} bytes")

            name = dist.tarball.rsplit("/", 1)[1]
            part = download / f"{name}.part"
            hashes, content_length = _download(client, dist.tarball, part)
        verify_download(hashes, dist, content_length=content_length)
        tarball = download / name
        part.rename(tarball)
        part = None
        print(f"Downloaded {tarball.name}: {hashes.size} bytes (Content-Length {content_length})")
        print("  SHA-512 integrity: MATCHES the registry value and the pinned value")
        print(f"  SHA-1 shasum:      MATCHES ({hashes.sha1_hex})")
        print(f"  SHA-256:           {hashes.sha256_hex}")

        binary, files, unpacked = extract(tarball, extracted)
        print(f"Archive entries checked and unpacked: {files} files, {unpacked} bytes")
        with binary.open("rb") as handle:
            elf = elf_info(handle)
        manifest = Manifest(
            package=PACKAGE,
            version=CONTRACT_VERSION,
            metadata_url=meta_url,
            tarball_url=dist.tarball,
            tarball_file=tarball.relative_to(root).as_posix(),
            tarball_bytes=hashes.size,
            integrity=dist.integrity,
            shasum=dist.shasum,
            tarball_sha256=hashes.sha256_hex,
            binary_file=binary.relative_to(root).as_posix(),
            binary_bytes=binary.stat().st_size,
            binary_sha256=sha256_file(binary),
            files=files,
            unpacked_bytes=unpacked,
            registry_file_count=dist.file_count,
            registry_unpacked_size=dist.unpacked_size,
            elf_interpreter=elf.interpreter,
            elf_needed=elf.needed,
            verified_at=datetime.now(UTC).isoformat(timespec="seconds"),
        )
        write_manifest(manifest_path, manifest)
    except (ArtifactError, httpx.HTTPError, OSError, ValueError) as exc:
        print(f"STOPPED, verification failed: {exc}")
        if part is not None:
            part.unlink(missing_ok=True)
        shutil.rmtree(extracted, ignore_errors=True)
        return 1

    _report(manifest, binary, root, elf.machine)
    return 0


def _report(manifest: Manifest, binary: Path, root: Path, machine: int) -> None:
    print(f"Binary: {binary}")
    print(f"  size {manifest.binary_bytes} bytes, SHA-256 {manifest.binary_sha256}")
    print(f"  ELF machine {machine} (62 = x86-64), loader {manifest.elf_interpreter}")
    print(f"  needs: {', '.join(manifest.elf_needed) or 'nothing (static)'}")
    print(f"  registry: {manifest.registry_file_count} files, {manifest.registry_unpacked_size} bytes unpacked")
    print(f"Manifest: {root / 'manifest.json'}")
    print(f"Disk used by {root}: {_folder_bytes(root)} bytes")


def verify_only(cache: Path) -> int:
    root = cache / CONTRACT_VERSION
    try:
        manifest, binary = verify_binary(root / "manifest.json")
        with binary.open("rb") as handle:
            machine = elf_info(handle).machine
    except ArtifactError as exc:
        print(f"STOPPED: {exc}")
        return 1
    print("The kept binary still matches its manifest.")
    _report(manifest, binary, root, machine)
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--cache-dir", type=Path, default=DEFAULT_CACHE)
    parser.add_argument("--verify-only", action="store_true", help="re-hash the kept binary; no network")
    args = parser.parse_args()
    return verify_only(args.cache_dir) if args.verify_only else fetch(args.cache_dir)


if __name__ == "__main__":
    sys.exit(main())
