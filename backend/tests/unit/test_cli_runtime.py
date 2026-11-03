"""Provisioning and the image approval: pinned downloads, the image build, and how an installation comes to approve its image.
Offline: HTTP is a mock transport, Docker is a fake runner, and the archives are synthetic."""

from __future__ import annotations

import io
import json
import subprocess
import tarfile
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import httpx
import pytest

from app.cli import runtime
from app.cli.paths import Paths, paths as make_paths
from app.coding import opencode_container
from tests.unit.test_cli_image_audit import BASE, BASE_NO_LIBS, RG, RG_SHA, archive, rg_layer

GOOD_ID = "sha256:" + "a" * 64
OTHER_ID = "sha256:" + "b" * 64


@pytest.fixture(autouse=True)
def _forget_local_approvals() -> Iterator[None]:
    yield
    opencode_container._LOCALLY_APPROVED.clear()


@pytest.fixture
def p(tmp_path: Path) -> Paths:
    where = make_paths(tmp_path / "home")
    where.ensure()
    return where


def rg_tarball() -> bytes:
    out = io.BytesIO()
    with tarfile.open(fileobj=out, mode="w:gz") as tar:
        info = tarfile.TarInfo(runtime.RG_MEMBER)
        info.size, info.mode = len(RG), 0o755
        tar.addfile(info, io.BytesIO(RG))
    return out.getvalue()


@pytest.fixture
def pinned(monkeypatch: pytest.MonkeyPatch) -> bytes:
    """The ripgrep pins replaced with those of a synthetic release, so the real verification code runs on synthetic bytes."""
    data = rg_tarball()
    monkeypatch.setattr(runtime, "RG_ARCHIVE_SHA256", runtime._sha256(data))
    monkeypatch.setattr(runtime, "RG_ARCHIVE_BYTES", len(data))
    monkeypatch.setattr(runtime, "RG_SHA256", RG_SHA)
    monkeypatch.setattr(runtime, "RG_BYTES", len(RG))
    return data


def client_for(handler: Any) -> httpx.Client:
    return httpx.Client(transport=httpx.MockTransport(handler), follow_redirects=True)


# --- ripgrep ----------------------------------------------------------------------------------------


def test_the_real_pins_refuse_anything_that_is_not_the_published_release(p: Paths) -> None:
    with pytest.raises(runtime.ProvisionError, match="pinned SHA-256"):
        runtime.extract_ripgrep(rg_tarball(), runtime.ripgrep_path(p))
    assert not runtime.ripgrep_path(p).exists() and not runtime.ripgrep_ok(p)


def test_a_verified_download_is_kept_and_a_second_run_needs_no_network(p: Paths, pinned: bytes) -> None:
    calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(str(request.url))
        return httpx.Response(200, content=pinned)

    target = runtime.fetch_ripgrep(p, client=client_for(handler))
    assert target.read_bytes() == RG and not target.with_suffix(".part").exists()
    assert runtime.ripgrep_ok(p)
    runtime.fetch_ripgrep(p, client=client_for(lambda r: pytest.fail("a cached, verified binary must not be downloaded again")))
    assert calls == [runtime.RG_URL] and runtime.RG_URL.startswith("https://github.com/BurntSushi/ripgrep/releases/download/")


def test_a_download_that_is_tampered_with_is_discarded_even_if_it_is_a_valid_archive(p: Paths, pinned: bytes, monkeypatch: pytest.MonkeyPatch) -> None:
    tampered = io.BytesIO()
    with tarfile.open(fileobj=tampered, mode="w:gz") as tar:
        info = tarfile.TarInfo(runtime.RG_MEMBER)
        info.size = len(RG)
        tar.addfile(info, io.BytesIO(b"X" * len(RG)))  # same size, different bytes
    monkeypatch.setattr(runtime, "RG_ARCHIVE_SHA256", runtime._sha256(tampered.getvalue()))  # even an archive whose own hash was updated...
    monkeypatch.setattr(runtime, "RG_ARCHIVE_BYTES", len(tampered.getvalue()))
    with pytest.raises(runtime.ProvisionError, match="binary does not match its pinned SHA-256"):  # ...fails on the binary's hash
        runtime.fetch_ripgrep(p, client=client_for(lambda r: httpx.Response(200, content=tampered.getvalue())))
    assert not runtime.ripgrep_path(p).exists()


def test_download_failures_say_what_to_do(p: Paths, pinned: bytes) -> None:
    with pytest.raises(runtime.ProvisionError, match="answered 404"):
        runtime.fetch_ripgrep(p, client=client_for(lambda r: httpx.Response(404)))

    def offline(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("no route", request=request)

    with pytest.raises(runtime.ProvisionError, match="internet connection"):
        runtime.fetch_ripgrep(p, client=client_for(offline))


def test_a_release_without_the_binary_is_refused(p: Paths, monkeypatch: pytest.MonkeyPatch) -> None:
    out = io.BytesIO()
    with tarfile.open(fileobj=out, mode="w:gz") as tar:
        info = tarfile.TarInfo("ripgrep/README")
        info.size = 1
        tar.addfile(info, io.BytesIO(b"x"))
    monkeypatch.setattr(runtime, "RG_ARCHIVE_SHA256", runtime._sha256(out.getvalue()))
    monkeypatch.setattr(runtime, "RG_ARCHIVE_BYTES", len(out.getvalue()))
    with pytest.raises(runtime.ProvisionError, match="does not hold the expected binary"):
        runtime.extract_ripgrep(out.getvalue(), runtime.ripgrep_path(p))


# --- OpenCode ---------------------------------------------------------------------------------------


def test_opencode_is_fetched_by_the_pinned_downloader_run_unchanged_and_isolated(p: Paths, monkeypatch: pytest.MonkeyPatch) -> None:
    seen: list[list[str]] = []
    state = {"ok": False}

    def fake_run(command: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        seen.append(command)
        state["ok"] = True
        return subprocess.CompletedProcess(command, 0, "", "")

    monkeypatch.setattr(runtime, "opencode_ok", lambda paths: None if state["ok"] else "OpenCode has not been downloaded")
    assert runtime.fetch_opencode(p, run=fake_run) == runtime.opencode_manifest(p)
    command = seen[0]
    assert command[1] == "-I" and command[2].endswith("fetch_opencode.py") and command[3:] == ["--cache-dir", str(p.opencode_cache)]
    runtime.fetch_opencode(p, run=lambda *a, **k: pytest.fail("a verified binary must not be fetched again"))


def test_a_failed_opencode_download_reports_its_last_lines(p: Paths) -> None:
    def failing(command: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess(command, 1, "", "line one\nSTOPPED, verification failed: SHA-512 integrity mismatch")

    with pytest.raises(runtime.ProvisionError, match="SHA-512 integrity mismatch"):
        runtime.fetch_opencode(p, run=failing)


def test_opencode_is_not_trusted_without_its_manifest(p: Paths) -> None:
    assert runtime.opencode_ok(p) == "OpenCode has not been downloaded"


# --- Docker -----------------------------------------------------------------------------------------


class FakeDocker:
    """Answers `docker` calls from a table, and records them."""

    def __init__(self, archive_path: Path | None = None, *, current: str | None = GOOD_ID, build_fails: bool = False) -> None:
        self.calls: list[list[str]] = []
        self.archive_path = archive_path
        self.current = current
        self.build_fails = build_fails
        self.context_files: list[str] = []

    def __call__(self, command: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        args = command[1:]
        self.calls.append(args)
        ok = lambda out="": subprocess.CompletedProcess(command, 0, out, "")  # noqa: E731
        if args[:2] == ["image", "inspect"]:
            return ok(self.current + "\n") if self.current else subprocess.CompletedProcess(command, 1, "", "No such image")
        if args[0] == "build":
            self.context_files = sorted(f.name for f in Path(args[-1]).iterdir())
            return subprocess.CompletedProcess(command, 1, "step 1\nERROR: apk fetch failed", "") if self.build_fails else ok()
        if args[0] == "save":
            assert self.archive_path is not None
            Path(args[args.index("--output") + 1]).write_bytes(self.archive_path.read_bytes())
            return ok()
        if args[0] == "version":
            return ok("29.9.2\n")
        return ok()


def test_image_ids_are_read_only_when_they_are_full_ids() -> None:
    assert runtime.image_id("t", run=FakeDocker(current=GOOD_ID)) == GOOD_ID
    assert runtime.image_id("t", run=FakeDocker(current=None)) is None
    assert runtime.image_id("t", run=FakeDocker(current="sha256:short")) is None


def test_docker_missing_or_hung_is_a_plain_message() -> None:
    def missing(*a: Any, **k: Any) -> Any:
        raise FileNotFoundError

    def hung(*a: Any, **k: Any) -> Any:
        raise subprocess.TimeoutExpired("docker", 1)

    with pytest.raises(runtime.ProvisionError, match="not installed"):
        runtime.docker(["ps"], run=missing)
    with pytest.raises(runtime.ProvisionError, match="did not finish"):
        runtime.docker(["ps"], run=hung)


def test_the_image_is_built_from_a_folder_holding_only_the_dockerfile_and_the_verified_rg(p: Paths, pinned: bytes) -> None:
    runtime.fetch_ripgrep(p, client=client_for(lambda r: httpx.Response(200, content=pinned)))
    docker = FakeDocker()
    runtime.build_image(p, run=docker)
    build = next(c for c in docker.calls if c[0] == "build")
    assert build[1:3] == ["--tag", runtime.RUNTIME_TAG] and docker.context_files == ["Dockerfile", "rg"]
    assert runtime.RUNTIME_TAG != "devpilot/opencode-runtime:1.19.5"  # never re-points the tag a person approved by hand


def test_a_build_needs_the_verified_ripgrep_and_reports_a_failed_build(p: Paths, pinned: bytes) -> None:
    with pytest.raises(runtime.ProvisionError, match="ripgrep is not downloaded"):
        runtime.build_image(p, run=FakeDocker())
    runtime.fetch_ripgrep(p, client=client_for(lambda r: httpx.Response(200, content=pinned)))
    with pytest.raises(runtime.ProvisionError, match="apk fetch failed"):
        runtime.build_image(p, run=FakeDocker(build_fails=True))


def test_the_dockerfile_builds_what_the_audit_expects() -> None:
    text = runtime.DOCKERFILE.read_text(encoding="utf-8")
    lines = [line for line in text.splitlines() if line and not line.startswith("#")]
    assert lines[0] == "FROM python:3.12-alpine" and "USER 65534:65534" in lines
    assert lines[-1].startswith("COPY ") and lines[-1].endswith("/usr/local/bin/rg")  # last, so the audit's "final layer is only rg" holds
    assert not any(line.startswith(("ADD ", "EXPOSE", "VOLUME", "ENTRYPOINT")) for line in lines)


# --- Approval ---------------------------------------------------------------------------------------


def test_an_image_that_passes_its_audit_is_recorded_and_then_approved_for_this_process(p: Paths, tmp_path: Path, pinned: bytes) -> None:
    docker = FakeDocker(archive(tmp_path / "ok.tar", [BASE, rg_layer()]))
    assert runtime.load_approval(p, run=docker) == (None, "the runtime image has not been audited and approved: run setup")
    approval = runtime.audit_and_approve(p, run=docker)
    assert approval.image_id == GOOD_ID and approval.tag == runtime.RUNTIME_TAG and approval.docker_server == "29.9.2"
    assert next(c for c in docker.calls if c[0] == "save")[-1] == GOOD_ID  # exported by ID: a re-tag mid-way cannot swap it
    record = json.loads(p.approved_runtime.read_text(encoding="utf-8"))
    assert record["image_id"] == GOOD_ID and record["facts"]["rg_sha256"] == RG_SHA

    assert GOOD_ID not in opencode_container.approved_runtime_images()
    assert runtime.load_approval(p, run=docker) == (GOOD_ID, None)
    assert GOOD_ID in opencode_container.approved_runtime_images()
    assert opencode_container.APPROVED_RUNTIME_IMAGES == frozenset({"sha256:a7753fe443915eed28c577c1d1b3f0e27ab1c5bd16b7debc7de9579d2aaefe99"})  # the built-in pin is untouched


def test_an_image_that_fails_its_audit_is_not_approved_and_nothing_is_recorded(p: Paths, tmp_path: Path, pinned: bytes) -> None:
    docker = FakeDocker(archive(tmp_path / "bad.tar", [BASE_NO_LIBS, rg_layer()]))
    with pytest.raises(runtime.ProvisionError, match="NOT approved"):
        runtime.audit_and_approve(p, run=docker)
    assert not p.approved_runtime.exists() and not opencode_container.approved_runtime_images() - opencode_container.APPROVED_RUNTIME_IMAGES


def test_a_rebuilt_or_replaced_image_loses_its_approval_until_it_is_audited_again(p: Paths, tmp_path: Path, pinned: bytes) -> None:
    runtime.audit_and_approve(p, run=FakeDocker(archive(tmp_path / "ok.tar", [BASE, rg_layer()])))
    approved, why = runtime.load_approval(p, run=FakeDocker(current=OTHER_ID))
    assert approved is None and "was rebuilt or replaced" in (why or "")
    assert OTHER_ID not in opencode_container.approved_runtime_images() and GOOD_ID not in opencode_container.approved_runtime_images()
    assert runtime.load_approval(p, run=FakeDocker(current=None))[0] is None


def test_a_damaged_or_foreign_record_approves_nothing(p: Paths) -> None:
    p.approved_runtime.write_text("{ not json", encoding="utf-8")
    assert "unreadable" in (runtime.load_approval(p, run=FakeDocker())[1] or "")
    p.approved_runtime.write_text(json.dumps({"image_id": GOOD_ID, "tag": "someone/else:1"}), encoding="utf-8")
    assert "another image" in (runtime.load_approval(p, run=FakeDocker())[1] or "")
    assert not opencode_container.approved_runtime_images() - opencode_container.APPROVED_RUNTIME_IMAGES


def test_only_a_full_image_id_can_be_approved_locally() -> None:
    for bad in ("sha256:abc", "latest", "SHA256:" + "A" * 64, ""):
        with pytest.raises(ValueError):
            opencode_container.approve_locally(bad)


def test_the_launcher_accepts_a_locally_approved_image_and_nothing_else(tmp_path: Path) -> None:
    from app.coding.launcher import config_problems
    from app.core.egress import EgressPolicy
    from tests.helpers import make_settings

    settings = make_settings(tmp_path, opencode_enabled=True, coding_live_approved=True, opencode_runtime_image=GOOD_ID)
    egress = EgressPolicy.from_file(settings.egress_allowlist_file)
    assert any("no approved runtime image" in problem for problem in config_problems(settings, egress))
    opencode_container.approve_locally(GOOD_ID)
    assert not any("no approved runtime image" in problem for problem in config_problems(settings, egress))
    other = make_settings(tmp_path, opencode_enabled=True, coding_live_approved=True, opencode_runtime_image=OTHER_ID)
    assert any("no approved runtime image" in problem for problem in config_problems(other, egress))
