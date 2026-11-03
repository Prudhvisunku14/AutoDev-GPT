"""One paid demonstration: real GPT, real OpenCode, the API filter, the model gateway, real Docker tests.

    & $env:DEVPILOT_PYTHON backend\\scripts\\live_demo.py                       # preflight only: contacts nothing external
    & $env:DEVPILOT_PYTHON backend\\scripts\\live_demo.py --approve-spend 1.50  # APPROVAL: calls OpenAI and may spend that much

The limits are constants here, not `.env` values, so what was approved is what runs. `.env` supplies the key, so
both forms load it into this process, the preflight too. Neither belongs in the offline workflow.

Before GPT's first paid call: the preflight, the free model-access check, and a dry launch that starts the real
containers and stops them, so a broken network or published port costs nothing. A separate guard stops everything
if this process dies, and the machine is asked to stay awake.

`run_demo` is what the no-model check rehearses with a scripted model, so a paid run is never its first run.
"""

from __future__ import annotations

import argparse
import json
import secrets
import shutil
import socket
import subprocess
import sys
import time
import warnings
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.coding.guard_process import QUARANTINE_FILE, keep_awake, on_mains_power, start_guard  # noqa: E402
from app.coding.launcher import EDGE_LABEL, NETWORKS, LaunchRefused, LaunchRequest, OpenCodeLauncher, read_ledger  # noqa: E402
from app.coding.opencode_container import APPROVED_RUNTIME_IMAGES, CONTAINER_LABEL, ContainerPolicyError, verified_binary  # noqa: E402
from app.config import REPO_ROOT, Settings  # noqa: E402
from app.core.egress import EgressPolicy  # noqa: E402
from app.main import create_app  # noqa: E402
from app.orchestrator.llm_client import llm_config_problem  # noqa: E402
from app.sandbox.docker_cli import DockerCli  # noqa: E402
from app.sandbox.policy import APPROVED_IMAGES  # noqa: E402
from app.sandbox.process import SubprocessRunner  # noqa: E402
from app.sandbox.runner import DockerSandbox  # noqa: E402
from app.selection.selector import ProviderOption, ProviderSelector  # noqa: E402
from app.verification.report import render_markdown  # noqa: E402

REQUEST = "Create a Python calculator application supporting addition, subtraction, multiplication and division, with unit tests."
MODEL = "gpt-6.1-sol"
SPEND_LIMIT_USD = 1.50
# Standard tier, up to 272,000 input tokens: developers.openai.com/api/docs/pricing, read 2026-10-31.
PRICE_INPUT_PER_MTOK = 2.0
PRICE_OUTPUT_PER_MTOK = 10.0
RUNTIME_IMAGE = "sha256:a7753fe443915eed28c577c1d1b3f0e27ab1c5bd16b7debc7de9579d2aaefe99"
MANIFEST = Path(r"D:\DevPilotCache\opencode\1.19.5\manifest.json")
DEMO_ROOT = Path(r"D:\DevPilotCache\live-demo")
LIMITS: dict[str, Any] = {
    "openai_model": MODEL, "opencode_model": f"openai/{MODEL}", "openai_price_input_per_mtok": PRICE_INPUT_PER_MTOK, "openai_price_output_per_mtok": PRICE_OUTPUT_PER_MTOK,
    "openai_max_output_tokens": 4_000, "openai_max_retries": 0, "run_budget_usd": SPEND_LIMIT_USD, "run_max_tokens": 300_000, "max_turns": 14, "max_tool_calls": 14,
    "run_wall_clock_seconds": 1_200.0, "opencode_max_tokens_per_task": 100_000, "opencode_max_attempts_per_task": 2, "opencode_timeout_seconds": 300.0, "max_repair_attempts": 1,
    "gateway_max_requests_per_task": 15, "gateway_max_output_tokens": 8_000, "gateway_max_usd_per_task": 0.75, "opencode_enabled": True,
    "opencode_runtime_image": RUNTIME_IMAGE, "opencode_manifest_file": MANIFEST,
}  # fmt: skip
API = "/api/v1"
_GUARD_MARGIN_SECONDS = 300.0
_MIN_FREE_GB = 5


@dataclass(frozen=True)
class Experiment:
    """What one paid run is. The defaults are the calculator demonstration above, unchanged. `scripts/live_build.py` is another:
    it brings its own request, limits, independent acceptance suite and checks, and runs through the same code."""

    name: str = "Calculator"
    request: str = REQUEST
    limits: dict[str, Any] | None = None  # None: LIMITS
    spend_limit: float = SPEND_LIMIT_USD
    acceptance: dict[str, Any] | None = None  # the evaluator's frozen suite, as the projects API takes it
    extra_checks: Callable[[], list[tuple[str, bool, str]]] | None = None  # more preflight checks
    confirm: Callable[[], bool] | None = None  # a person's separate go-ahead, asked after the preflight and before anything is started
    judge: Callable[[dict[str, Any]], list[str]] | None = None  # why a finished run is not a success; empty means it is
    show_report: bool = False
    # Spending across runs. `on_start` runs once the person has confirmed, before anything is started. `on_end` gets what the run
    # cost, 0.0 when it stopped before any model call, and None when the cost is unknown (it is then counted at the cap).
    on_start: Callable[[], None] | None = None
    on_end: Callable[[float | None], None] | None = None


def live_settings(root: Path, *, approved: bool, limits: dict[str, Any] | None = None) -> Settings:
    return Settings(
        _env_file=REPO_ROOT / ".env", devpilot_api_token=secrets.token_urlsafe(32), database_url=f"sqlite:///{(root / 'demo.db').as_posix()}",
        workspaces_dir=root / "workspaces", coding_checkpoints_dir=root / "checkpoints", coding_work_dir=root / "work", coding_live_approved=approved, **(LIMITS if limits is None else limits),
    )  # fmt: skip


def leftovers(cli: DockerCli) -> dict[str, list[str]]:
    containers = [c for label in (CONTAINER_LABEL, EDGE_LABEL, "devpilot.sandbox") for c in cli.labelled_containers(label)]
    listed = subprocess.run(["docker", "network", "ls", "--format", "{{.Name}}"], capture_output=True, text=True, timeout=30).stdout.split()
    return {"containers": containers, "networks": [n for n in listed if n.startswith("devpilot-")]}


def _ca_certificates(image: str) -> int:
    """How many certificate authorities the gateway's TLS context trusts. Asked with no network at all."""
    code = "import ssl; print(ssl.create_default_context().cert_store_stats()['x509_ca'])"
    done = subprocess.run(["docker", "run", "--rm", "--network", "none", "--pull", "never", "--read-only", "--cap-drop", "ALL", image, "python", "-I", "-c", code], capture_output=True, text=True, timeout=60)
    return int(done.stdout.strip()) if done.returncode == 0 and done.stdout.strip().isdigit() else 0


def _port_free(port: int) -> bool:
    with socket.socket() as probe:
        probe.settimeout(1)
        return probe.connect_ex(("127.0.0.1", port)) != 0


def preflight(settings: Settings, cli: DockerCli, spend_limit: float = SPEND_LIMIT_USD) -> list[tuple[str, bool, str]]:
    """Every check that needs nothing external. It reads the key's presence, never its value, and sends it nowhere."""
    checks: list[tuple[str, bool, str]] = []

    def check(label: str, passed: bool, detail: str = "") -> None:
        checks.append((label, passed, detail))

    check("the OpenAI key is configured", settings.openai_api_key is not None)
    check("prices are the official Standard rates", (settings.openai_price_input_per_mtok, settings.openai_price_output_per_mtok) == (2.0, 10.0), f"${settings.openai_price_input_per_mtok} / ${settings.openai_price_output_per_mtok}")
    check("the total spend limit is the approved one", settings.run_budget_usd == spend_limit and settings.cost_known, f"${settings.run_budget_usd:.2f}")
    check("the SDK makes no retries of its own", settings.openai_max_retries == 0)
    check("the runtime image is the approved one", settings.opencode_runtime_image in APPROVED_RUNTIME_IMAGES, str(settings.opencode_runtime_image)[:19])
    try:
        verified_binary(settings.opencode_manifest_file or Path())
        check("the pinned OpenCode binary matches its manifest", True)
    except ContainerPolicyError as exc:
        check("the pinned OpenCode binary matches its manifest", False, str(exc))
    images = (settings.opencode_runtime_image or "", APPROVED_IMAGES["python-runner"].reference)
    for image in images:
        check(f"image present: {image[:32]}", subprocess.run(["docker", "image", "inspect", image], capture_output=True, timeout=30).returncode == 0)
    authorities = _ca_certificates(APPROVED_IMAGES["python-runner"].reference)
    check("the gateway's image trusts certificate authorities", authorities > 0, f"{authorities} authorities")
    egress = EgressPolicy.from_file(settings.egress_allowlist_file)
    problem = llm_config_problem(settings, egress)
    check("the GPT client is configured, for OpenAI's own address", problem is None and not settings.openai_base_url, problem or str(settings.openai_base_url or ""))
    for url, what in ((settings.opencode_base_url, "the filter on loopback"), ("https://api.openai.com/v1/responses", "OpenAI")):
        decision = egress.check("backend", url)
        check(f"the allowlist permits {what}", decision.allowed, decision.reason)
    left = leftovers(cli)
    check("no DevPilot container or network exists", left == {"containers": [], "networks": []}, str(left) if left["containers"] or left["networks"] else "")
    check("the filter's loopback port is free", _port_free(int(settings.opencode_base_url.rsplit(":", 1)[-1].strip("/"))))
    markers = sorted(DEMO_ROOT.glob(f"*/guard/{QUARANTINE_FILE}")) if DEMO_ROOT.is_dir() else []
    check("no earlier run left an unconfirmed stop", not markers, ", ".join(map(str, markers[:2])))
    check("the laptop is on mains power", on_mains_power() is not False, "plug it in first" if on_mains_power() is False else "")
    free = shutil.disk_usage(DEMO_ROOT.anchor if DEMO_ROOT.is_absolute() else ".").free // 1024**3
    check("there is room on D:", free >= _MIN_FREE_GB, f"{free} GB free")
    return checks


def model_access() -> bool:
    """GET /v1/models/{model}: not billed. It proves the key sees the model, not that quota remains."""
    done = subprocess.run([sys.executable, str(Path(__file__).resolve().parent / "check_openai_access.py")], cwd=REPO_ROOT, capture_output=True, text=True, timeout=90)
    print(done.stdout.strip())
    return done.returncode == 0


def dry_launch(launcher: Any, settings: Settings) -> str | None:
    """Start the real containers and stop them, before anything is paid for. Why it failed, or None."""
    workspace = settings.coding_work_dir / "dry-launch"
    workspace.mkdir(parents=True)
    try:
        session = launcher.start(LaunchRequest(run_id="dry-launch", call_id="dry-launch", workspace=workspace, token_budget=1_000, usd_budget=0.01))
    except LaunchRefused as exc:
        shutil.rmtree(workspace, ignore_errors=True)
        return str(exc)
    session.close()
    stopped = session.writer.terminate()  # asking again is safe, and it proves the stop rather than assuming it
    shutil.rmtree(workspace, ignore_errors=True)
    return None if stopped.confirmed else f"the dry launch could not be confirmed stopped: {stopped.evidence}"


def run_demo(settings: Settings, *, llm: Any, coding: ProviderSelector, sandbox: Any, out: Path, request: str = REQUEST, wait: float | None = None, cli: DockerCli | None = None,
             name: str = "Calculator", acceptance: dict[str, Any] | None = None) -> dict[str, Any]:  # fmt: skip
    """The demonstration, start to finish. A run that pauses is cancelled: nothing carries on spending unattended."""
    with warnings.catch_warnings():
        warnings.filterwarnings("ignore", message=".*httpx.*")
        from fastapi.testclient import TestClient

    app = create_app(settings, llm=llm, sandbox=sandbox, coding=coding)
    headers = {"Authorization": f"Bearer {settings.devpilot_api_token.get_secret_value()}"}
    limit = time.monotonic() + (wait if wait is not None else settings.run_wall_clock_seconds + 120.0)
    began = time.monotonic()
    with TestClient(app) as client:
        project = client.post(f"{API}/projects", json={"name": name, "requirement_text": request, **({"acceptance": acceptance} if acceptance else {})}, headers=headers)
        project.raise_for_status()  # a suite the API refuses must stop the run before it starts, not be skipped
        project = project.json()
        run = client.post(f"{API}/projects/{project['id']}/runs", headers=headers).json()
        client.post(f"{API}/runs/{run['id']}/start", headers=headers).raise_for_status()
        while True:
            run = client.get(f"{API}/runs/{run['id']}", headers=headers).json()
            if run["ended_at"]:
                break
            if run["pause_reason"] or time.monotonic() > limit:
                client.post(f"{API}/runs/{run['id']}/cancel", headers=headers).raise_for_status()
                run = client.get(f"{API}/runs/{run['id']}", headers=headers).json()
                break
            time.sleep(1.0)
        report = client.get(f"{API}/runs/{run['id']}/report", headers=headers)
        delivered = app.state.workspace_fs.project_dir(project["id"])
    evidence = settings.coding_work_dir / "evidence"
    ledgers = [read_ledger(path) for path in sorted(evidence.glob("*/ledger.jsonl"))] if evidence.is_dir() else []
    ledgers = [ledger for ledger in ledgers if ledger["requests"]]  # the dry launch leaves an empty one
    summary: dict[str, Any] = {
        "state": run["state"], "outcome": run["outcome"], "detail": run["status_detail"], "turns": run["turns"], "tool_calls": run["tool_calls"], "cost_usd": run["cost_usd"],
        "tokens_in": run["tokens_in"], "tokens_out": run["tokens_out"], "seconds": round(time.monotonic() - began, 1), "attempts": len(ledgers), "gateway": ledgers,
        "delivered": sorted(p.name for p in delivered.iterdir()) if delivered.is_dir() else [], "report": report.json() if report.status_code == 200 else {},
    }  # fmt: skip
    if cli is not None:
        summary["left_behind"] = leftovers(cli)
    (out / "summary.json").write_text(json.dumps(summary, indent=1, default=str), encoding="utf-8")
    return summary


def main(argv: list[str] | None = None, experiment: Experiment | None = None, *, doc: str | None = None) -> int:
    exp = experiment or Experiment()
    parser = argparse.ArgumentParser(description=doc or __doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--approve-spend", type=float, help="the exact approved limit in US dollars; without it nothing external is contacted")
    args = parser.parse_args(argv)
    if args.approve_spend is not None and args.approve_spend != exp.spend_limit:
        print(f"Refused: the approved limit is ${exp.spend_limit:.2f}, and this script takes no other figure.")
        return 2
    approved = args.approve_spend is not None
    out = DEMO_ROOT / datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    out.mkdir(parents=True)
    settings = live_settings(out, approved=approved, limits=exp.limits)
    cli = DockerCli("docker", SubprocessRunner())
    checks = preflight(settings, cli, exp.spend_limit) + (exp.extra_checks() if exp.extra_checks is not None else [])
    for label, passed, detail in checks:
        print(f"  {'ok  ' if passed else 'FAIL'}  {label}" + (f"  [{detail}]" if detail else ""))
    failed = [label for label, passed, _ in checks if not passed]
    if not approved:
        shutil.rmtree(out, ignore_errors=True)
        print(f"\nPreflight only: {len(checks) - len(failed)} of {len(checks)} checks passed. Nothing external was contacted, and no key was sent.")
        return 1 if failed else 0
    if failed:
        print(f"\nRefused: {len(failed)} checks failed, and nothing was contacted.")
        return 1
    if exp.confirm is not None and not exp.confirm():  # a person's own go-ahead, separate from the flag above
        shutil.rmtree(out, ignore_errors=True)
        print("\nNot confirmed: nothing was started, contacted or spent.")
        return 0
    if exp.on_start is not None:  # the run counts at its whole cap from here until it is settled, so a crash cannot hide it
        exp.on_start()
    keep_awake()
    start_guard(out / "guard", deadline=settings.run_wall_clock_seconds + _GUARD_MARGIN_SECONDS, networks=[name for name, _ in reversed(NETWORKS)])
    if not model_access():
        print("Stopped: the key cannot see the model. Nothing was billed.")
        if exp.on_end is not None:
            exp.on_end(0.0)
        return 1
    settings.coding_work_dir.mkdir(parents=True, exist_ok=True)
    launcher = OpenCodeLauncher(settings, cli, EgressPolicy.from_file(settings.egress_allowlist_file))
    problem = launcher.unavailable_reason() or dry_launch(launcher, settings)
    if problem:
        print(f"Stopped before any model call: {problem}")
        if exp.on_end is not None:
            exp.on_end(0.0)
        return 1
    selector = ProviderSelector([ProviderOption("opencode_server", launcher, frozenset({"code_generation", "test_generation", "documentation"}), simulated=False)])
    summary = run_demo(settings, llm=None, coding=selector, sandbox=DockerSandbox.from_settings(settings), out=out, cli=cli, request=exp.request, name=exp.name, acceptance=exp.acceptance)
    if exp.on_end is not None:
        exp.on_end(summary.get("cost_usd"))
    print(json.dumps({k: v for k, v in summary.items() if k != "report"}, indent=1, default=str))
    if exp.show_report and summary.get("report"):
        print("\n" + render_markdown(summary["report"]))
    print(f"Evidence: {out}")
    problems = exp.judge(summary) if exp.judge is not None else []
    for problem in problems:
        print(f"Not a success: {problem}")
    return 0 if summary["state"] == "COMPLETED" and summary.get("left_behind") == {"containers": [], "networks": []} and not problems else 1


if __name__ == "__main__":
    sys.exit(main())
