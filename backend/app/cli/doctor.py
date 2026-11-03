"""`devpilot doctor`: every requirement for a real build, checked locally, with what to do about each one that is missing.

It reads local state only. It never contacts OpenAI, never starts a container, never reads the repository's `.env`, and never prints
the key. Each check is `ok`, `warn` (a build can still start) or `fail` (it cannot), and a failure says how to fix it.
"""

from __future__ import annotations

import importlib.util
import shutil
import socket
import subprocess
import sys
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from app.cli import credentials, runtime
from app.cli.engine import build_settings
from app.cli.paths import Paths
from app.cli.userconfig import UserConfig
from app.coding.launcher import config_problems
from app.core.egress import EgressPolicy

MIN_FREE_GB = 5.0
OPENCODE_PORT = 4096
_MODULES = ("fastapi", "uvicorn", "pydantic", "pydantic_settings", "sqlalchemy", "yaml", "openai", "httpx")


@dataclass(frozen=True)
class Check:
    name: str
    status: str  # "ok" | "warn" | "fail"
    detail: str = ""
    fix: str = ""


def _port_free(port: int) -> bool:
    with socket.socket() as probe:
        probe.settimeout(1)
        return probe.connect_ex(("127.0.0.1", port)) != 0


def _free_gb(path: Path) -> float:
    target = path if path.exists() else path.parent
    while not target.exists() and target != target.parent:
        target = target.parent
    return shutil.disk_usage(target).free / 1024**3


def _on_mains() -> bool | None:
    from app.coding.guard_process import on_mains_power

    return on_mains_power()


def run_checks(
    paths: Paths,
    config: UserConfig,
    *,
    run: runtime.Runner = subprocess.run,
    which: Callable[[str], str | None] = shutil.which,
    port_free: Callable[[int], bool] = _port_free,
    free_gb: Callable[[Path], float] = _free_gb,
    on_mains: Callable[[], bool | None] = _on_mains,
) -> list[Check]:
    checks: list[Check] = []

    def add(name: str, ok: bool, detail: str = "", fix: str = "", *, warn: bool = False) -> None:
        checks.append(Check(name, "ok" if ok else ("warn" if warn else "fail"), detail, fix))

    add("Python 3.12", sys.version_info[:2] == (3, 12), f"running {sys.version.split()[0]}", "run setup.cmd again: it creates the environment with Python 3.12")
    missing = [m for m in _MODULES if importlib.util.find_spec(m) is None]
    add("DevPilot dependencies", not missing, f"missing: {', '.join(missing)}" if missing else "all installed", "run setup.cmd again")
    add("Git", which("git") is not None, "" if which("git") else "not found", "install Git for Windows (only needed to update DevPilot)", warn=True)

    # Docker: the daemon, the sandbox image, the audited runtime image.
    docker_path = which("docker")
    daemon = False
    if docker_path is None:
        add("Docker installed", False, "docker was not found on PATH", "install Docker Desktop (with the WSL2 backend), then start it")
    else:
        try:
            done = runtime.docker(["version", "--format", "{{.Server.Version}}"], run=run, timeout=30)
            daemon = done.returncode == 0 and bool(done.stdout.strip())
            add("Docker running", daemon, f"engine {done.stdout.strip()}" if daemon else "the engine is not answering", "start Docker Desktop and wait until it says it is running")
        except runtime.ProvisionError as exc:
            add("Docker running", False, str(exc), "start Docker Desktop")
    image_id: str | None = None
    if daemon:
        sandbox = runtime.docker(["image", "inspect", "python:3.12-alpine", "--format", "{{.Id}}"], run=run, timeout=30)
        add("Sandbox image python:3.12-alpine", sandbox.returncode == 0, "" if sandbox.returncode == 0 else "not present", "run setup.cmd: it pulls the image")
        image_id, why = runtime.load_approval(paths, run=run)
        add("OpenCode runtime image audited and approved", image_id is not None, (image_id or "")[:19] if image_id else (why or ""), "run setup.cmd: it builds the image, audits its contents and approves it")
    else:
        add("OpenCode runtime image audited and approved", False, "cannot be checked while Docker is not running", "start Docker Desktop first")

    problem = runtime.opencode_ok(paths)
    add("OpenCode binary verified", problem is None, problem or f"{runtime.CONTRACT_VERSION}, hash matches the pinned release", "run setup.cmd: it downloads and verifies OpenCode")

    # The user's key, model and prices. Presence only: the key's value is never read into output.
    where = credentials.source(paths.credentials)
    add("OpenAI API key", where is not None, f"from {where}" if where else "none set", "run setup.cmd, or set the OPENAI_API_KEY environment variable")
    key = credentials.load_key(paths.credentials)
    if key is not None and not credentials.plausible(key):
        add("OpenAI API key format", False, "it does not look like an OpenAI key", "run setup.cmd and enter the key again")
    add("Model and prices", bool(config.model) and config.prices_set, f"{config.model}, ${config.price_input_per_mtok} in / ${config.price_output_per_mtok} out per 1M tokens" if config.model and config.prices_set else "not set", "run setup.cmd: it asks for the model and your prices, which cap your spending")
    add("Spending cap", True, f"${config.run_cap_usd:.2f} per build, GPT and OpenCode together, enforced before every request")

    # The network policy and the engine's own readiness check, the same one a build runs.
    manifest = runtime.opencode_manifest(paths) if problem is None else None
    try:
        settings = build_settings(paths, config, key=key, image_id=image_id, manifest=manifest, cap_usd=config.run_cap_usd, live_approved=True)
        egress = EgressPolicy.from_file(settings.egress_allowlist_file)
        remaining = [p for p in config_problems(settings, egress) if not (image_id is None and "runtime image" in p) and not (manifest is None and "MANIFEST" in p)]
        policy_ok = egress.check("backend", "https://api.openai.com/v1/responses").allowed and egress.check("backend", settings.opencode_base_url).allowed
        add("Network policy (OpenAI and the local filter only)", policy_ok, "", "restore config/egress_allowlist.yaml from the repository")
        add("Coding engine configuration", not remaining, "; ".join(remaining) if remaining else "complete", "run setup.cmd")
    except Exception as exc:  # noqa: BLE001 - a settings error is a finding, not a crash
        add("Coding engine configuration", False, f"{type(exc).__name__}: {str(exc)[:200]}", "run setup.cmd")

    # Leftovers, disk, power, port.
    if daemon:
        names: list[str] = []
        for label in ("devpilot.opencode", "devpilot.edge", "devpilot.sandbox"):
            listed = runtime.docker(["ps", "-aq", "--filter", f"label={label}"], run=run, timeout=30)
            names += listed.stdout.split()
        nets = [n for n in runtime.docker(["network", "ls", "--format", "{{.Name}}"], run=run, timeout=30).stdout.split() if n.startswith("devpilot-")]
        add("No leftover DevPilot containers or networks", not names and not nets, f"{len(names)} containers, {len(nets)} networks" if names or nets else "", "a crashed build left them: remove them with `docker rm -f` and `docker network rm`, or run `devpilot build` once more, which sweeps containers", warn=True)
    markers = sorted(paths.guard.glob("*/quarantine.json")) if paths.guard.is_dir() else []
    add("No unconfirmed container stop", not markers, f"{len(markers)} marker(s) under {paths.guard}" if markers else "", "an earlier build could not confirm a container stopped: check `docker ps -a`, then delete the marker folder")
    gb = free_gb(paths.home)
    add("Disk space", gb >= MIN_FREE_GB, f"{gb:.1f} GB free where DevPilot keeps its files", f"free space on that drive, or set DEVPILOT_HOME to a roomier one (now {paths.home})")
    power = on_mains()
    add("Mains power", power is not False, "on battery" if power is False else "", "plug the laptop in: a sleeping laptop freezes a build", warn=True)
    add(f"Port {OPENCODE_PORT} free", port_free(OPENCODE_PORT), "", f"close the program using port {OPENCODE_PORT} (the local OpenCode filter needs it)")
    return checks


def render(checks: list[Check]) -> str:
    marks = {"ok": "ok  ", "warn": "warn", "fail": "FAIL"}
    lines = []
    for c in checks:
        lines.append(f"  [{marks[c.status]}] {c.name}" + (f"  ({c.detail})" if c.detail else ""))
        if c.status != "ok" and c.fix:
            lines.append(f"         to fix: {c.fix}")
    failed = sum(c.status == "fail" for c in checks)
    warned = sum(c.status == "warn" for c in checks)
    passed = sum(c.status == "ok" for c in checks)
    lines.append("")
    lines.append(f"{passed} of {len(checks)} checks passed" + (f", {warned} warning(s)" if warned else "") + (f", {failed} FAILED: a build cannot start until they are fixed." if failed else ". Ready to build."))
    return "\n".join(lines)


def blockers(checks: list[Check]) -> list[Check]:
    return [c for c in checks if c.status == "fail"]
