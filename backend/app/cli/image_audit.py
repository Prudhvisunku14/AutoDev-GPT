"""An offline audit of a Docker image, from its `docker save` archive. It reads tar members and runs nothing.

The OpenCode runtime image is built on each machine (from the pinned Dockerfile and hash-verified inputs), so its ID cannot be a
constant in the code. What can be checked on any machine is its *content*, and that is what this module checks before an installation
approves the image it built:

- it runs as a non-root user and exposes nothing (no entrypoint, ports or volumes);
- its layers are intact (each layer's hash equals the `diff_id` the image's config records);
- the last layer, the one DevPilot added, holds exactly one file: `usr/local/bin/rg`, root-owned, mode 0755, not setuid, and its
  SHA-256 is the pinned ripgrep's (so OpenCode never tries to download one);
- the layers beneath it provide the C++ runtime libraries the OpenCode binary needs.

Both archive layouts are read: OCI (Docker's containerd image store) and the older `manifest.json` form.
"""

from __future__ import annotations

import gzip
import hashlib
import io
import json
import re
import tarfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

RUN_USER = "65534:65534"
RG_PATH = "usr/local/bin/rg"
_RG_ALLOWED = frozenset({"usr", "usr/local", "usr/local/bin", RG_PATH})
_LIBRARIES = {"libstdc++": re.compile(r"(^|/)libstdc\+\+\.so\.6"), "libgcc_s": re.compile(r"(^|/)libgcc_s\.so\.1$")}
_MAX_LAYER_BYTES = 600_000_000


@dataclass
class Audit:
    problems: list[str] = field(default_factory=list)
    facts: dict[str, Any] = field(default_factory=dict)

    @property
    def ok(self) -> bool:
        return not self.problems


def _name(member: tarfile.TarInfo) -> str:
    return member.name.removeprefix("./").rstrip("/")


def _blob(tar: tarfile.TarFile, name: str) -> bytes | None:
    try:
        handle = tar.extractfile(name)
    except KeyError:
        return None
    return handle.read() if handle is not None else None


def _resolve_oci(tar: tarfile.TarFile, descriptor: dict[str, Any], depth: int = 0) -> tuple[dict[str, Any], dict[str, Any]] | None:
    """The (image manifest, config) a descriptor leads to, skipping attestation manifests."""
    digest = str(descriptor.get("digest", "")).removeprefix("sha256:")
    body = _blob(tar, f"blobs/sha256/{digest}")
    if body is None or depth > 3:
        return None
    document = json.loads(body)
    if "manifests" in document:
        for entry in document["manifests"]:
            if (entry.get("platform") or {}).get("os") == "unknown" or (entry.get("annotations") or {}).get("vnd.docker.reference.type") == "attestation-manifest":
                continue
            found = _resolve_oci(tar, entry, depth + 1)
            if found:
                return found
        return None
    config = _blob(tar, "blobs/sha256/" + str(document.get("config", {}).get("digest", "")).removeprefix("sha256:"))
    return (document, json.loads(config)) if config is not None else None


def _layers(tar: tarfile.TarFile) -> tuple[dict[str, Any], list[bytes]] | str:
    """The config and the layers' raw bytes, in order; or why they cannot be read."""
    names = set(tar.getnames())
    if "index.json" in names:
        index = json.loads(_blob(tar, "index.json") or b"{}")
        for descriptor in index.get("manifests", []):
            found = _resolve_oci(tar, descriptor)
            if found:
                manifest, config = found
                blobs = [_blob(tar, "blobs/sha256/" + layer["digest"].removeprefix("sha256:")) for layer in manifest["layers"]]
                if any(b is None for b in blobs):
                    return "a layer listed in the manifest is missing from the archive"
                return config, [b for b in blobs if b is not None]
        return "the archive holds no image manifest"
    if "manifest.json" in names:
        entry = json.loads(_blob(tar, "manifest.json") or b"[]")[0]
        config_bytes = _blob(tar, entry["Config"])
        blobs = [_blob(tar, layer) for layer in entry["Layers"]]
        if config_bytes is None or any(b is None for b in blobs):
            return "the archive is missing its config or a layer"
        return json.loads(config_bytes), [b for b in blobs if b is not None]
    return "this is not a docker save archive"


def _uncompressed(raw: bytes) -> bytes | str:
    if raw[:2] == b"\x1f\x8b":
        try:
            return gzip.decompress(raw)
        except (OSError, EOFError):
            return "a layer is a damaged gzip stream"
    if raw[:4] == b"\x28\xb5\x2f\xfd":
        return "a layer is zstd-compressed, which this audit cannot read (save the image with gzip layers)"
    return raw


def audit_archive(path: Path, *, rg_sha256: str) -> Audit:
    """Audit a `docker save` archive. `rg_sha256` is the pinned ripgrep binary's SHA-256."""
    audit = Audit()
    try:
        with tarfile.open(path) as tar:
            loaded = _layers(tar)
    except (OSError, tarfile.TarError, ValueError, KeyError, IndexError) as exc:
        audit.problems.append(f"the archive cannot be read: {exc}")
        return audit
    if isinstance(loaded, str):
        audit.problems.append(loaded)
        return audit
    config, raw_layers = loaded
    inner = config.get("config") or {}
    audit.facts.update(user=inner.get("User"), architecture=config.get("architecture"), os=config.get("os"), layers=len(raw_layers))

    if inner.get("User") != RUN_USER:
        audit.problems.append(f"the image does not run as {RUN_USER} (it runs as {inner.get('User') or 'root'})")
    for key in ("Entrypoint", "ExposedPorts", "Volumes"):
        if inner.get(key):
            audit.problems.append(f"the image sets {key}, which the runtime image must not")
    if (config.get("architecture"), config.get("os")) != ("amd64", "linux"):
        audit.problems.append("the image is not linux/amd64, which the pinned OpenCode binary needs")

    diff_ids = (config.get("rootfs") or {}).get("diff_ids") or []
    if len(diff_ids) != len(raw_layers) or len(raw_layers) < 2:
        audit.problems.append("the layer list does not match the image's config")
        return audit

    tars: list[bytes] = []
    for position, (raw, expected) in enumerate(zip(raw_layers, diff_ids, strict=True), start=1):
        data = _uncompressed(raw)
        if isinstance(data, str):
            audit.problems.append(data)
            return audit
        if len(data) > _MAX_LAYER_BYTES:
            audit.problems.append(f"layer {position} is unexpectedly large")
            return audit
        if "sha256:" + hashlib.sha256(data).hexdigest() != expected:
            audit.problems.append(f"layer {position} does not match its recorded hash")
        tars.append(data)
    audit.facts["diff_ids"] = list(diff_ids)

    _check_added_layer(audit, tars[-1], rg_sha256)
    _check_base_libraries(audit, tars[:-1])
    return audit


def _check_added_layer(audit: Audit, layer: bytes, rg_sha256: str) -> None:
    found_rg = False
    with tarfile.open(fileobj=io.BytesIO(layer)) as tar:
        for member in tar.getmembers():
            name = _name(member)
            if name not in _RG_ALLOWED:
                audit.problems.append(f"the added layer holds {name!r}, which is not ripgrep")
                continue
            if member.isdir():
                continue
            if name != RG_PATH or not member.isfile():
                audit.problems.append(f"the added layer holds {name!r} as something other than a plain file")
                continue
            found_rg = True
            handle = tar.extractfile(member)
            digest = hashlib.sha256(handle.read() if handle else b"").hexdigest()
            audit.facts["rg_sha256"] = digest
            if digest != rg_sha256:
                audit.problems.append("the ripgrep in the image is not the pinned binary (SHA-256 differs)")
            if member.mode & 0o7000:
                audit.problems.append("ripgrep is setuid, setgid or sticky in the image")
            if member.mode & 0o022:
                audit.problems.append("ripgrep is writable by group or others in the image")
            if (member.uid, member.gid) != (0, 0):
                audit.problems.append("ripgrep is not owned by root in the image")
    if not found_rg:
        audit.problems.append("the added layer does not hold usr/local/bin/rg")


def _check_base_libraries(audit: Audit, layers: list[bytes]) -> None:
    missing = set(_LIBRARIES)
    for data in layers:
        with tarfile.open(fileobj=io.BytesIO(data)) as tar:
            for member in tar.getmembers():
                for library, pattern in _LIBRARIES.items():
                    if pattern.search(_name(member)):
                        missing.discard(library)
    audit.facts["runtime_libraries"] = sorted(set(_LIBRARIES) - missing)
    if missing:
        audit.problems.append(f"the OpenCode binary needs {', '.join(sorted(missing))}, which the image's layers do not provide")
