"""Serve the dashboard with the real coding engine: real GPT, real OpenCode, real Docker tests. Builds are started from the dashboard.

    & $env:DEVPILOT_PYTHON scripts\\live_server.py                          # preflight only: contacts nothing external, sends no key
    & $env:DEVPILOT_PYTHON scripts\\live_server.py --approve-session 1.00   # APPROVAL: asks for a typed "serve live", then serves

Run it from the backend folder, with Docker running, instead of `python -m app`. It is `scripts/live_build.py`'s configuration, the one
that delivered the verified attendance tracker: its model, prices, runtime image, verified OpenCode binary and limits, with a run limit of
$0.50 (GPT and OpenCode together). Those, the database and the paths are constants in code and override `.env`, so what was approved is
what runs. `.env` supplies the OpenAI key and DEVPILOT_API_TOKEN, and both forms load it into this process.

Before serving, the same steps as a terminal build: every check that needs nothing external (the preflight), the key's access to the
model (GET /v1/models/{model}, not billed), a dry launch of the real containers, then keep-awake and the guard that stops the coding
containers if this process ends. While serving:
- a build starts only with the dashboard's spending approval of the run limit (REQUIRE_SPEND_APPROVAL),
- this session's runs together stay within the total approved here, each counted at its whole limit until it ends (SessionBudget),
- the session lasts --hours (default 2). The guard stops the server then, and no run is started that could not finish before.
Its database and workspaces live in D:\\DevPilotCache\\live-dashboard, apart from the terminal experiments, whose runs the dashboard
shows read-only as history.
"""

from __future__ import annotations

import argparse
import socket
import sys
import time
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent))  # live_build.py and live_demo.py, beside this file
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import live_build  # noqa: E402
import live_demo  # noqa: E402
from app.coding.guard_process import QUARANTINE_FILE, keep_awake, start_guard  # noqa: E402
from app.coding.launcher import NETWORKS, OpenCodeLauncher  # noqa: E402
from app.config import REPO_ROOT, Settings  # noqa: E402
from app.core.egress import EgressPolicy  # noqa: E402
from app.main import create_app  # noqa: E402
from app.orchestrator.start_policy import SessionBudget  # noqa: E402
from app.sandbox.docker_cli import DockerCli  # noqa: E402
from app.sandbox.process import SubprocessRunner  # noqa: E402
from app.sandbox.runner import DockerSandbox  # noqa: E402
from app.selection.selector import ProviderOption, ProviderSelector  # noqa: E402

ROOT = Path(r"D:\DevPilotCache\live-dashboard")
RUN_LIMIT_USD = live_build.SPEND_LIMIT_USD
MAX_SESSION_USD = 5.00  # the most --approve-session accepts: a mistyped figure must not approve more
MAX_HOURS = 8.0
CONFIRMATION = "serve live"
LIMITS: dict[str, Any] = {**live_build.LIMITS, "require_spend_approval": True}
_START_MARGIN_SECONDS = 300.0  # a run starts only if it could finish this long before the session ends
CAPABILITIES = frozenset({"code_generation", "test_generation", "documentation"})


def server_settings(root: Path, *, approved: bool) -> Settings:
    return Settings(
        _env_file=REPO_ROOT / ".env", database_url=f"sqlite:///{(root / 'devpilot.db').as_posix()}", workspaces_dir=root / "workspaces",
        coding_checkpoints_dir=root / "checkpoints", coding_work_dir=root / "work", coding_live_approved=approved, **LIMITS,
    )  # fmt: skip


def _port_free(port: int) -> bool:
    with socket.socket() as probe:
        probe.settimeout(1)
        return probe.connect_ex(("127.0.0.1", port)) != 0


def server_checks(settings: Settings, root: Path) -> list[tuple[str, bool, str]]:
    """What a long-lived server needs beyond a terminal build's preflight."""
    markers = sorted(root.glob(f"guard/*/{QUARANTINE_FILE}")) if root.is_dir() else []
    leftover = settings.coding_work_dir / "dry-launch"
    return [
        ("no earlier server session left an unconfirmed stop", not markers, ", ".join(map(str, markers[:2]))),
        ("no dry launch was left behind", not leftover.exists(), str(leftover) if leftover.exists() else ""),
        (f"the dashboard's port {settings.devpilot_port} is free", _port_free(settings.devpilot_port), ""),
    ]


def confirm(total: float, hours: float, ask: Callable[[str], str] = input, say: Callable[[str], None] = print) -> bool:
    """A person's own go-ahead, in the terminal. Anything but the typed phrase stops before Docker or OpenAI are touched."""
    say(
        "\nThis serves the dashboard with real paid model calls.\n"
        f"  Each build needs its own approval in the dashboard, and is stopped before it could pass ${RUN_LIMIT_USD:.2f}.\n"
        f"  This session's builds together stay within ${total:.2f}, and the session ends after {hours:g} hours.\n"
    )
    return ask(f'Type "{CONFIRMATION}" to continue: ').strip() == CONFIRMATION


def main(argv: list[str] | None = None, *, settings_for: Callable[..., Settings] = server_settings, serve: Callable[..., None] | None = None,
         ask: Callable[[str], str] = input, root: Path = ROOT) -> int:  # fmt: skip
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--approve-session", type=float, help="the most this session's builds may spend together, in US dollars; without it nothing external is contacted")
    parser.add_argument("--hours", type=float, default=2.0, help=f"how long the session lasts (at most {MAX_HOURS:g})")
    args = parser.parse_args(argv)
    total = args.approve_session
    if total is not None and not RUN_LIMIT_USD <= total <= MAX_SESSION_USD:
        print(f"Refused: --approve-session takes ${RUN_LIMIT_USD:.2f} (one build) to ${MAX_SESSION_USD:.2f}.")
        return 2
    if not 0 < args.hours <= MAX_HOURS:
        print(f"Refused: --hours takes more than 0 and at most {MAX_HOURS:g}.")
        return 2
    approved = total is not None
    root.mkdir(parents=True, exist_ok=True)
    settings = settings_for(root, approved=approved)
    cli = DockerCli("docker", SubprocessRunner())
    checks = live_demo.preflight(settings, cli, RUN_LIMIT_USD) + live_build.machine_checks() + server_checks(settings, root)
    for label, passed, detail in checks:
        print(f"  {'ok  ' if passed else 'FAIL'}  {label}" + (f"  [{detail}]" if detail else ""))
    failed = [label for label, passed, _ in checks if not passed]
    if not approved:
        print(f"\nPreflight only: {len(checks) - len(failed)} of {len(checks)} checks passed. Nothing external was contacted, and no key was sent.")
        return 1 if failed else 0
    if failed:
        print(f"\nRefused: {len(failed)} checks failed, and nothing was contacted.")
        return 1
    assert total is not None
    if not confirm(total, args.hours, ask):
        print("\nNot confirmed: nothing was started, contacted or spent.")
        return 0
    seconds = args.hours * 3600.0
    keep_awake()
    start_guard(root / "guard" / datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ"), deadline=seconds, networks=[name for name, _ in reversed(NETWORKS)])
    if not live_demo.model_access():
        print("Stopped: the key cannot see the model. Nothing was billed.")
        return 1
    settings.coding_work_dir.mkdir(parents=True, exist_ok=True)
    launcher = OpenCodeLauncher(settings, cli, EgressPolicy.from_file(settings.egress_allowlist_file))
    problem = launcher.unavailable_reason() or live_demo.dry_launch(launcher, settings)
    if problem:
        print(f"Stopped before any model call: {problem}")
        return 1
    budget = SessionBudget(total, settings.run_budget_usd, run_seconds=settings.run_wall_clock_seconds + _START_MARGIN_SECONDS, ends_at=time.monotonic() + seconds)
    selector = ProviderSelector([ProviderOption("opencode_server", launcher, CAPABILITIES, simulated=False)])
    if serve is None:  # really serving: static websites are buildable only if this computer's verifier image passed its self-test
        print(f"Website builds: {_load_web_approval(settings)}")
    app = create_app(settings, sandbox=DockerSandbox.from_settings(settings), coding=selector, start_policy=budget)
    print(f"\nServing on http://{settings.devpilot_host}:{settings.devpilot_port} until {args.hours:g} hours have passed. Open the dashboard at "
          "http://localhost:5173 and connect with DEVPILOT_API_TOKEN from .env. Stop with Ctrl-C.")  # fmt: skip
    (serve or _serve)(app, settings)
    return 0


def _load_web_approval(settings: Settings) -> str:
    """The `devpilot setup --web` approval, re-checked against the image's current ID. Without it, static-html stays unavailable."""
    from app.cli.paths import paths
    from app.cli.webruntime import load_approval
    from app.verification.web import WebVerifier

    image_id, why = load_approval(paths(), WebVerifier(EgressPolicy.from_file(settings.egress_allowlist_file), docker=settings.docker_cli, work_dir=settings.coding_work_dir / "web-verification"))
    return f"enabled (verifier {image_id[:19]}…)" if image_id else f"unavailable: {why}"


def _serve(app: Any, settings: Settings) -> None:
    import uvicorn

    uvicorn.run(app, host=settings.devpilot_host, port=settings.devpilot_port, log_config=None, access_log=False)


if __name__ == "__main__":
    sys.exit(main())
