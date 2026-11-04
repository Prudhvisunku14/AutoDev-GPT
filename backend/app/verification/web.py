"""Website verification: install, type check, build, serve, HTTP and asset checks, and a real headless browser, in Docker.

One component (a static site or a React + Vite app) is verified from a disposable copy, never the person's files:

1. **install** (React + Vite only): `devpilot/verify-web` runs DevPilot's fixed installer (`install-deps.mjs`) on an
   *internal* Docker network. Its only way out is DevPilot's install proxy (`edge/install_proxy.py`, in the approved
   python:3.12-alpine image), which tunnels to the hosts of the egress allowlist's `sandbox_install` component on port 443
   and nothing else. npm runs with lifecycle scripts disabled, so no package code runs while a network is reachable. A
   project's own .npmrc/.yarnrc is removed first, and dependencies from git, URLs or files are refused.
2. **verify**: the same image with `--network none` runs the fixed verifier (`verify-web.mjs`): tsc, `vite build` into
   DevPilot's own output folder, DevPilot's static server, HTTP and asset checks, and Chromium (rendering, uncaught errors,
   failed requests, console errors, same-origin links, button clicks, and the person's required checks when given).

Every container drops all capabilities, cannot gain privileges, has a read-only root, a process and memory limit, and
runs as the image's unprivileged user. Containers and networks are removed afterwards, whatever happens. The verdict is
PASSED only when every stage passed *and* the browser really ran; anything unread, missing or cut short is FAILED or
NOT_RUN, never PASSED. A project's own scripts (`npm run build`, `preview`, `test`) are never run.
"""

from __future__ import annotations

import json
import os
import re
import secrets
import shutil
import subprocess
import threading
import time
import copy as copying
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field, replace
from pathlib import Path, PurePosixPath
from typing import Any

from app.core.egress import EgressPolicy
from app.sandbox.process import SubprocessRunner

WEB_IMAGE_TAG = "devpilot/verify-web:1"
PROXY_IMAGE = "python:3.12-alpine"  # the approved sandbox image; the proxy is standard-library Python
IMAGE_CONTEXT = Path(__file__).resolve().parents[2] / "sandbox_images" / "verify-web"
PROXY_SOURCE = Path(__file__).resolve().parents[2] / "edge" / "install_proxy.py"
WEB_KINDS = ("static-html", "react-vite")
LABEL = "devpilot.web"
_REPORT = re.compile(r"^DEVPILOT_WEB_REPORT (\{.*\})\s*$", re.MULTILINE)
_INSTALL = re.compile(r"^DEVPILOT_INSTALL_REPORT (\{.*\})\s*$", re.MULTILINE)
# Never copied into the disposable folder: package-manager configuration (it could point npm elsewhere or carry a token),
# version control, and anything a previous install or build left behind.
_EXCLUDED_DIRS = {"node_modules", ".git", "dist", "build", ".next", ".cache", ".vite", ".turbo", "coverage"}
_EXCLUDED_FILES = {".npmrc", ".yarnrc", ".yarnrc.yml", ".pnpmfile.cjs", "bunfig.toml"}
MANDATORY = {"react-vite": ("manifest", "installed", "typecheck", "build", "serve", "http", "assets", "browser"), "static-html": ("serve", "http", "assets", "browser")}

Runner = Callable[..., subprocess.CompletedProcess[str]]

# This process's approval of a verify-web image, by exact ID: set only after `devpilot setup --web` built it and its self-test
# passed (app.cli.webruntime). Nothing else sets it, and each verification re-reads the tag's ID and refuses on any difference.
_APPROVED: set[str] = set()


def approve_locally(image_id: str) -> None:
    if not re.fullmatch(r"sha256:[0-9a-f]{64}", image_id):
        raise ValueError("an approval names a full sha256 image ID")
    _APPROVED.add(image_id)


def approved_image_ids() -> frozenset[str]:
    return frozenset(_APPROVED)


class WebVerificationError(Exception):
    pass


class _Interrupted(Exception):
    def __init__(self, status: str) -> None:
        self.status = status


@dataclass(frozen=True)
class Stage:
    name: str
    ok: bool
    detail: str
    skipped: bool = False


@dataclass(frozen=True)
class WebResult:
    verdict: str  # PASSED | FAILED | NOT_RUN
    reason: str
    kind: str
    stages: tuple[Stage, ...] = ()
    pages: tuple[dict[str, Any], ...] = ()
    blocked_external: tuple[str, ...] = ()
    install: dict[str, Any] | None = None
    proxy_audit: tuple[dict[str, Any], ...] = ()
    image_id: str | None = None
    output_tail: str = ""
    evidence: dict[str, Any] = field(default_factory=dict)
    exec_status: str = "SUCCEEDED"
    cleanup: str = "not_needed"
    duration_ms: int = 0

    @property
    def passed(self) -> bool:
        return self.verdict == "PASSED"

    def to_dict(self) -> dict[str, Any]:
        return {
            "verdict": self.verdict, "reason": self.reason, "kind": self.kind, "image_id": self.image_id,
            "stages": [{"name": s.name, "ok": s.ok, "detail": s.detail, "skipped": s.skipped} for s in self.stages],
            "pages": list(self.pages), "blocked_external": list(self.blocked_external), "install": self.install, "proxy_audit": list(self.proxy_audit),
            "evidence": self.evidence, "exec_status": self.exec_status, "cleanup": self.cleanup, "duration_ms": self.duration_ms,
        }  # fmt: skip

    def describe(self) -> str:
        lines = [f"Website verification: {self.verdict} — {self.reason}"]
        for s in self.stages:
            mark = "skip" if s.skipped else ("ok  " if s.ok else "FAIL")
            lines.append(f"  [{mark}] {s.name}: {s.detail.splitlines()[0] if s.detail else ''}")
        if self.blocked_external:
            lines.append(f"  Requests to other sites were blocked (there is no network during verification): {len(self.blocked_external)}, e.g. {self.blocked_external[0]}")
        return "\n".join(lines)


def judge(kind: str, stdout: str, *, install: dict[str, Any] | None = None, image_id: str | None = None) -> WebResult:
    """The verifier's own report, checked again here. PASSED needs every mandatory stage present and passed, and the browser run."""
    tail = stdout[-4_000:]
    if install is not None and install.get("ok") is not True:
        return WebResult("FAILED", f"dependency install failed: {str(install.get('detail', ''))[-600:]}", kind, install=install, image_id=image_id, output_tail=tail)
    match = list(_REPORT.finditer(stdout))
    if not match:
        return WebResult("NOT_RUN", "the verifier produced no report (it may have been killed by a limit)", kind, install=install, image_id=image_id, output_tail=tail)
    try:
        data = json.loads(match[-1].group(1))
    except ValueError:
        return WebResult("NOT_RUN", "the verifier's report could not be read", kind, install=install, image_id=image_id, output_tail=tail)
    if not isinstance(data, dict) or not isinstance(data.get("stages"), list) or not isinstance(data.get("pages", []), list):
        return WebResult("NOT_RUN", "the verifier's report has an invalid shape", kind, image_id=image_id, output_tail=tail)
    stages = tuple(Stage(str(s.get("name")), s.get("ok") is True, str(s.get("detail", "")), bool(s.get("skipped"))) for s in data.get("stages", []) if isinstance(s, dict))
    by_name = {s.name: s for s in stages}
    common = {"kind": kind, "stages": stages, "pages": tuple(p for p in data.get("pages", []) if isinstance(p, dict)), "blocked_external": tuple(str(u) for u in data.get("blocked_external", [])),
              "install": install, "image_id": image_id, "output_tail": tail}  # fmt: skip
    failed = next((s for s in stages if not s.ok), None)
    if failed:
        return WebResult("FAILED", f"{failed.name}: {failed.detail.splitlines()[0] if failed.detail else 'failed'}", **common)
    missing = [name for name in MANDATORY.get(kind, ()) if name not in by_name or by_name[name].skipped]
    if missing:
        return WebResult("FAILED", f"stages did not run: {', '.join(missing)}", **common)
    browser = next((s for s in data.get("stages", []) if isinstance(s, dict) and s.get("name") == "browser"), {})
    if browser.get("ran") is not True or not common["pages"]:
        return WebResult("FAILED", "the browser did not run, so the pages were not seen", **common)
    if kind == "react-vite" and (install is None or install.get("ok") is not True):
        return WebResult("FAILED", "dependencies were not installed by DevPilot's installer", **common)
    if data.get("verdict") != "PASSED":
        return WebResult("FAILED", str(data.get("reason") or "the verifier did not report a pass"), **common)
    return WebResult("PASSED", "every stage passed, including a real browser", **common)


def _report_json(pattern: re.Pattern[str], text: str) -> dict[str, Any] | None:
    found = list(pattern.finditer(text))
    if not found:
        return None
    try:
        data = json.loads(found[-1].group(1))
    except ValueError:
        return None
    return data if isinstance(data, dict) else None


def prepare_copy(files: Mapping[str, bytes], dest: Path, *, component: str = "") -> list[str]:
    """Write one component's files into an empty folder, leaving out package-manager configuration and build leftovers.
    Returns what was left out. Paths are re-checked so nothing lands outside `dest`."""
    if dest.exists() and any(dest.iterdir()):
        raise WebVerificationError(f"{dest} is not empty")
    dest.mkdir(parents=True, exist_ok=True)
    prefix = f"{component}/" if component else ""
    skipped: list[str] = []
    base = dest.resolve()
    for rel, data in sorted(files.items()):
        if not rel.startswith(prefix):
            continue
        inner = rel[len(prefix):]
        parts = PurePosixPath(inner).parts
        if not parts or any(p in ("", ".", "..") for p in parts) or inner.startswith("/") or "\\" in inner or ":" in inner:
            raise WebVerificationError(f"{rel!r} is not a safe relative path")
        if any(p in _EXCLUDED_DIRS for p in parts[:-1]) or parts[-1] in _EXCLUDED_FILES:
            skipped.append(inner)
            continue
        target = (dest / inner).resolve()
        if base not in target.parents:
            raise WebVerificationError(f"{rel!r} would land outside the copy")
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(data)
    return skipped


def read_folder(folder: Path, *, max_files: int = 20_000, max_bytes: int = 300_000_000) -> dict[str, bytes]:
    """A local project's files (links and excluded folders are not followed), for verifying a folder on disk."""
    files: dict[str, bytes] = {}
    total = 0
    root = folder.resolve()
    for current, dirs, names in os.walk(root, followlinks=False):
        dirs[:] = sorted(d for d in dirs if d not in _EXCLUDED_DIRS and not (Path(current) / d).is_symlink())  # never descended into
        for name in sorted(names):
            path = Path(current) / name
            if path.is_symlink() or not path.is_file():
                continue
            data = path.read_bytes()
            total += len(data)
            if len(files) >= max_files or total > max_bytes:
                raise WebVerificationError(f"{folder} is over the limit of {max_files} files or {max_bytes} bytes")
            files[path.relative_to(root).as_posix()] = data
    return files


def _hardening(*, memory: str, pids: int) -> list[str]:
    return ["--cap-drop", "ALL", "--security-opt", "no-new-privileges", "--read-only", "--pids-limit", str(pids), "--memory", memory, "--memory-swap", memory]


class WebVerifier:
    """Runs the stages on Docker. `run` is subprocess.run-like, so the tests drive it without Docker."""

    def __init__(self, egress: EgressPolicy, *, docker: str = "docker", run: Runner = subprocess.run, work_dir: Path, image: str = WEB_IMAGE_TAG,
                 memory: str = "2g", cpus: float = 2.0, timeout: float = 900.0, pids: int = 512, capture_bytes: int = 65_536) -> None:  # fmt: skip
        self._egress = egress
        self._docker = docker
        self._run = run
        self._work = work_dir
        self._image = image
        self._memory, self._cpus, self._timeout = memory, cpus, timeout
        self._pids, self._capture_bytes = pids, capture_bytes
        self._cancel: threading.Event | None = None
        self._deadline: float | None = None
        self._cleaning = False

    def _docker_call(self, args: list[str], *, timeout: float = 120.0) -> subprocess.CompletedProcess[str]:
        if not self._cleaning:
            if self._cancel is not None and self._cancel.is_set():
                raise _Interrupted("CANCELLED")
            if self._deadline is not None:
                timeout = min(timeout, self._deadline - time.monotonic())
                if timeout <= 0:
                    raise _Interrupted("TIMEOUT")
        if self._run is subprocess.run:
            result = SubprocessRunner().run([self._docker, *args], timeout=timeout, cancel=None if self._cleaning else self._cancel, capture_bytes=self._capture_bytes)
            for stopped, status in ((result.cancelled, "CANCELLED"), (result.timed_out, "TIMEOUT"), (result.output_limit or result.stdout_truncated or result.stderr_truncated, "OUTPUT_LIMIT")):
                if stopped:
                    raise _Interrupted(status)
            return subprocess.CompletedProcess([self._docker, *args], result.returncode, result.stdout.decode("utf-8", errors="replace"), result.stderr.decode("utf-8", errors="replace"))
        return self._run([self._docker, *args], capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=timeout)

    def install_hosts(self) -> list[str]:
        hosts = [h for h in ("registry.npmjs.org",) if self._egress.check("sandbox_install", f"https://{h}/").allowed]
        if not hosts:
            raise WebVerificationError("the egress allowlist does not allow registry.npmjs.org for sandbox_install, so nothing can be installed")
        return hosts

    def image_id(self) -> str:
        done = self._docker_call(["image", "inspect", "--format", "{{.Id}}", self._image], timeout=60)
        value = done.stdout.strip()
        if done.returncode != 0 or not re.fullmatch(r"sha256:[0-9a-f]{64}", value):
            raise WebVerificationError(f"the website verification image {self._image} is not present: run `devpilot setup --web`")
        return value

    def verify(self, files: Mapping[str, bytes], kind: str, *, component: str = "", expect: Mapping[str, Any] | None = None, require_approval: bool = True, cancel: threading.Event | None = None) -> WebResult:
        # Per-attempt state: concurrent runs must never share a cancel event or deadline.
        attempt = copying.copy(self)
        attempt._cancel, attempt._deadline = cancel, time.monotonic() + self._timeout
        started = time.monotonic()
        try:
            result = attempt._verify(files, kind, component=component, expect=expect, require_approval=require_approval)
        except (_Interrupted, subprocess.TimeoutExpired) as exc:
            status = exc.status if isinstance(exc, _Interrupted) else "TIMEOUT"
            result = WebResult("NOT_RUN", f"website verification ended with {status}", kind, exec_status=status)
        return replace(result, duration_ms=int((time.monotonic() - started) * 1000))

    def _verify(self, files: Mapping[str, bytes], kind: str, *, component: str, expect: Mapping[str, Any] | None, require_approval: bool) -> WebResult:
        if kind not in WEB_KINDS:
            raise WebVerificationError(f"no website verification exists for {kind}; it covers {', '.join(WEB_KINDS)}")
        image_id = self.image_id()
        if require_approval and image_id not in _APPROVED:
            raise WebVerificationError(f"the image {self._image} ({image_id[:19]}…) has not passed its self-test on this computer: run `devpilot setup --web`")
        run_id = secrets.token_hex(6)
        folder = self._work / f"web-{run_id}"
        copy, spec, ledger, edge = folder / "work", folder / "spec", folder / "ledger", folder / "edge"
        for f in (spec, ledger, edge):
            f.mkdir(parents=True, exist_ok=True)
        install: dict[str, Any] | None = None
        audit: tuple[dict[str, Any], ...] = ()
        result = WebResult("NOT_RUN", "verification did not finish", kind, image_id=image_id)
        phase = "prepare"
        try:
            skipped = prepare_copy(files, copy, component=component)
            if expect is not None:
                (spec / "expect.json").write_text(json.dumps(expect), encoding="utf-8")
            if kind == "react-vite":
                if not (copy / "package.json").is_file():
                    raise WebVerificationError("a React + Vite component needs package.json")
                phase = "install"
                install, audit = self._install(run_id, copy, ledger, edge, image_id)
            if install is not None and install.get("ok") is not True:
                result = judge(kind, "", install=install, image_id=image_id)
            else:
                phase = "verify"
                done = self._verify_container(run_id, copy, spec if expect is not None else None, kind, image_id)
                if self._cancel is not None and self._cancel.is_set():
                    raise _Interrupted("CANCELLED")
                result = judge(kind, done.stdout + "\n" + done.stderr, install=install, image_id=image_id)
                if done.returncode != 0 and result.verdict != "FAILED":
                    result = replace(result, verdict="FAILED", reason=f"verification container exited with {done.returncode}", exec_status="FAILED")
            result = replace(result, proxy_audit=audit, evidence={"skipped_files": skipped, "files": sorted(files), "phase": phase}, exec_status="SUCCEEDED" if result.passed else "FAILED")
        except (_Interrupted, subprocess.TimeoutExpired) as exc:
            status = exc.status if isinstance(exc, _Interrupted) else "TIMEOUT"
            result = WebResult("NOT_RUN" if status == "CANCELLED" else "FAILED", f"{phase}: {status}", kind, install=install, proxy_audit=audit, image_id=image_id, evidence={"phase": phase}, exec_status=status)
        except WebVerificationError as exc:
            result = WebResult("NOT_RUN", f"{phase}: {exc}", kind, install=install, proxy_audit=audit, image_id=image_id, evidence={"phase": phase}, exec_status="ERROR")
        except OSError as exc:
            result = WebResult("NOT_RUN", f"{phase}: Docker unavailable ({type(exc).__name__})", kind, image_id=image_id, exec_status="UNAVAILABLE")
        finally:
            cleaned = self._cleanup(run_id)
        if not cleaned:
            return replace(result, verdict="NOT_RUN", reason="website resources could not be confirmed removed", cleanup="failed", exec_status="ERROR")
        return replace(result, cleanup="removed")

    # --- the containers ------------------------------------------------------------------------------------

    def _names(self, run_id: str) -> dict[str, str]:
        return {"internal": f"devpilot-webinst-{run_id}", "egress": f"devpilot-webegr-{run_id}", "proxy": f"devpilot-webproxy-{run_id}",
                "install": f"devpilot-webdeps-{run_id}", "verify": f"devpilot-webverify-{run_id}"}  # fmt: skip

    def _install(self, run_id: str, copy: Path, ledger: Path, edge: Path, image_id: str) -> tuple[dict[str, Any], tuple[dict[str, Any], ...]]:
        n = self._names(run_id)
        shutil.copyfile(PROXY_SOURCE, edge / "install_proxy.py")
        (edge / "proxy.json").write_text(json.dumps({"hosts": self.install_hosts(), "max_bytes": 1_500_000_000}), encoding="utf-8")
        for name, internal in ((n["internal"], True), (n["egress"], False)):
            made = self._docker_call(["network", "create", *(["--internal"] if internal else []), "--label", LABEL, name])
            if made.returncode != 0:
                raise WebVerificationError(f"docker could not create the network {name}: {made.stderr.strip()[-300:]}")
        started = self._docker_call([
            "run", "-d", "--name", n["proxy"], "--label", LABEL, "--network", n["egress"], *_hardening(memory="128m", pids=64), "--user", "65534:65534",
            "-v", f"{edge}:/opt/edge:ro", "-v", f"{ledger}:/var/devpilot/ledger", PROXY_IMAGE,
            "python", "-I", "-B", "/opt/edge/install_proxy.py", "--config", "/opt/edge/proxy.json", "--listen", "0.0.0.0:3128", "--audit", "/var/devpilot/ledger/install-proxy.jsonl",
        ])  # fmt: skip
        if started.returncode != 0:
            raise WebVerificationError(f"the install proxy could not start: {started.stderr.strip()[-300:]}")
        joined = self._docker_call(["network", "connect", "--alias", "installproxy", n["internal"], n["proxy"]])
        if joined.returncode != 0:
            raise WebVerificationError(f"the install proxy could not join the install network: {joined.stderr.strip()[-300:]}")
        proxy = "http://installproxy:3128"
        done = self._docker_call([
            "run", "--rm", "--name", n["install"], "--label", LABEL, "--network", n["internal"], *_hardening(memory=self._memory, pids=self._pids), "--cpus", str(self._cpus),
            "--tmpfs", "/tmp:rw,exec,size=1536m", "-e", "HOME=/tmp", "-e", f"HTTPS_PROXY={proxy}", "-e", f"https_proxy={proxy}", "-e", f"npm_config_https_proxy={proxy}",
            "-e", f"npm_config_proxy={proxy}", "-e", "NO_PROXY=", "-v", f"{copy}:/work", image_id, "node", "/opt/verify/install-deps.mjs",
        ], timeout=self._timeout)  # fmt: skip
        report = _report_json(_INSTALL, done.stdout) or {"ok": False, "detail": (done.stdout + done.stderr)[-1_500:] or f"the installer exited with {done.returncode}"}
        if done.returncode != 0:
            report = {"ok": False, "detail": f"the installer exited with {done.returncode}"}
        audit_file = ledger / "install-proxy.jsonl"
        try:
            rows = tuple(json.loads(line) for line in audit_file.read_text(encoding="utf-8").splitlines() if line.strip()) if audit_file.is_file() else ()
        except ValueError as exc:
            raise WebVerificationError("the install proxy audit could not be read") from exc
        return report, rows

    def _verify_container(self, run_id: str, copy: Path, spec: Path | None, kind: str, image_id: str) -> subprocess.CompletedProcess[str]:
        n = self._names(run_id)
        mounts = ["-v", f"{copy}:/work"] + (["-v", f"{spec}:/spec:ro"] if spec else [])
        argv = ["node", "/opt/verify/verify-web.mjs", "--kind", kind] + (["--expect", "/spec/expect.json"] if spec else [])
        return self._docker_call([
            "run", "--rm", "--name", n["verify"], "--label", LABEL, "--network", "none", *_hardening(memory=self._memory, pids=self._pids), "--cpus", str(self._cpus),
            "--tmpfs", "/tmp:rw,exec,size=1536m", "--shm-size", "512m", "-e", "HOME=/tmp", *mounts, image_id, *argv,
        ], timeout=self._timeout)  # fmt: skip

    def _cleanup(self, run_id: str) -> bool:
        n = self._names(run_id)
        self._cleaning = True
        clean = True
        for resource, remove, inspect in (
            *((n[key], ["rm", "-f"], ["ps", "-aq"]) for key in ("install", "verify", "proxy")),
            *((n[key], ["network", "rm"], ["network", "ls", "-q"]) for key in ("internal", "egress")),
        ):
            try:
                self._docker_call([*remove, resource], timeout=10)
                check = self._docker_call([*inspect, "--filter", f"name=^/?{resource}$"], timeout=10)
                clean = clean and check.returncode == 0 and not check.stdout.strip()
            except (OSError, subprocess.TimeoutExpired, _Interrupted):
                clean = False
        return clean
