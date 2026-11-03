"""Everything `devpilot setup` provisions, and how an installation comes to approve the image it built.

Each external input is pinned by hash and verified before use: the OpenCode tarball (its SHA-512 is pinned in
scripts/fetch_opencode.py, which this module runs unchanged) and the ripgrep archive and binary (SHA-256, below). The runtime image
is built from `Dockerfile.runtime`, exported with `docker save`, audited offline (app.cli.image_audit), and only then recorded as
approved for this machine, by its exact ID.
"""

from __future__ import annotations

import hashlib
import json
import shutil
import subprocess
import sys
import tarfile
import tempfile
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import httpx

from app.cli.image_audit import audit_archive
from app.cli.paths import Paths
from app.coding.opencode_container import ContainerPolicyError, approve_locally, verified_binary
from app.coding.opencode_contract import CONTRACT_VERSION
from app.config import REPO_ROOT

RG_VERSION = "15.1.0"
RG_URL = f"https://github.com/BurntSushi/ripgrep/releases/download/{RG_VERSION}/ripgrep-{RG_VERSION}-x86_64-unknown-linux-musl.tar.gz"
RG_MEMBER = f"ripgrep-{RG_VERSION}-x86_64-unknown-linux-musl/rg"
RG_ARCHIVE_SHA256 = "1c9297be4a084eea7ecaedf93eb03d058d6faae29bbc57ecdaf5063921491599"  # GitHub's release archive, recorded in docs/PROGRESS.md
RG_ARCHIVE_BYTES = 2_263_077
RG_SHA256 = "ebeaf56f8a25e102e9419933423738b3a2a613a444fd749d695e15eba53f71f2"  # the `rg` inside it
RG_BYTES = 5_445_512
RUNTIME_TAG = f"devpilot/opencode-runtime:portable-{CONTRACT_VERSION}"
DOCKERFILE = Path(__file__).with_name("Dockerfile.runtime")
FETCH_SCRIPT = REPO_ROOT / "backend" / "scripts" / "fetch_opencode.py"

Runner = Callable[..., "subprocess.CompletedProcess[str]"]


class ProvisionError(Exception):
    """A step failed and nothing unverified was kept. The message says what to do."""


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


# --- ripgrep ----------------------------------------------------------------------------------------


def ripgrep_path(paths: Paths) -> Path:
    return paths.ripgrep_cache / RG_VERSION / "rg"


def ripgrep_ok(paths: Paths) -> bool:
    target = ripgrep_path(paths)
    return target.is_file() and target.stat().st_size == RG_BYTES and _sha256(target.read_bytes()) == RG_SHA256


def extract_ripgrep(archive: bytes, destination: Path) -> None:
    """The one member we want, after the archive's own hash matched. Nothing else is read or written."""
    if len(archive) != RG_ARCHIVE_BYTES or _sha256(archive) != RG_ARCHIVE_SHA256:
        raise ProvisionError("the ripgrep download does not match its pinned SHA-256, so it was discarded")
    import io

    with tarfile.open(fileobj=io.BytesIO(archive), mode="r:gz") as tar:
        try:
            member = tar.getmember(RG_MEMBER)
        except KeyError as exc:
            raise ProvisionError("the ripgrep archive does not hold the expected binary") from exc
        handle = tar.extractfile(member) if member.isfile() and member.size == RG_BYTES else None
        data = handle.read() if handle else b""
    if _sha256(data) != RG_SHA256:
        raise ProvisionError("the ripgrep binary does not match its pinned SHA-256, so it was discarded")
    destination.parent.mkdir(parents=True, exist_ok=True)
    part = destination.with_suffix(".part")
    part.write_bytes(data)
    part.replace(destination)


def fetch_ripgrep(paths: Paths, *, client: httpx.Client | None = None) -> Path:
    target = ripgrep_path(paths)
    if ripgrep_ok(paths):
        return target
    owned = client is None
    http = client or httpx.Client(timeout=httpx.Timeout(30.0, read=120.0), follow_redirects=True)  # GitHub redirects; the hash is the trust
    try:
        response = http.get(RG_URL)
        if response.status_code != 200:
            raise ProvisionError(f"GitHub answered {response.status_code} for the ripgrep download ({RG_URL})")
        extract_ripgrep(response.content, target)
    except httpx.HTTPError as exc:
        raise ProvisionError(f"the ripgrep download failed ({type(exc).__name__}): check your internet connection and run setup again") from exc
    finally:
        if owned:
            http.close()
    return target


# --- OpenCode ---------------------------------------------------------------------------------------


def opencode_manifest(paths: Paths) -> Path:
    return paths.opencode_cache / CONTRACT_VERSION / "manifest.json"


def opencode_ok(paths: Paths) -> str | None:
    """None when the cached binary re-verifies against its manifest and the pinned hash; otherwise why not."""
    manifest = opencode_manifest(paths)
    if not manifest.is_file():
        return "OpenCode has not been downloaded"
    try:
        verified_binary(manifest)
    except ContainerPolicyError as exc:
        return str(exc)
    return None


def fetch_opencode(paths: Paths, *, run: Runner = subprocess.run) -> Path:
    """Run the pinned downloader unchanged: it checks the registry document, the tarball's SHA-512 and every archive entry."""
    if opencode_ok(paths) is None:
        return opencode_manifest(paths)
    paths.opencode_cache.mkdir(parents=True, exist_ok=True)
    done = run([sys.executable, "-I", str(FETCH_SCRIPT), "--cache-dir", str(paths.opencode_cache)], capture_output=True, text=True, timeout=900)
    if done.returncode != 0:
        tail = "\n".join((done.stdout + done.stderr).strip().splitlines()[-4:])
        raise ProvisionError(f"the OpenCode download or its verification failed:\n{tail}")
    problem = opencode_ok(paths)
    if problem:
        raise ProvisionError(problem)
    return opencode_manifest(paths)


# --- Docker -----------------------------------------------------------------------------------------


def docker(args: list[str], *, run: Runner = subprocess.run, timeout: float = 120.0) -> subprocess.CompletedProcess[str]:
    try:
        return run(["docker", *args], capture_output=True, text=True, timeout=timeout)
    except FileNotFoundError as exc:
        raise ProvisionError("Docker is not installed or not on PATH: install Docker Desktop and start it") from exc
    except subprocess.TimeoutExpired as exc:
        raise ProvisionError(f"docker {args[0]} did not finish in {timeout:.0f} s") from exc


def image_id(tag: str, *, run: Runner = subprocess.run) -> str | None:
    done = docker(["image", "inspect", tag, "--format", "{{.Id}}"], run=run, timeout=30)
    value = done.stdout.strip()
    return value if done.returncode == 0 and value.startswith("sha256:") and len(value) == 71 else None


def build_image(paths: Paths, *, run: Runner = subprocess.run) -> None:
    """Build the runtime image from Dockerfile.runtime and the verified rg, in a folder holding nothing else."""
    if not ripgrep_ok(paths):
        raise ProvisionError("ripgrep is not downloaded and verified yet")
    with tempfile.TemporaryDirectory(prefix="devpilot-image-", dir=paths.home) as context:
        shutil.copyfile(DOCKERFILE, Path(context) / "Dockerfile")
        shutil.copyfile(ripgrep_path(paths), Path(context) / "rg")
        done = docker(["build", "--tag", RUNTIME_TAG, "--label", f"devpilot.recipe={_sha256(DOCKERFILE.read_bytes())[:16]}", context], run=run, timeout=900)
    if done.returncode != 0:
        tail = "\n".join((done.stdout + done.stderr).strip().splitlines()[-6:])
        raise ProvisionError(f"docker build failed:\n{tail}\nThe build needs internet access to fetch python:3.12-alpine and two Alpine packages.")


@dataclass(frozen=True)
class Approval:
    image_id: str
    tag: str
    approved_at: str
    docker_server: str | None
    facts: dict[str, Any]


def audit_and_approve(paths: Paths, *, run: Runner = subprocess.run) -> Approval:
    """Export the built image, audit its contents offline, and only if every check passes record its ID as approved here."""
    current = image_id(RUNTIME_TAG, run=run)
    if current is None:
        raise ProvisionError(f"the image {RUNTIME_TAG} does not exist: build it first")
    with tempfile.TemporaryDirectory(prefix="devpilot-audit-", dir=paths.home) as scratch:
        archive = Path(scratch) / "image.tar"
        saved = docker(["save", "--output", str(archive), current], run=run, timeout=600)  # by ID, so a re-tag mid-way cannot swap it
        if saved.returncode != 0:
            raise ProvisionError(f"docker save failed: {saved.stderr.strip()[-300:]}")
        audit = audit_archive(archive, rg_sha256=RG_SHA256)
    if not audit.ok:
        raise ProvisionError("the runtime image failed its audit and was NOT approved:\n  - " + "\n  - ".join(audit.problems))
    server = docker(["version", "--format", "{{.Server.Version}}"], run=run, timeout=30).stdout.strip() or None
    approval = Approval(current, RUNTIME_TAG, datetime.now(UTC).isoformat(timespec="seconds"), server, {k: v for k, v in audit.facts.items()})
    paths.approved_runtime.parent.mkdir(parents=True, exist_ok=True)
    paths.approved_runtime.write_text(json.dumps(approval.__dict__, indent=2) + "\n", encoding="utf-8")
    return approval


def load_approval(paths: Paths, *, run: Runner = subprocess.run) -> tuple[str | None, str | None]:
    """(approved image ID, None) when the recorded approval still names the image the tag points at; else (None, why).

    The ID is a content address, so an image that is not the audited one cannot carry it. A re-tag or rebuild gives a different ID
    and is refused until setup audits it again. On success the ID is registered for this process (`approve_locally`)."""
    if not paths.approved_runtime.is_file():
        return None, "the runtime image has not been audited and approved: run setup"
    try:
        record = json.loads(paths.approved_runtime.read_text(encoding="utf-8"))
        recorded = str(record["image_id"])
    except (OSError, ValueError, KeyError):
        return None, "the approval record is unreadable: run setup again"
    if record.get("tag") != RUNTIME_TAG:
        return None, "the approval record is for another image: run setup again"
    current = image_id(RUNTIME_TAG, run=run)
    if current is None:
        return None, f"the image {RUNTIME_TAG} is not present (is Docker running?)"
    if current != recorded:
        return None, f"the image {RUNTIME_TAG} is not the one that was audited (it was rebuilt or replaced): run setup to audit it again"
    approve_locally(current)
    return current, None
