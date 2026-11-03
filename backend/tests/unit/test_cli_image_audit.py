"""The offline image audit, on synthetic `docker save` archives in both layouts. Nothing is run; no Docker is needed."""

from __future__ import annotations

import gzip
import hashlib
import io
import json
import tarfile
from pathlib import Path
from typing import Any

import pytest

from app.cli.image_audit import RG_PATH, RUN_USER, audit_archive

RG = b"\x7fELF fake ripgrep binary"
RG_SHA = hashlib.sha256(RG).hexdigest()


def _tar(members: list[tuple[str, bytes | None, dict[str, Any]]]) -> bytes:
    out = io.BytesIO()
    with tarfile.open(fileobj=out, mode="w") as tar:
        for name, content, options in members:
            info = tarfile.TarInfo(name)
            info.mode = options.get("mode", 0o755 if content is None else 0o644)
            info.uid, info.gid = options.get("uid", 0), options.get("gid", 0)
            if content is None:
                info.type = tarfile.DIRTYPE
                tar.addfile(info)
            else:
                info.size = len(content)
                tar.addfile(info, io.BytesIO(content))
    return out.getvalue()


BASE = _tar([("usr", None, {}), ("usr/lib", None, {}), ("usr/lib/libstdc++.so.6.0.33", b"x", {}), ("usr/lib/libgcc_s.so.1", b"y", {})])
BASE_NO_LIBS = _tar([("usr", None, {}), ("etc", None, {}), ("etc/os-release", b"alpine", {})])


def rg_layer(**rg_options: Any) -> bytes:
    return _tar([("usr", None, {}), ("usr/local", None, {}), ("usr/local/bin", None, {}), (RG_PATH, RG, rg_options)])


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def archive(
    path: Path, layers: list[bytes], *, user: str | None = RUN_USER, config_extra: dict[str, Any] | None = None, layout: str = "oci",
    compress: str = "gzip", tamper_diff_id: bool = False, arch: str = "amd64",
) -> Path:  # fmt: skip
    diff_ids = [f"sha256:{_sha(layer)}" for layer in layers]
    if tamper_diff_id:
        diff_ids[0] = "sha256:" + "0" * 64
    stored = [gzip.compress(layer, mtime=0) if compress == "gzip" else (b"\x28\xb5\x2f\xfd" + layer) if compress == "zstd" else layer for layer in layers]
    cfg: dict[str, Any] = {"architecture": arch, "os": "linux", "config": {"Cmd": ["python3"], **({"User": user} if user else {}), **(config_extra or {})}, "rootfs": {"type": "layers", "diff_ids": diff_ids}}
    config_bytes = json.dumps(cfg).encode()
    members: dict[str, bytes] = {}
    if layout == "oci":
        for blob in stored:
            members[f"blobs/sha256/{_sha(blob)}"] = blob
        members[f"blobs/sha256/{_sha(config_bytes)}"] = config_bytes
        manifest = json.dumps({"schemaVersion": 2, "config": {"mediaType": "application/vnd.oci.image.config.v1+json", "digest": f"sha256:{_sha(config_bytes)}"},
                               "layers": [{"mediaType": "application/vnd.oci.image.layer.v1.tar+gzip", "digest": f"sha256:{_sha(b)}", "size": len(b)} for b in stored]}).encode()
        attestation = json.dumps({"schemaVersion": 2, "config": {"mediaType": "application/vnd.oci.empty.v1+json", "digest": "sha256:" + "e" * 64}, "layers": []}).encode()
        members[f"blobs/sha256/{_sha(manifest)}"] = manifest
        members[f"blobs/sha256/{_sha(attestation)}"] = attestation
        inner = json.dumps({"schemaVersion": 2, "mediaType": "application/vnd.oci.image.index.v1+json", "manifests": [
            {"digest": f"sha256:{_sha(attestation)}", "platform": {"architecture": "unknown", "os": "unknown"}, "annotations": {"vnd.docker.reference.type": "attestation-manifest"}},
            {"digest": f"sha256:{_sha(manifest)}", "platform": {"architecture": "amd64", "os": "linux"}},
        ]}).encode()
        members[f"blobs/sha256/{_sha(inner)}"] = inner
        members["index.json"] = json.dumps({"schemaVersion": 2, "manifests": [{"digest": f"sha256:{_sha(inner)}"}]}).encode()
    else:  # the older manifest.json layout
        members["config.json"] = config_bytes
        for number, blob in enumerate(stored):
            members[f"layer{number}/layer.tar"] = blob
        members["manifest.json"] = json.dumps([{"Config": "config.json", "Layers": [f"layer{n}/layer.tar" for n in range(len(stored))]}]).encode()
    with tarfile.open(path, "w") as tar:
        for name, body in members.items():
            info = tarfile.TarInfo(name)
            info.size = len(body)
            tar.addfile(info, io.BytesIO(body))
    return path


@pytest.mark.parametrize("layout", ["oci", "manifest"])
def test_an_image_built_to_the_recipe_passes_in_both_archive_layouts(tmp_path: Path, layout: str) -> None:
    audit = audit_archive(archive(tmp_path / "i.tar", [BASE, rg_layer()], layout=layout), rg_sha256=RG_SHA)
    assert audit.ok, audit.problems
    assert audit.facts["user"] == RUN_USER and audit.facts["rg_sha256"] == RG_SHA and audit.facts["runtime_libraries"] == ["libgcc_s", "libstdc++"]


def test_uncompressed_layers_are_read_too(tmp_path: Path) -> None:
    assert audit_archive(archive(tmp_path / "i.tar", [BASE, rg_layer()], compress="none"), rg_sha256=RG_SHA).ok


def test_a_ripgrep_that_is_not_the_pinned_binary_is_refused(tmp_path: Path) -> None:
    audit = audit_archive(archive(tmp_path / "i.tar", [BASE, rg_layer()]), rg_sha256=hashlib.sha256(b"another").hexdigest())
    assert not audit.ok and any("not the pinned binary" in p for p in audit.problems)


def test_an_image_that_runs_as_root_or_exposes_anything_is_refused(tmp_path: Path) -> None:
    for user in (None, "0:0", "1000:1000"):
        assert any("does not run as" in p for p in audit_archive(archive(tmp_path / f"u{user}.tar".replace(":", "_"), [BASE, rg_layer()], user=user), rg_sha256=RG_SHA).problems)
    for extra in ({"Entrypoint": ["/bin/sh"]}, {"ExposedPorts": {"22/tcp": {}}}, {"Volumes": {"/data": {}}}):
        audit = audit_archive(archive(tmp_path / "x.tar", [BASE, rg_layer()], config_extra=extra), rg_sha256=RG_SHA)
        assert any("must not" in p for p in audit.problems), extra


def test_a_wrong_platform_is_refused(tmp_path: Path) -> None:
    assert any("linux/amd64" in p for p in audit_archive(archive(tmp_path / "i.tar", [BASE, rg_layer()], arch="arm64"), rg_sha256=RG_SHA).problems)


def test_the_added_layer_may_hold_only_ripgrep(tmp_path: Path) -> None:
    sneaky = _tar([("usr", None, {}), ("usr/local", None, {}), ("usr/local/bin", None, {}), (RG_PATH, RG, {"mode": 0o755}), ("usr/local/bin/sh", b"#!", {})])
    audit = audit_archive(archive(tmp_path / "i.tar", [BASE, sneaky]), rg_sha256=RG_SHA)
    assert any("usr/local/bin/sh" in p and "not ripgrep" in p for p in audit.problems)
    assert any("does not hold usr/local/bin/rg" in p for p in audit_archive(archive(tmp_path / "j.tar", [BASE, _tar([("usr", None, {})])]), rg_sha256=RG_SHA).problems)


@pytest.mark.parametrize(("options", "expected"), [({"mode": 0o4755}, "setuid"), ({"mode": 0o775}, "writable"), ({"uid": 1000}, "not owned by root"), ({"gid": 1000}, "not owned by root")])
def test_ripgrep_must_be_root_owned_not_setuid_and_not_writable(tmp_path: Path, options: dict[str, int], expected: str) -> None:
    audit = audit_archive(archive(tmp_path / "i.tar", [BASE, rg_layer(**options)]), rg_sha256=RG_SHA)
    assert any(expected in p for p in audit.problems), audit.problems


def test_the_layers_beneath_must_provide_the_runtime_libraries_opencode_needs(tmp_path: Path) -> None:
    audit = audit_archive(archive(tmp_path / "i.tar", [BASE_NO_LIBS, rg_layer()]), rg_sha256=RG_SHA)
    assert any("libgcc_s, libstdc++" in p for p in audit.problems)


def test_a_layer_that_does_not_match_its_recorded_hash_is_refused(tmp_path: Path) -> None:
    audit = audit_archive(archive(tmp_path / "i.tar", [BASE, rg_layer()], tamper_diff_id=True), rg_sha256=RG_SHA)
    assert any("does not match its recorded hash" in p for p in audit.problems)


def test_a_layer_the_audit_cannot_read_is_a_refusal_not_a_pass(tmp_path: Path) -> None:
    audit = audit_archive(archive(tmp_path / "i.tar", [BASE, rg_layer()], compress="zstd"), rg_sha256=RG_SHA)
    assert not audit.ok and any("zstd" in p for p in audit.problems)


def test_garbage_and_missing_files_are_refusals(tmp_path: Path) -> None:
    (tmp_path / "junk.tar").write_bytes(b"not a tar at all")
    assert not audit_archive(tmp_path / "junk.tar", rg_sha256=RG_SHA).ok
    assert not audit_archive(tmp_path / "missing.tar", rg_sha256=RG_SHA).ok
    empty = tmp_path / "empty.tar"
    with tarfile.open(empty, "w"):
        pass
    assert any("not a docker save archive" in p for p in audit_archive(empty, rg_sha256=RG_SHA).problems)


def test_a_single_layer_image_is_refused(tmp_path: Path) -> None:
    assert not audit_archive(archive(tmp_path / "i.tar", [rg_layer()]), rg_sha256=RG_SHA).ok
