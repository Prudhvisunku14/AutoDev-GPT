"""The no-model launcher check: the actual launcher on real containers, with no model and no route out.

    & $env:DEVPILOT_PYTHON backend\\scripts\\opencode_launcher_check.py                 # dry run: starts nothing
    & $env:DEVPILOT_PYTHON backend\\scripts\\opencode_launcher_check.py --run           # APPROVAL: starts containers
    & $env:DEVPILOT_PYTHON backend\\scripts\\opencode_launcher_check.py --run --only stop --deadline 180

Real here: `OpenCodeLauncher`, Docker, the approved runtime image, the OpenCode server, the API filter, the
gateway's code, internal networks, a writable bind mount, `ContainerTerminator`, `AttemptGuard`, the adapter
and the Docker test sandbox.

A separate guard process (app/coding/guard_process.py) watches this one, because a process cannot be trusted to
clean up after itself: PowerShell terminates a child part-way through its `finally`. `--deadline` is per scenario.

Not real, and never presented as real:
- The model. The gateway's upstream is a script (scripts/opencode_check/fake_model_gateway.py). No key exists
  anywhere in this check, and no request can leave, because every network is internal.
- The route from the host to the filter. A published port needs a network with a route out, which this check
  does not create. `ExecForwarder` carries each connection through `docker exec` instead.

`NoModelLauncher` only removes from the live layout: no ingress network, no egress network, no published
port, no key. It adds nothing to OpenCode's container, whose definition and checks are the production ones.
"""

from __future__ import annotations

import argparse
import base64
import json
import os
import re
import secrets
import shutil
import signal
import socket
import subprocess
import sys
import threading
import time
import warnings
from collections import Counter
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import live_demo  # noqa: E402
import opencode_filter_check as filter_check  # noqa: E402

from app.coding.attempt_store import SqlAttemptStore  # noqa: E402
from app.coding.candidate import CandidateWorkspaces  # noqa: E402
from app.coding.container_stop import ContainerTerminator  # noqa: E402
from app.coding.guard_process import QUARANTINE_FILE, READY_FILE, REPORT_FILE, RESOLVED_FILE, keep_awake, start_guard  # noqa: E402
from app.coding.launcher import EDGE_LABEL, EDGE_MOUNT, EGRESS_NETWORK, GATEWAY_PORT, LEDGER_MOUNT, LaunchRequest, OpenCodeLauncher, edge_args, ledger_rows, read_ledger  # noqa: E402
from app.coding.opencode_container import (  # noqa: E402
    APPROVED_RUNTIME_IMAGES,
    CONTAINER_LABEL,
    CORE_NETWORK,
    MODEL_NETWORK,
    PASSWORD_ENV,
    deployment_problems,
)
from app.coding.opencode_policy import PROMPTED_AGENT, session_rules, verify_effective_config  # noqa: E402
from app.coding.opencode_tools import tool_inventory_problems  # noqa: E402
from app.coding.session_control import UNRESOLVED_PHASES, AttemptGuard, workspace_fingerprint  # noqa: E402
from app.coding.types import AttemptPhase  # noqa: E402
from app.config import Settings  # noqa: E402
from app.core.redaction import Redactor  # noqa: E402
from app.db import repositories  # noqa: E402
from app.db.models import CodingAttempt, new_id  # noqa: E402
from app.db.session import create_db_engine, create_session_factory, init_db  # noqa: E402
from app.demo import mvp  # noqa: E402
from app.main import create_app  # noqa: E402
from app.orchestrator.fake_llm import FakeLLM, Step, call, fail  # noqa: E402
from app.orchestrator.llm_client import LLMErrorKind  # noqa: E402
from app.sandbox.docker_cli import ContainerPresence, DockerCli  # noqa: E402
from app.sandbox.process import SubprocessRunner  # noqa: E402
from app.sandbox.runner import DockerSandbox  # noqa: E402
from app.selection.selector import ProviderOption, ProviderSelector  # noqa: E402
from app.tools.adapters.coding import CodingRuntime, recover_attempts  # noqa: E402
from app.tools.adapters.opencode import OpenCodeClient, verify_contract  # noqa: E402
from app.tools.adapters.workspace_fs import WorkspaceFS  # noqa: E402
from edge.filter_policy import OPERATIONS, Policy  # noqa: E402
from edge.filter_policy import TOKEN_ENV as FILTER_TOKEN_ENV  # noqa: E402
from edge.gateway_policy import TOKEN_ENV as GATEWAY_TOKEN_ENV  # noqa: E402
from edge.gateway_policy import UPSTREAM_KEY_ENV  # noqa: E402

HERE = Path(__file__).resolve().parent
CHECK_ROOT = Path(r"D:\DevPilotCache\launcher-check")
REPORTS = Path(r"D:\DevPilotCache\opencode-check\reports")
MANIFEST = Path(r"D:\DevPilotCache\opencode\1.19.5\manifest.json")
MODEL = "openai/gpt-6.1-sol"
MODEL_ID = "gpt-6.1-sol"
API = "/api/v1"
SENTINEL = "ORIGINAL_ONLY_SENTINEL.txt"
FORWARD_PORT_HINT = "127.0.0.1"
ENGINE_REQUESTS = 60  # 22 prompts at two model turns each, with room: the check's limits must exceed its own script
ENGINE_TOKENS = 2_000_000  # the reservation is sized by the request's bytes, and the session's history grows
STEP_SECONDS = 30.0  # a scripted step takes under 7 s
FILTER_SLOTS = 32  # edge/filter_server.py MAX_CONNECTIONS, which this check must not change
STANDBY_GAP_SECONDS = 15.0
BRIDGE = (
    "import os, socket, threading\n"
    "s = socket.create_connection(('127.0.0.1', 8080))\n"
    "def up():\n"
    "    while True:\n"
    "        d = os.read(0, 65536)\n"
    "        if not d:\n"
    "            try:\n"
    "                s.shutdown(socket.SHUT_WR)\n"
    "            except OSError:\n"
    "                pass\n"
    "            return\n"
    "        s.sendall(d)\n"
    "threading.Thread(target=up, daemon=True).start()\n"
    "while True:\n"
    "    d = s.recv(65536)\n"
    "    if not d:\n"
    "        break\n"
    "    os.write(1, d)\n"
)
NET_PROBE = """
import json, socket, sys
out = {}
for label, host, port in json.load(sys.stdin):
    try:
        socket.create_connection((host, port), timeout=3).close()
        out[label] = "connected"
    except OSError as exc:
        out[label] = f"{type(exc).__name__}: {exc}"
try:
    socket.getaddrinfo("example.com", 443)
    out["dns example.com"] = "resolved"
except OSError as exc:
    out["dns example.com"] = f"{type(exc).__name__}: {exc}"
print(json.dumps(out))
"""
HTTP_PROBE = """
import json, socket, sys
out = []
for item in json.load(sys.stdin):
    data = bytearray()
    try:
        with socket.create_connection((item["host"], item["port"]), timeout=30) as conn:
            conn.sendall(item["raw"].encode("latin-1"))
            conn.shutdown(socket.SHUT_WR)
            while True:
                chunk = conn.recv(65536)
                if not chunk:
                    break
                data.extend(chunk)
    except OSError as exc:
        out.append({"id": item["id"], "error": type(exc).__name__})
        continue
    head, _, body = bytes(data).partition(b"\\r\\n\\r\\n")
    line = head.split(b"\\r\\n")[0].split(b" ")
    out.append({"id": item["id"], "status": int(line[1]) if len(line) > 1 and line[1].isdigit() else None, "body": body[:300].decode("utf-8", "replace")})
print(json.dumps(out))
"""
AUDIT_LATENCY = """
import json, os, sys, time
def measure(path):
    spent = []
    with open(path, "a", encoding="utf-8", buffering=1) as stream:
        for _ in range(400):
            began = time.perf_counter()
            stream.write("x" * 120 + "\\n")
            stream.flush()
            spent.append((time.perf_counter() - began) * 1000)
    os.unlink(path)
    spent.sort()
    return {"p50_ms": round(spent[200], 3), "p99_ms": round(spent[396], 3), "max_ms": round(spent[-1], 3)}
out = {}
for label, path in json.load(sys.stdin):
    try:
        out[label] = measure(path)
    except OSError as exc:
        out[label] = f"{type(exc).__name__}: {exc}"
print(json.dumps(out))
"""
WRITE_PROBE = """
import json, os, stat, time
out = {"uid": os.getuid(), "gid": os.getgid()}
def step(name, action):
    try:
        action()
        out[name] = "ok"
    except OSError as exc:
        out[name] = f"refused: {type(exc).__name__} errno {exc.errno}"
step("create", lambda: open("/workspace/probe.txt", "w").write("from the container\\n"))
step("modify the copied file", lambda: open("/workspace/README.md", "a").write("changed in the container\\n"))
step("nested folders", lambda: os.makedirs("/workspace/pkg/sub"))
step("nested file", lambda: open("/workspace/pkg/sub/mod.py", "w").write("x = 1\\n"))
step("rename", lambda: os.rename("/workspace/probe.txt", "/workspace/renamed.txt"))
step("temporary", lambda: open("/workspace/gone.txt", "w").write("x"))
step("delete", lambda: os.remove("/workspace/gone.txt"))
step("outside: etc", lambda: open("/etc/devpilot-outside", "w").write("x"))
step("outside: root", lambda: open("/devpilot-outside", "w").write("x"))
step("outside: managed config", lambda: open("/etc/opencode/opencode.json", "a").write("x"))
step("outside: binary", lambda: open("/opt/opencode/opencode", "ab").write(b"x"))
step("outside: rg", lambda: open("/usr/local/bin/rg", "ab").write(b"x"))
s = os.stat("/workspace/renamed.txt")
out["owner"] = [s.st_uid, s.st_gid]
out["mtime_ns"] = s.st_mtime_ns
found = []
for folder, dirs, files in os.walk("/"):
    if folder.startswith(("/proc", "/sys", "/dev")):
        dirs[:] = []
        continue
    if "ORIGINAL_ONLY_SENTINEL.txt" in files:
        found.append(folder)
out["sentinel_found_in"] = found
print(json.dumps(out))
"""


def steps_marker(steps: list[dict[str, Any]]) -> str:
    return "DEVPILOT-STEPS:" + base64.b64encode(json.dumps(steps).encode()).decode() + ":END"


class Results:
    def __init__(self) -> None:
        self.rows: list[dict[str, Any]] = []
        self.scenario = ""

    def check(self, label: str, passed: bool, detail: Any = "") -> bool:
        self.rows.append({"scenario": self.scenario, "label": label, "result": "PASS" if passed else "FAIL", "detail": detail, "at": round(time.monotonic(), 1)})
        print(f"  {'PASS' if passed else 'FAIL'}  {label}" + (f"  [{str(detail)[:150]}]" if detail != "" else ""), flush=True)
        return passed

    def note(self, label: str, detail: Any) -> None:
        self.rows.append({"scenario": self.scenario, "label": label, "result": "NOTE", "detail": detail})
        print(f"  NOTE  {label}  [{str(detail)[:170]}]", flush=True)

    @property
    def counts(self) -> dict[str, int]:
        return {k: sum(1 for r in self.rows if r["result"] == k) for k in ("PASS", "FAIL", "NOTE")}


class ExecForwarder:
    """Stands in for `--publish`. Each connection on a loopback port becomes one `docker exec` into the filter."""

    def __init__(self, container_id: str) -> None:
        self._container = container_id
        self._listener = socket.create_server(("127.0.0.1", 0))
        self.port = self._listener.getsockname()[1]
        self._closed = threading.Event()
        threading.Thread(target=self._accept, daemon=True).start()

    def close(self) -> None:
        self._closed.set()
        self._listener.close()

    def _accept(self) -> None:
        while not self._closed.is_set():
            try:
                conn, _ = self._listener.accept()
            except OSError:
                return
            threading.Thread(target=self._serve, args=(conn,), daemon=True).start()

    def _serve(self, conn: socket.socket) -> None:
        proc = subprocess.Popen(["docker", "exec", "-i", self._container, "python", "-I", "-c", BRIDGE], stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, bufsize=0)
        assert proc.stdin is not None and proc.stdout is not None

        def down() -> None:
            while True:
                data = proc.stdout.read(65536)
                if not data:
                    break
                try:
                    conn.sendall(data)
                except OSError:
                    break
            try:
                conn.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass

        reader = threading.Thread(target=down, daemon=True)
        reader.start()
        try:
            while True:
                data = conn.recv(65536)
                if not data:
                    break
                proc.stdin.write(data)
                proc.stdin.flush()
            proc.stdin.close()
            reader.join(timeout=180)
        except OSError:
            pass
        finally:
            conn.close()
            proc.kill()
            proc.wait(timeout=10)


class NoModelLauncher(OpenCodeLauncher):
    """The actual launcher with less: two internal networks, nothing published, no key, a scripted upstream."""

    def __init__(self, settings: Settings, cli: DockerCli, *, script: list[dict[str, Any]] | None = None, gateway: dict[str, Any] | None = None, broken_stop: bool = False) -> None:
        super().__init__(settings, cli, None, sleep=time.sleep)  # type: ignore[arg-type]  # the published address is not used
        self._script = script or []
        self._gateway = gateway or {}
        self._broken_stop = broken_stop
        self.plan: Any = None
        self.forwarder: ExecForwarder | None = None
        self.filter_id = ""
        self.container: Any = None

    def unavailable_reason(self) -> str | None:
        s = self._settings
        problems = []
        if s.opencode_runtime_image not in APPROVED_RUNTIME_IMAGES:
            problems.append("the image is not the approved runtime image")
        if s.openai_api_key is not None:
            problems.append("a no-model check must hold no provider key")
        return "; ".join(problems) or None

    def networks(self) -> tuple[tuple[str, bool], ...]:
        return ((CORE_NETWORK, True), (MODEL_NETWORK, True))

    def _new_plan(self, request: LaunchRequest) -> Any:
        self.plan = super()._new_plan(request)
        return self.plan

    def _gateway_env(self, plan: Any) -> dict[str, str]:
        return {GATEWAY_TOKEN_ENV: plan.gateway_token}

    def _write_files(self, request: LaunchRequest, plan: Any) -> Any:
        container = super()._write_files(request, plan)
        self.container = container
        edge = plan.folder / "edge"
        shutil.copyfile(HERE / "opencode_check" / "fake_model_gateway.py", edge / "fake_model_gateway.py")
        shutil.copyfile(HERE / "opencode_check" / "filter_probe.py", edge / "filter_probe.py")
        (edge / "fake-model.json").write_text(json.dumps(self._script), encoding="utf-8")
        config = json.loads((edge / "gateway.json").read_text(encoding="utf-8"))
        (edge / "gateway.json").write_text(json.dumps({**config, **self._gateway}), encoding="utf-8")
        return container

    def _gateway_args(self, plan: Any) -> list[str]:
        return edge_args(
            plan.gateway_name, networks=(MODEL_NETWORK,), publish=(), env_names=(GATEWAY_TOKEN_ENV,),
            mounts=((plan.folder / "edge", EDGE_MOUNT, True), (plan.folder / "ledger", LEDGER_MOUNT, False)),
            argv=("python", "-I", "-B", f"{EDGE_MOUNT}/fake_model_gateway.py", "--config", f"{EDGE_MOUNT}/gateway.json", "--script", f"{EDGE_MOUNT}/fake-model.json",
                  "--listen-port", str(GATEWAY_PORT), "--audit", f"{LEDGER_MOUNT}/gateway-audit.jsonl", "--ledger", f"{LEDGER_MOUNT}/ledger.jsonl"),
        )  # fmt: skip

    def _filter_args(self, plan: Any) -> list[str]:
        args = super()._filter_args(plan)
        for flag, value in (("--network", INGRESS), ("--publish", None)):
            while flag in args and (value is None or args[args.index(flag) + 1] == value):
                at = args.index(flag)
                del args[at : at + 2]
        return args

    def _filter_url(self, filter_id: str) -> str:
        self.filter_id = filter_id
        self.forwarder = ExecForwarder(filter_id)
        return f"http://127.0.0.1:{self.forwarder.port}"

    def start(self, request: LaunchRequest) -> Any:
        session = super().start(request)
        close, forwarder = session.close, self.forwarder

        def close_all() -> None:
            close()
            if forwarder is not None:
                forwarder.close()

        session.close = close_all
        if self._broken_stop:  # the daemon cannot be asked: a CLI that is not there
            session.writer = ContainerTerminator(DockerCli("docker-is-not-installed-here", SubprocessRunner()), session.container_id)
        return session


INGRESS = "devpilot-ingress"


@dataclass
class World:
    results: Results
    root: Path
    cli: DockerCli = field(default_factory=lambda: DockerCli("docker", SubprocessRunner()))
    marker: Path | None = None
    scenario_began: float = field(default_factory=time.monotonic)

    def settings(self, name: str, **overrides: Any) -> Settings:
        folder = self.root / name
        folder.mkdir(parents=True, exist_ok=True)
        values: dict[str, Any] = {
            "devpilot_api_token": secrets.token_urlsafe(32), "database_url": f"sqlite:///{(folder / 'check.db').as_posix()}", "workspaces_dir": folder / "workspaces",
            "coding_checkpoints_dir": folder / "checkpoints", "coding_work_dir": folder / "work", "opencode_model": MODEL, "opencode_manifest_file": MANIFEST,
            "opencode_runtime_image": next(iter(APPROVED_RUNTIME_IMAGES)), "openai_api_key": None, "opencode_server_password": None, "log_level": "WARNING",
            "openai_price_input_per_mtok": 2.0, "openai_price_output_per_mtok": 10.0, "gateway_max_usd_per_task": 0.75, "gateway_max_requests_per_task": 20, **overrides,
        }  # fmt: skip
        return Settings(_env_file=None, **values)

    def project(self, settings: Settings, *, seed: dict[str, str] | None = None) -> tuple[WorkspaceFS, str, str, Path]:
        """A real project, its disposable copy, and a sentinel added afterwards, so only the real project holds it."""
        real = WorkspaceFS(settings.workspaces_dir, max_file_bytes=2_000_000, max_files=100)
        project_id, run_id = new_id(), new_id()
        real.create_project(project_id)
        for rel, content in (seed or {"README.md": "# The user's project\n"}).items():
            real.write_text(project_id, rel, content)
        copies = CandidateWorkspaces(WorkspaceFS(settings.coding_work_dir, max_file_bytes=2_000_000, max_files=100))
        copy = copies.open_or_create(run_id, real.project_dir(project_id))
        real.write_text(project_id, SENTINEL, "only the real project has this\n")
        return real, project_id, run_id, copy

    def leftovers(self) -> dict[str, list[str]]:
        containers = [cid for label in (CONTAINER_LABEL, EDGE_LABEL, "devpilot.sandbox") for cid in self.cli.labelled_containers(label)]
        networks = [n for n in _docker("network", "ls", "--format", "{{.Name}}").stdout.split() if n.startswith("devpilot-")]
        return {"containers": containers, "networks": networks}


def _docker(*args: str, stdin: str | None = None, timeout: int = 120) -> subprocess.CompletedProcess[str]:
    return subprocess.run(["docker", *args], input=stdin, capture_output=True, encoding="utf-8", errors="replace", timeout=timeout)


def _exec_python(container: str, code: str, stdin: str = "") -> Any:
    done = _docker("exec", "-i", container, "python", "-I", "-c", code, stdin=stdin)
    if done.returncode != 0:
        return {"exec_failed": done.stderr.strip()[-300:]}
    return json.loads(done.stdout)


def _tcp(host: str, port: int) -> str:
    try:
        socket.create_connection((host, port), timeout=2).close()
        return "connected"
    except OSError as exc:
        return type(exc).__name__


class StepFailed(RuntimeError):
    """A scripted step cannot finish, so the scenario stops here rather than wait out the steps after it."""


def refused(parts: list[dict[str, Any]]) -> bool:
    """A tool was attempted and did not complete. No part at all is no evidence: nothing was attempted."""
    return bool(parts) and all(p["status"] != "completed" for p in parts)


def _wait_idle(client: OpenCodeClient, session_id: str, *, seconds: float = 60.0) -> bool:
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        status = client.statuses().get(session_id)
        if isinstance(status, dict) and status.get("type") == "retry":
            # The script cannot fail, so something refused the request. OpenCode's own retries last about 65 s, and
            # a prompt sent meanwhile lands on a busy session and is never run, so waiting can only hide the cause.
            raise StepFailed(f"OpenCode is retrying a refused request: {status.get('message')}")
        if not (isinstance(status, dict) and status.get("type") == "busy"):
            messages = client.messages(session_id)
            last = messages[-1]["info"] if messages else {}
            if last.get("role") == "assistant" and (last.get("time") or {}).get("completed"):
                return True
        time.sleep(1.0)
    return False


def _prompt(session_id: str, client: OpenCodeClient, steps: list[dict[str, Any]]) -> int | None:
    body = {"agent": PROMPTED_AGENT, "model": {"providerID": "openai", "modelID": MODEL_ID}, "parts": [{"type": "text", "text": f"Scripted check, no model. {steps_marker(steps)}"}]}
    return client.prompt_async(session_id, body)


def _tool_parts(client: OpenCodeClient, session_id: str) -> list[dict[str, Any]]:
    found = []
    for message in client.messages(session_id):
        for part in message.get("parts") or []:
            if part.get("type") == "tool":
                state = part.get("state") or {}
                found.append({"tool": part.get("tool"), "status": state.get("status"), "error": str(state.get("error") or "")[:160], "path": (state.get("input") or {}).get("filePath"), "args": sorted(state.get("input") or {})})
    return found


def _audit(plan: Any) -> list[dict[str, Any]]:
    path = plan.folder / "ledger" / "filter-audit.jsonl"
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()] if path.is_file() else []


def _ledger(folder: Path, name: str = "ledger.jsonl") -> list[dict[str, Any]]:
    path = folder / name
    return ledger_rows(path)[0] if path.is_file() else []


def _start(world: World, name: str, *, launcher_args: dict[str, Any] | None = None, token_budget: int = 50_000, **settings: Any) -> tuple[Settings, NoModelLauncher, Any, LaunchRequest, tuple[WorkspaceFS, str, str, Path]]:
    config = world.settings(name, **settings)
    launcher = NoModelLauncher(config, world.cli, **(launcher_args or {}))
    project = world.project(config)
    request = LaunchRequest(run_id=project[2], call_id=f"call_{name}", workspace=project[3], token_budget=token_budget, usd_budget=None)
    return config, launcher, launcher.start(request), request, project


# --- Scenario 1: boundaries, with no prompt ---------------------------------------------------------


def boundaries(world: World) -> None:
    r = world.results
    config, launcher, session, _request, (real, project_id, _run, copy) = _start(world, "boundaries")
    plan = launcher.plan
    opencode = session.container_id
    client: OpenCodeClient = session._client
    try:
        found = world.cli.inspect_json(opencode) or {}
        r.check("1. OpenCode runs the approved runtime image", found.get("Image") in APPROVED_RUNTIME_IMAGES, found.get("Image"))
        problems = deployment_problems(found, launcher.container)
        r.check("1. the running container matches its definition", launcher.container is not None and problems == [], problems[:3])
        r.check("   runs as user 65534 with a read-only root and no capabilities", found["Config"]["User"] == "65534:65534" and found["HostConfig"]["ReadonlyRootfs"] is True and "ALL" in [c.upper() for c in found["HostConfig"]["CapDrop"]])
        r.check("   only the two internal networks", sorted(found["NetworkSettings"]["Networks"]) == sorted([CORE_NETWORK, MODEL_NETWORK]), sorted(found["NetworkSettings"]["Networks"]))
        for name in (CORE_NETWORK, MODEL_NETWORK):
            r.check(f"   {name} is internal", (world.cli.inspect_network(name) or {}).get("Internal") is True)
        r.check("   no non-internal DevPilot network exists", not [n for n in world.leftovers()["networks"] if n not in (CORE_NETWORK, MODEL_NETWORK)])
        for label, cid in (("OpenCode", opencode), ("filter", launcher.filter_id)):
            ports = (world.cli.inspect_json(cid) or {}).get("HostConfig", {}).get("PortBindings")
            r.check(f"   {label} publishes no port", not ports, ports)

        url = f"http://127.0.0.1:{launcher.forwarder.port}"
        for label, token in (("no token", None), ("a wrong token", "x" * 43), ("the gateway's token", plan.gateway_token), ("OpenCode's password", plan.password)):
            status = _http_status(url, "/devpilot/health", token)
            r.check(f"2. the filter refuses {label}", status == 401, status)
        r.check("2. the filter accepts this run's token", _http_status(url, "/devpilot/health", plan.filter_token) == 200)
        r.check("3. OpenCode answers through the filter", client.health() == "1.19.5", client.health())
        contract = verify_contract(client)
        r.check("3. its API matches the reviewed contract", contract.version == "1.19.5")

        planted = {"opencode.json": json.dumps({"permission": {"*": "allow", "bash": "allow"}}), ".opencode/agent/planted.md": "---\ndescription: planted\nmode: primary\n---\nplanted\n"}
        for rel, content in planted.items():
            target = copy / rel
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(content, encoding="utf-8")
        agents = client.agents()
        names = [a.get("name") for a in agents]
        problems = verify_effective_config(client.config(), agents)
        r.check("6. a project opencode.json and .opencode/ placed after start change nothing", problems == [] and "planted" not in names, problems[:3] or names)
        env = found["Config"]["Env"]
        r.check("6. OPENCODE_DISABLE_PROJECT_CONFIG=1 is set", "OPENCODE_DISABLE_PROJECT_CONFIG=1" in env)
        shutil.rmtree(copy / ".opencode")
        (copy / "opencode.json").unlink()
        r.check("7. only reviewed tools are offered", tool_inventory_problems(client.tool_ids(), contract.version, agents) == [])
        r.check("7. no agent beyond build and OpenCode's hidden system agents", set(names) <= {"build", "compaction", "summary", "title"}, names)

        refused_groups, busy, rows = _refusals(launcher, client)
        for label, (total, bad) in refused_groups.items():
            r.check(f"4. {label}: {total} refused", not bad, bad[:3] if bad else "")
        r.check("4. the audit holds every one of them, and none was forwarded", len(rows) >= sum(t for t, _ in refused_groups.values()) and not [e for e in rows if e.get("forwarded")], {"decisions": len(rows), "forwarded": [e for e in rows if e.get("forwarded")][:3], "answered_busy_first": busy})
        approved = {o.template for o in OPERATIONS}
        strays = [e for e in _audit(plan) if e.get("forwarded") and e.get("operation") not in approved]
        r.check("4. in the whole run only approved operations were forwarded", strays == [], strays[:2])
        _overload(r, launcher, opencode)

        core_ip = found["NetworkSettings"]["Networks"][CORE_NETWORK]["IPAddress"]
        r.check("5. nothing listens on the host's 127.0.0.1:4096", _tcp("127.0.0.1", 4096) != "connected", _tcp("127.0.0.1", 4096))
        r.check("5. the host cannot reach OpenCode's container address", _tcp(core_ip, 4096) != "connected", f"{core_ip}: {_tcp(core_ip, 4096)}")
        _network_probes(r, plan, opencode, launcher.filter_id, world)

        direct = _http_inside(launcher.filter_id, [{"id": "filter token on OpenCode", "host": plan.opencode_name, "port": 4096, "raw": _raw_get("/global/health", f"Bearer {plan.filter_token}")},
                                                   {"id": "no credential on OpenCode", "host": plan.opencode_name, "port": 4096, "raw": _raw_get("/global/health", None)}])  # fmt: skip
        for item in direct:
            r.check(f"5. {item['id']} gets 401", item.get("status") == 401, item.get("status"))

        rg = _docker("exec", opencode, "rg", "--version")
        where = _exec_python(opencode, "import json, shutil\nprint(json.dumps(shutil.which('rg')))")
        r.check("10. the runtime finds the pinned rg on PATH", rg.returncode == 0 and "ripgrep 15.1.0" in rg.stdout and where == "/usr/local/bin/rg", (where, rg.stdout.splitlines()[:1]))

        write = _exec_python(opencode, WRITE_PROBE)
        _write_results(r, write, copy, real, project_id)

        names_seen = [e.split("=", 1)[0] for e in env]
        r.check("13. no provider credential in OpenCode's environment", not [n for n in names_seen if any(p in n.upper() for p in ("OPENAI", "ANTHROPIC", "API_KEY"))], "")
        gateway_env = (world.cli.inspect_json(_id_of(world, plan.gateway_name)) or {}).get("Config", {}).get("Env", [])
        r.check("13. no provider key in the gateway's environment", not [e for e in gateway_env if e.startswith("DEVPILOT_GATEWAY_OPENAI_KEY")])
        r.check("13. the settings hold no provider key", config.openai_api_key is None)
    finally:
        cid = session.container_id
        session.close()
    stop = world.cli.inspect_container(cid, expected_id=cid)
    r.check("11. the stop is confirmed by an independent read", stop.presence is ContainerPresence.NOT_FOUND, stop.detail)
    left = world.leftovers()
    r.check("12. no container or network remains", left == {"containers": [], "networks": []}, left)
    r.check("12. the attempt's folder is gone and its logs are kept", not list((config.coding_work_dir / "attempts").iterdir()) and bool(list((config.coding_work_dir / "evidence").iterdir())))
    r.check("    the original project is intact", real.read_text(project_id, "README.md") == "# The user's project\n")


def _id_of(world: World, name: str) -> str:
    return _docker("container", "inspect", "--format", "{{.Id}}", name).stdout.strip()


def _http_status(url: str, path: str, token: str | None) -> int:
    import httpx

    headers = {"Authorization": f"Bearer {token}"} if token else {}
    return httpx.get(url + path, headers=headers, timeout=20).status_code


def _raw_get(path: str, authorization: str | None) -> str:
    lines = [f"GET {path} HTTP/1.1", "Host: check", "Connection: close"]
    if authorization:
        lines.append(f"Authorization: {authorization}")
    return "\r\n".join([*lines, "", ""])


def _http_inside(container: str, requests: list[dict[str, Any]]) -> list[dict[str, Any]]:
    answer = _exec_python(container, HTTP_PROBE, json.dumps(requests))
    return answer if isinstance(answer, list) else [{"id": "probe", "error": answer}]


def _session_count(opencode: str) -> int:
    """How many sessions OpenCode itself says it created. A witness inside OpenCode, not the filter's word."""
    done = _docker("logs", opencode)
    return len(re.findall(r"message=created id=ses_", done.stdout + done.stderr))


def _send(launcher: NoModelLauncher, items: list[bytes], **options: Any) -> list[dict[str, Any]]:
    request = {"port": 8080, **options, "requests": [{"id": str(i), "raw_b64": base64.b64encode(raw).decode()} for i, raw in enumerate(items)]}
    done = _docker("exec", "-i", launcher.filter_id, "python", "-I", f"{EDGE_MOUNT}/filter_probe.py", stdin=json.dumps(request), timeout=60)
    return json.loads(done.stdout) if done.returncode == 0 else [{"id": "probe", "error": done.stderr[-200:]}]


def _overload(r: Results, launcher: NoModelLauncher, opencode: str) -> None:
    """Fill the filter on purpose. Whatever it answers then, nothing may reach OpenCode, and each refusal is recorded."""
    plan = launcher.plan
    token = plan.filter_token
    new_session = filter_check.request_bytes("POST", f"/session?{filter_check.QUERY}", body=json.dumps({"title": "overload", "permission": session_rules()}).encode(), token=token)
    forbidden = filter_check.request_bytes("POST", f"/session/{filter_check.INERT_SESSION}/shell?{filter_check.QUERY}", body=b'{"command":"id"}', token=token)
    sessions, before = _session_count(opencode), len(_audit(plan))
    answers = _send(launcher, [new_session, forbidden, new_session, forbidden, new_session], hold=FILTER_SLOTS, settle=1.5)
    time.sleep(1.0)  # the held connections are decided as they close
    rows = _audit(plan)[before:]
    statuses = [a.get("status") or a.get("error") for a in answers]
    r.check("overload: with every slot taken, all five requests are refused 503", statuses == [503] * 5, statuses)
    r.check("overload: each refusal is in the audit log, and nothing was forwarded", sum(1 for e in rows if e.get("status") == 503) == 5 and not [e for e in rows if e.get("forwarded")], f"{len(rows)} decisions, {sum(1 for e in rows if e.get('forwarded'))} forwarded")
    r.check("overload: OpenCode's own log shows no session was created", _session_count(opencode) == sessions, f"{sessions} sessions before and after")
    time.sleep(1.0)
    answer = _send(launcher, [new_session])
    r.check("overload: the filter recovers by itself and answers normally", answer[0].get("status") == 200, answer[0].get("status"))
    time.sleep(0.5)
    r.check("overload: the witness works: that one session does appear in OpenCode's log", _session_count(opencode) == sessions + 1, f"{sessions} became {_session_count(opencode)}")
    before = len(_audit(plan))
    burst = _send(launcher, [forbidden] * 300, workers=16)
    time.sleep(1.5)
    rows = _audit(plan)[before:]
    seen = Counter(a.get("status") or a.get("error") for a in burst)
    r.check("overload: 300 forbidden requests from 16 clients at once are all refused", set(seen) <= {403, 503}, dict(seen))
    r.check("overload: none was forwarded, and the audit log agrees with the clients", not [e for e in rows if e.get("forwarded")] and Counter(e.get("status") for e in rows) == seen, dict(Counter(e.get("status") for e in rows)))
    errors = _docker("logs", launcher.filter_id).stderr
    r.note("what the filter itself reported under that burst", {"no thread available": errors.count("can't start new thread"), "tracebacks": errors.count("Traceback"), "last lines": errors.strip().splitlines()[-3:]})


def _refusals(launcher: NoModelLauncher, client: OpenCodeClient) -> tuple[dict[str, tuple[int, list[Any]]], int, list[dict[str, Any]]]:
    plan = launcher.plan
    token = plan.filter_token
    model = {"providerID": "openai", "modelID": MODEL_ID}
    policy = Policy.from_config({"directory": "/workspace", "provider_id": "openai", "model_id": MODEL_ID, "session_rules": session_rules()})
    denied, _allowed = filter_check.denied_operations(client.spec(), policy)
    target = f"/session/{filter_check.INERT_SESSION}/prompt_async?{filter_check.QUERY}"
    groups: dict[str, tuple[list[tuple[str, bytes]], set[int]]] = {
        "every other operation in the server's spec": ([(f"{m} {t}", filter_check.request_bytes(m, t, token=token)) for m, t in denied], {403}),
        "unknown paths (C13)": ([(p, filter_check.request_bytes("GET", p, token=token)) for p in filter_check.UNKNOWN_PATHS], {403}),
        "unknown methods": ([(m, filter_check.request_bytes(m, "/config", token=token)) for m in filter_check.UNKNOWN_METHODS], {403}),
        "encoded and normalized paths": ([(p, filter_check.request_bytes("GET", p, token=token)) for p in filter_check.PATH_TRICKS], {400, 403}),
        "unapproved directories": ([(d, filter_check.request_bytes("GET", f"/config?{d}", token=token)) for d in filter_check.DIRECTORIES], {400, 403}),
        "unapproved prompts: other agents, models, parts and the tools field": (
            [(n, filter_check.request_bytes("POST", target, body=json.dumps({**p, "model": p["model"] if n in ("another model", "another provider") else model} if "model" in p else p).encode(), token=token))
             for n, p in filter_check.REFUSED_PROMPTS.items()], {403}),
        "unapproved sessions": ([(n, filter_check.request_bytes("POST", f"/session?{filter_check.QUERY}", body=json.dumps(p).encode(), token=token)) for n, p in filter_check.REFUSED_SESSIONS.items()], {403}),
        "granting a permission": ([("allow once", filter_check.request_bytes("POST", f"/permission/per_x/reply?{filter_check.QUERY}", body=b'{"reply":"once"}', token=token))], {403}),
    }  # fmt: skip
    results: dict[str, tuple[int, list[Any]]] = {}
    busy = 0
    before = len(_audit(plan))  # after `client.spec()`, which is approved and is forwarded
    for label, (items, statuses) in groups.items():
        pending = dict(items)
        answers: dict[str, Any] = {}
        # A burst can fill the filter's connection slots, and it then answers 503 and forwards nothing. So
        # the same request is sent again once the burst is over, and only its real answer counts.
        for _ in range(4):
            request = {"port": 8080, "requests": [{"id": name, "raw_b64": base64.b64encode(raw).decode()} for name, raw in pending.items()]}
            done = _docker("exec", "-i", launcher.filter_id, "python", "-I", f"{EDGE_MOUNT}/filter_probe.py", stdin=json.dumps(request), timeout=180)
            batch = json.loads(done.stdout) if done.returncode == 0 else [{"id": "probe", "error": done.stderr[-200:]}]
            answers.update({a.get("id"): a for a in batch})
            pending = {a["id"]: pending[a["id"]] for a in batch if a.get("status") == 503 and a.get("id") in pending}
            busy += len(pending)
            if not pending:
                break
            time.sleep(1.0)
        results[label] = (len(answers), [(a.get("id"), a.get("status") or a.get("error")) for a in answers.values() if a.get("status") not in statuses])
    return results, busy, _audit(plan)[before:]


def _network_probes(r: Results, plan: Any, opencode: str, filter_id: str, world: World) -> None:
    external = ["public address 1.1.1.1:53", "1.1.1.1", 53]
    gateway_id = _id_of(world, plan.gateway_name)
    from_opencode = _exec_python(opencode, NET_PROBE, json.dumps([external, ["gateway", plan.gateway_name, GATEWAY_PORT], ["filter", plan.filter_name, 8080]]))
    from_filter = _exec_python(filter_id, NET_PROBE, json.dumps([external, ["gateway", plan.gateway_name, GATEWAY_PORT], ["opencode", plan.opencode_name, 4096]]))
    from_gateway = _exec_python(gateway_id, NET_PROBE, json.dumps([external, ["filter", plan.filter_name, 8080], ["opencode", plan.opencode_name, 4096]]))
    for label, answer in (("OpenCode", from_opencode), ("the filter", from_filter), ("the gateway", from_gateway)):
        r.check(f"5. {label} has no route out", answer.get("public address 1.1.1.1:53") != "connected", answer.get("public address 1.1.1.1:53"))
        r.check(f"5. {label} cannot resolve external names", answer.get("dns example.com") != "resolved", answer.get("dns example.com"))
    r.check("5. OpenCode reaches the gateway, its only way to a model", from_opencode.get("gateway") == "connected", from_opencode.get("gateway"))
    r.check("5. the filter reaches OpenCode", from_filter.get("opencode") == "connected", from_filter.get("opencode"))
    r.check("5. the filter cannot reach the gateway", from_filter.get("gateway") != "connected", from_filter.get("gateway"))
    r.check("5. the gateway cannot reach the filter", from_gateway.get("filter") != "connected", from_gateway.get("filter"))
    answer = _http_inside(gateway_id, [{"id": "no password", "host": plan.opencode_name, "port": 4096, "raw": _raw_get("/global/health", None)}])
    r.check("5. the gateway's network peer OpenCode still demands its password", answer[0].get("status") == 401, answer[0].get("status"))


def _write_results(r: Results, write: dict[str, Any], copy: Path, real: WorkspaceFS, project_id: str) -> None:
    r.check("8. Docker Desktop: user 65534 is the writer", write.get("uid") == 65534 and write.get("gid") == 65534, (write.get("uid"), write.get("gid")))
    for name in ("create", "modify the copied file", "nested folders", "nested file", "rename", "temporary", "delete"):
        r.check(f"8. in the disposable copy: {name}", write.get(name) == "ok", write.get(name))
    for name in ("outside: etc", "outside: root", "outside: managed config", "outside: binary", "outside: rg"):
        r.check(f"8. {name} is refused", str(write.get(name, "")).startswith("refused"), write.get(name))
    listing = sorted(p.relative_to(copy).as_posix() for p in copy.rglob("*"))
    r.check("8. the changes appear on the host in the copy", {"renamed.txt", "pkg/sub/mod.py"} <= set(listing) and "gone.txt" not in listing, listing)
    mtime = (copy / "renamed.txt").stat().st_mtime_ns
    r.check("8. modification times agree with the host", abs(mtime - write.get("mtime_ns", 0)) < 2_000_000_000, f"{abs(mtime - write.get('mtime_ns', 0)) / 1e6:.1f} ms apart")
    r.check("9. the original project is not visible in the container", write.get("sentinel_found_in") == [], write.get("sentinel_found_in"))
    r.check("9. the container's writes did not reach the original project", "changed in the container" not in real.read_text(project_id, "README.md") and not (real.project_dir(project_id) / "renamed.txt").exists())


# --- Scenario 2: the gateway over the real internal network --------------------------------------------


def _gateway_request(token: str | None, *, model: str = MODEL_ID, path: str = "/v1/responses", body: dict[str, Any] | None = None, method: str = "POST") -> str:
    payload = json.dumps(body if body is not None else {"model": model, "input": [{"role": "user", "content": [{"type": "input_text", "text": "hello"}]}], "stream": True, "tools": [{"type": "function", "name": "read"}]})
    lines = [f"{method} {path} HTTP/1.1", "Host: gateway", "Connection: close", "Content-Type: application/json", f"Content-Length: {len(payload)}"]
    if token is not None:
        lines.append(f"Authorization: Bearer {token}")
    return "\r\n".join([*lines, "", ""]) + payload


def gateway(world: World) -> None:
    r = world.results
    _config, launcher, session, _request, _project = _start(world, "gateway")
    plan = launcher.plan
    opencode = session.container_id
    edge, ledger_root = plan.folder / "edge", plan.folder / "ledger"
    extra: list[str] = []
    token = secrets.token_urlsafe(32)

    def probe_gateway(name: str, config: dict[str, Any], script: list[dict[str, Any]], requests: list[str]) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
        (edge / f"gw-{name}.json").write_text(json.dumps({"model": MODEL_ID, "max_output_tokens": 1_000, **config}), encoding="utf-8")
        (edge / f"gw-{name}-script.json").write_text(json.dumps(script), encoding="utf-8")
        container = f"{plan.gateway_name}-{name}"
        args = edge_args(container, networks=(MODEL_NETWORK,), publish=(), env_names=(GATEWAY_TOKEN_ENV,),
                         mounts=((edge, EDGE_MOUNT, True), (ledger_root, LEDGER_MOUNT, False)),
                         argv=("python", "-I", "-B", f"{EDGE_MOUNT}/fake_model_gateway.py", "--config", f"{EDGE_MOUNT}/gw-{name}.json", "--script", f"{EDGE_MOUNT}/gw-{name}-script.json",
                               "--listen-port", str(GATEWAY_PORT), "--audit", f"{LEDGER_MOUNT}/gw-{name}-audit.jsonl", "--ledger", f"{LEDGER_MOUNT}/gw-{name}.jsonl"))  # fmt: skip
        extra.append(world.cli.run_detached(args, env={GATEWAY_TOKEN_ENV: token}))
        for _ in range(40):
            if _exec_python(opencode, NET_PROBE, json.dumps([["g", container, GATEWAY_PORT]])).get("g") == "connected":
                break
            time.sleep(0.25)
        answers: list[dict[str, Any]] = []
        for number, raw in enumerate(requests):  # one at a time, so each sees the last one settled
            answers += _http_inside(opencode, [{"id": str(number), "host": container, "port": GATEWAY_PORT, "raw": raw}])
        return answers, _ledger(ledger_root, f"gw-{name}.jsonl")

    try:
        small = {"usage": {"input_tokens": 300, "output_tokens": 50}}
        answers, _ = probe_gateway("auth", {"max_requests": 50, "max_tokens": 1_000_000}, [{"text": "ok", **small}] * 3, [
            _gateway_request(None), _gateway_request("wrong" * 10), _gateway_request(token, model="gpt-other"), _gateway_request(token, path="/v1/files"),
            _gateway_request(token, path="/v1/chat/completions"), _gateway_request(token, path="/v1/responses", method="GET"), _gateway_request(token, path="/v1/responses?x=1"),
            _gateway_request(token, body={"model": MODEL_ID, "input": "x", "tools": [{"type": "web_search"}]}), _gateway_request(token, body={"model": MODEL_ID, "input": "x", "background": True}),
            _gateway_request(token),
        ])  # fmt: skip
        statuses = [a.get("status") for a in answers]
        r.check("no token is refused", statuses[0] == 401, statuses[0])
        r.check("a wrong token is refused", statuses[1] == 401, statuses[1])
        r.check("another run's model is refused", statuses[2] == 403, statuses[2])
        r.check("unsupported destinations are refused (files, chat, other method, a query)", statuses[3:7] == [403, 403, 403, 403], statuses[3:7])
        r.check("hosted tools and background requests are refused", statuses[7:9] == [403, 403], statuses[7:9])
        r.check("the right token and model are answered, by the fake upstream", statuses[9] == 200, statuses[9])

        answers, ledger = probe_gateway("requests", {"max_requests": 2, "max_tokens": 1_000_000}, [{"text": "ok", **small}] * 5, [_gateway_request(token)] * 4)
        statuses = [a.get("status") for a in answers]
        r.check("the request limit stops the third request", statuses == [200, 200, 429, 429] and len(ledger) == 2, statuses)

        answers, ledger = probe_gateway("tokens", {"max_requests": 50, "max_tokens": 2_500}, [{"text": "ok", **small}] * 9, [_gateway_request(token)] * 9)
        statuses = [a.get("status") for a in answers]
        spent = sum(e["charged_tokens"] for e in ledger)
        r.check("the token limit stops requests before it is passed", 429 in statuses and spent <= 2_500, f"{statuses.count(200)} forwarded, {spent} of 2500 tokens")

        priced = {"price_input_per_mtok": 2.0, "price_output_per_mtok": 10.0, "max_usd": 0.02, "max_requests": 50, "max_tokens": 10_000_000}
        answers, ledger = probe_gateway("usd", priced, [{"text": "ok"}] * 12, [_gateway_request(token)] * 12)
        statuses = [a.get("status") for a in answers]
        spent_usd = sum(e["cost_usd"] for e in ledger)
        r.check("the dollar limit stops requests before it is passed", 429 in statuses and spent_usd <= 0.02, f"{statuses.count(200)} forwarded, ${spent_usd:.4f} of $0.02")
        r.check("cost is recorded from each answer's usage", all(e["cost_usd"] == (1_000 * 2 + 100 * 10) / 1_000_000 for e in ledger), ledger[0]["cost_usd"] if ledger else None)
        r.check("the next request is refused though the spent amount alone would allow it", spent_usd < 0.02, "a request that could overrun was denied")

        answers, ledger = probe_gateway("failures", {**priced, "max_usd": 1.0}, [{"raise_timeout": True}, {"status": 401}, {"status": 429}, {"status": 301}, {"no_usage": True, "text": "x"}],
                                        [_gateway_request(token)] * 5)  # fmt: skip
        statuses = [a.get("status") for a in answers]
        r.check("an upstream timeout answers 504", statuses[0] == 504, statuses[0])
        r.check("a refused provider key answers 502 and says nothing of it", statuses[1] == 502 and "key" not in answers[1].get("body", "").lower(), answers[1])
        r.check("rate limiting reaches the client as 429", statuses[2] == 429, statuses[2])
        r.check("a redirect is not followed", statuses[3] == 502, statuses[3])
        r.check("a timed-out request is charged its whole reservation", ledger[0]["usage_known"] is False and ledger[0]["charged_tokens"] > 1_000 and ledger[0]["cost_usd"] > 0, ledger[0])
        r.check("an answer with no usage is charged its whole reservation", ledger[4]["usage_known"] is False and ledger[4]["cost_usd"] > 0, ledger[4].get("cost_usd"))

        logs = "".join(p.read_text(encoding="utf-8") for p in ledger_root.glob("*.jsonl"))
        r.check("no token, prompt or key is in any gateway log", token not in logs and plan.gateway_token not in logs and "hello" not in logs and "Authorization" not in logs)
        r.check("the ledger holds only numbers and the model", all(set(e) == {"request", "ts", "model", "status", "input_tokens", "output_tokens", "cached_tokens", "charged_tokens", "cost_usd", "usage_known"} for e in ledger))
    finally:
        for container in extra:
            world.cli.remove(container)
        session.close()
    r.check("nothing remains", world.leftovers() == {"containers": [], "networks": []}, world.leftovers())


# --- Scenario 3: the real engine, driven by a script -----------------------------------------------------


def _filter_rates(r: Results, launcher: NoModelLauncher) -> None:
    """Requests the filter must refuse, at rates that show whether a 503 comes from load, and what the audit log costs."""
    plan = launcher.plan
    latency = _exec_python(launcher.filter_id, AUDIT_LATENCY, json.dumps([["the D: bind mount", f"{LEDGER_MOUNT}/latency-probe.tmp"], ["the container's own /tmp", "/tmp/latency-probe.tmp"]]))
    r.note("what one audit line costs, append and flush", latency)
    encoded = base64.b64encode(filter_check.request_bytes("GET", "/index.html", token=plan.filter_token)).decode()
    for label, count, gap, strict in (("20 a second", 100, 0.05, True), ("100 a second", 200, 0.01, True), ("full speed, one client", 300, 0.0, False)):
        before = len(_audit(plan))
        request = {"port": 8080, "gap": gap, "requests": [{"id": str(i), "raw_b64": encoded} for i in range(count)]}
        began = time.monotonic()
        done = _docker("exec", "-i", launcher.filter_id, "python", "-I", f"{EDGE_MOUNT}/filter_probe.py", stdin=json.dumps(request), timeout=60)
        seconds = round(time.monotonic() - began, 1)
        answers = json.loads(done.stdout) if done.returncode == 0 else []
        time.sleep(1.0)  # a decision is written after its answer is sent
        rows = _audit(plan)[before:]
        seen = Counter(a.get("status") or a.get("error") for a in answers)
        r.check(f"3. at {label} every request is refused", len(answers) == count and set(seen) <= {403, 503}, {"answers": dict(seen), "seconds": seconds})
        r.check(f"3. at {label} nothing is forwarded", len(rows) == count and not any(row["forwarded"] for row in rows), f"{len(rows)} decisions, {sum(1 for row in rows if row['forwarded'])} forwarded")
        r.check(f"3. at {label} the audit log agrees with the client", Counter(row["status"] for row in rows) == seen, dict(Counter(row["status"] for row in rows)))
        if strict:
            r.check(f"3. at {label} the filter never answers busy", 503 not in seen, dict(seen))


def add_file(path: str, content: str) -> dict[str, Any]:
    lines = ["*** Begin Patch", f"*** Add File: {path}", *[f"+{line}" for line in content.splitlines()], "*** End Patch"]
    return {"tool": "apply_patch", "arguments": {"patchText": "\n".join(lines)}}


def update_file(path: str, old: str, new: str) -> dict[str, Any]:
    lines = ["*** Begin Patch", f"*** Update File: {path}", "@@", f"-{old}", f"+{new}", "*** End Patch"]
    return {"tool": "apply_patch", "arguments": {"patchText": "\n".join(lines)}}


def move_file(path: str, target: str) -> dict[str, Any]:
    lines = ["*** Begin Patch", f"*** Update File: {path}", f"*** Move to: {target}", "@@", " VALUE = 2", "*** End Patch"]
    return {"tool": "apply_patch", "arguments": {"patchText": "\n".join(lines)}}


def engine(world: World) -> None:
    """OpenCode's own rules, on its own tools. For a gpt model it offers apply_patch, never write or edit."""
    r = world.results
    config, launcher, session, _request, (real, project_id, _run, copy) = _start(
        world, "engine", token_budget=ENGINE_TOKENS, opencode_max_tokens_per_task=ENGINE_TOKENS, gateway_max_requests_per_task=ENGINE_REQUESTS,
    )  # fmt: skip
    client: OpenCodeClient = session._client
    plan = launcher.plan
    opencode = session.container_id
    inside = "/workspace/made_by_opencode.py"
    try:
        created = client.create_session("no-model check")
        session_id = created["id"]
        r.check("a session opens through the filter with the exact rules", created["permission"] == session_rules())

        def run(step: dict[str, Any]) -> list[dict[str, Any]]:
            began = time.monotonic()
            before = len(_tool_parts(client, session_id))
            status = _prompt(session_id, client, [step])
            settled = _wait_idle(client, session_id, seconds=STEP_SECONDS)
            parts = _tool_parts(client, session_id)[before:]
            print(f"    ({time.monotonic() - began:.1f} s) {step['tool']}: {[(p['tool'], p['status']) for p in parts]}", flush=True)
            if status != 204 or not settled:
                raise StepFailed(f"the scripted {step['tool']} step did not finish: status {status}, settled {settled}, parts {parts}")
            return parts

        def worked(parts: list[dict[str, Any]]) -> bool:
            return bool(parts) and parts[0]["status"] == "completed"

        made = run(add_file(inside, "VALUE = 1\n"))
        on_disk = (copy / "made_by_opencode.py").read_text(encoding="utf-8") if (copy / "made_by_opencode.py").exists() else ""
        r.check("8. OpenCode creates a file in the disposable copy with apply_patch", worked(made) and "VALUE = 1" in on_disk, made)
        r.check("8. OpenCode reads inside the disposable copy", worked(run({"tool": "read", "arguments": {"filePath": inside}})))
        r.check("10. the grep tool works with the pinned rg", worked(run({"tool": "grep", "arguments": {"pattern": "VALUE"}})))
        r.check("10. the glob tool works with the pinned rg", worked(run({"tool": "glob", "arguments": {"pattern": "*.py"}})))
        changed = run(update_file(inside, "VALUE = 1", "VALUE = 2"))
        r.check("8. OpenCode changes a file in the disposable copy", worked(changed) and "VALUE = 2" in (copy / "made_by_opencode.py").read_text(encoding="utf-8"), changed)

        for label, target, probe in (("/tmp", "/tmp/outside.txt", "/tmp/outside.txt"), ("/etc", "/etc/outside.txt", "/etc/outside.txt"), ("a path escaping with ..", "../outside.txt", "/outside.txt"),
                                     ("the absolute parent of the workspace", "/outside.txt", "/outside.txt")):  # fmt: skip
            parts = run(add_file(target, "x\n"))
            exists = _exec_python(opencode, f"import json, os\nprint(json.dumps(os.path.exists({probe!r})))")
            r.check(f"8. a patch to {label} is refused", refused(parts) and exists is False, parts[:1])
        parts = run({"tool": "read", "arguments": {"filePath": "/etc/passwd"}})
        r.check("8. reading outside the workspace is refused", refused(parts), parts[:1])
        for name in ("opencode.json", "AGENTS.md", ".git/config", ".env", ".opencode/plugin/x.js"):
            parts = run(add_file(f"/workspace/{name}", "x\n"))
            r.check(f"8. a patch creating {name} is refused", refused(parts) and not (copy / name).exists(), parts[:1])

        (copy / ".env").write_text("KEY=not-real\n", encoding="utf-8")
        parts = run({"tool": "read", "arguments": {"filePath": "/workspace/.env"}})
        r.check("8. reading .env is refused", refused(parts), parts[:1])
        (copy / ".env").unlink()

        for label, tool, arguments in (
            ("bash", "bash", {"command": "touch /workspace/bash_ran", "description": "x"}),
            ("write, which this model is not offered", "write", {"filePath": "/workspace/write_ran.txt", "content": "x"}),
            ("webfetch", "webfetch", {"url": "https://example.com", "format": "text"}),
            ("task", "task", {"description": "x", "prompt": "x", "subagent_type": "general"}),
            ("an unknown tool", "devpilot_unknown_tool", {"x": 1}),
        ):
            parts = run({"tool": tool, "arguments": arguments})
            r.check(f"7. {label} is attempted and does not run", refused(parts), parts[:1])
        r.check("7. those calls had no effect", not (copy / "bash_ran").exists() and not (copy / "write_ran.txt").exists())

        from app.coding.audit import audit, take_snapshot

        base = take_snapshot(copy)
        moved = run(move_file(inside, "/workspace/opencode.json"))
        after = take_snapshot(copy)
        report = audit(copy, base, after, max_file_bytes=config.workspace_max_file_bytes, max_files=config.workspace_max_files, secrets=[])
        escaped = (copy / "opencode.json").exists()
        r.note("a patch moving a file onto opencode.json", {"engine": moved[:1], "file_appeared": escaped})
        r.check("8. the audit refuses a move onto a protected file, whatever the engine did", (not escaped) or any("opencode.json" in v for v in report.violations), report.violations[:2])
        if escaped:
            (copy / "opencode.json").unlink()

        _filter_rates(r, launcher)
        server_log = _docker("logs", opencode).stdout + _docker("logs", opencode).stderr
        (world.root / "opencode-engine.log").write_text(server_log, encoding="utf-8")
        r.check("10. OpenCode downloaded nothing", "downloading ripgrep" not in server_log, "no 'downloading ripgrep' in its log")
        bin_folder = _exec_python(opencode, "import json, os\np = '/tmp/home/.local/share/opencode/bin'\nprint(json.dumps(os.listdir(p) if os.path.isdir(p) else []))")
        r.check("10. no rg was placed in OpenCode's own folder", "rg" not in bin_folder, bin_folder)
        ledger = _ledger(plan.folder / "ledger")
        r.check("13. model requests went only to the scripted upstream", bool(ledger) and all(e["model"] == MODEL_ID for e in ledger), f"{len(ledger)} scripted answers")
        r.note("what OpenCode's tool calls did", [(p["tool"], p["status"], p["error"][:90]) for p in _tool_parts(client, session_id)])
        r.check("9. the original project never received these files", not (real.project_dir(project_id) / "made_by_opencode.py").exists())
    finally:
        session.close()
    r.check("nothing remains", world.leftovers() == {"containers": [], "networks": []}, world.leftovers())


# --- The whole stack: FakeLLM, the real tools, real OpenCode, a scripted model, real Docker tests -----------


def _opening() -> list[Step]:
    return mvp._calculator_opening()


def _settled_attempts(app: Any, wait: float = 60.0) -> None:
    """A cancelled run ends at once, and its attempt finishes stopping behind it. Read the attempt when that is done."""
    deadline = time.monotonic() + wait
    while time.monotonic() < deadline:
        with app.state.session_factory() as s:
            if not [a for a in s.query(CodingAttempt) if a.phase in UNRESOLVED_PHASES]:
                return
        time.sleep(0.25)


def _run_stack(world: World, name: str, gpt: list[Step], *, launcher_args: dict[str, Any] | None = None, seed: dict[str, str] | None = None, wait: float = 240.0, cancel_on: str | None = None, **settings: Any) -> dict[str, Any]:
    with warnings.catch_warnings():
        warnings.filterwarnings("ignore", message=".*httpx.*")
        from fastapi.testclient import TestClient

    config = world.settings(name, **settings)
    launcher = NoModelLauncher(config, world.cli, **(launcher_args or {}))
    selector = ProviderSelector([ProviderOption("opencode_server, scripted model", launcher, frozenset({"code_generation", "test_generation", "documentation"}), simulated=True)])
    app = create_app(config, llm=FakeLLM(*gpt), sandbox=DockerSandbox.from_settings(config), coding=selector)
    headers = {"Authorization": f"Bearer {config.devpilot_api_token.get_secret_value()}"}
    with TestClient(app) as client:
        project = client.post(f"{API}/projects", json={"name": name, "requirement_text": mvp.CALCULATOR_REQUIREMENT}, headers=headers).json()
        for rel, content in (seed or {"README.md": "# The user's project\n"}).items():
            app.state.workspace_fs.write_text(project["id"], rel, content)
        original = app.state.workspace_fs.project_dir(project["id"])
        before = mvp._hashes(original)
        run = client.post(f"{API}/projects/{project['id']}/runs", headers=headers).json()
        client.post(f"{API}/runs/{run['id']}/start", headers=headers).raise_for_status()
        deadline = time.monotonic() + wait
        while time.monotonic() < deadline:
            run = client.get(f"{API}/runs/{run['id']}", headers=headers).json()
            if run["ended_at"] or run["pause_reason"]:
                break
            if cancel_on is not None and (config.coding_work_dir / run["id"] / cancel_on).exists():
                client.post(f"{API}/runs/{run['id']}/cancel", headers=headers).raise_for_status()
                cancel_on = None
            time.sleep(0.25)
        _settled_attempts(app)
        invocations = client.get(f"{API}/runs/{run['id']}/invocations", headers=headers).json()
        with app.state.session_factory() as s:
            attempts = [(str(a.phase), a.container_id, a.message_id, a.failure_reason, a.stop_evidence, a.process_terminated, a.restore_verified) for a in s.query(CodingAttempt).order_by(CodingAttempt.id)]
        report = client.get(f"{API}/runs/{run['id']}/report", headers=headers)
        coding_events = client.get(f"{API}/runs/{run['id']}/events?limit=500", headers=headers).json()
    return {"run": run, "config": config, "launcher": launcher, "before": before, "after": mvp._hashes(original), "original": original, "invocations": invocations, "attempts": attempts,
            "report": report.json() if report.status_code == 200 else {}, "events": coding_events}  # fmt: skip


def _generate(task: int, steps: list[dict[str, Any]]) -> Step:
    return call("generate_code", {"task_order": task, "instructions": f"Follow the script. {steps_marker(steps)}"})


def stop_path(world: World) -> None:
    """A1 and A2: one prompt, a stop confirmed from Docker, and the disposable copy restored."""
    r = world.results
    steps = [add_file("/workspace/half_done.py", "half\n"), {"stall": 90}]
    result = _run_stack(world, "stop", [*_opening(), _generate(1, steps), fail(LLMErrorKind.RATE_LIMIT, "the script stops here")], opencode_timeout_seconds=12)
    attempts, run = result["attempts"], result["run"]
    r.check("the attempt timed out and was restored", len(attempts) == 1 and attempts[0][0] == "RESTORED", attempts[:1])
    phase, container, message, reason, evidence, terminated, restored = attempts[0]
    r.check("A2: the stop was confirmed by an independent read", terminated is True and "after: NOT_FOUND" in str(evidence), evidence)
    r.check("A2: the restore was verified against the checkpoint", restored is True)
    phases = _phases(result)
    r.check("A2: nothing was restored until the writer was confirmed gone", _in_order(phases, "ABORT_REQUESTED", "QUIESCENCE_CHECK", "SAFE_TO_RESTORE", "RESTORED"), phases)
    gone = world.cli.inspect_container(container, expected_id=container)
    r.check("the container is gone, by its full ID", gone.presence is ContainerPresence.NOT_FOUND, gone.detail)
    copy = result["config"].coding_work_dir / run["id"]
    r.check("the half-written file was undone in the copy", not (copy / "half_done.py").exists(), sorted(p.name for p in copy.iterdir()) if copy.exists() else "copy gone")
    audit = _evidence(result, "filter-audit.jsonl")
    prompts = [e for e in audit if str(e.get("target", "")).endswith("/prompt_async")]
    r.check("A1: exactly one prompt was sent", len(prompts) == 1 and prompts[0].get("status") == 204, [(e.get("target"), e.get("status")) for e in prompts])
    r.check("A1: the server took DevPilot's own message ID", bool(message), message)
    r.check("the original project is unchanged", result["before"] == result["after"])
    r.check("nothing remains", world.leftovers() == {"containers": [], "networks": []}, world.leftovers())
    r.note("cancellation here stopped a scripted stall, not a real model", "model-driven cancellation is not verified")


def _phases(result: dict[str, Any]) -> list[str]:
    return [str(e["payload"]["to"]) for e in result["events"] if e["type"] == "coding.attempt_phase"]


def _in_order(seen: list[str], *wanted: str) -> bool:
    """True when `wanted` happen in this order, with nothing between the last two that could undo the proof."""
    rest = iter(seen)
    return all(step in rest for step in wanted)


def cancel_path(world: World) -> None:
    """A person cancels through the API while the writer is working. The same stop as a timeout."""
    r = world.results
    steps = [add_file("/workspace/half_done.py", "half\n"), {"stall": 90}]
    result = _run_stack(world, "cancel", [*_opening(), _generate(1, steps), fail(LLMErrorKind.RATE_LIMIT, "the script stops here")], cancel_on="half_done.py", opencode_timeout_seconds=60)
    attempts, run = result["attempts"], result["run"]
    r.check("the run is cancelled", run["state"] == "CANCELLED", (run["state"], run["outcome"]))
    r.check("the attempt was stopped and restored", len(attempts) == 1 and attempts[0][0] == "RESTORED", attempts[:1])
    phase, container, _message, _reason, evidence, terminated, restored = attempts[0]
    r.check("the stop was confirmed by an independent read", terminated is True and "after: NOT_FOUND" in str(evidence), evidence)
    r.check("the restore was verified against the checkpoint", restored is True)
    r.check("nothing was restored until the writer was confirmed gone", _in_order(_phases(result), "ABORT_REQUESTED", "QUIESCENCE_CHECK", "SAFE_TO_RESTORE", "RESTORED"), _phases(result))
    r.check("the container is gone, by its full ID", world.cli.inspect_container(container, expected_id=container).presence is ContainerPresence.NOT_FOUND)
    r.check("the original project is unchanged", result["before"] == result["after"])
    r.check("nothing remains", world.leftovers() == {"containers": [], "networks": []}, world.leftovers())


def _evidence(result: dict[str, Any], name: str) -> list[dict[str, Any]]:
    folder = result["config"].coding_work_dir / "evidence"
    rows: list[dict[str, Any]] = []
    for path in sorted(folder.glob(f"*/{name}")) if folder.is_dir() else []:
        rows += _ledger(path.parent, name) if name == "ledger.jsonl" else [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]
    return rows


def quarantine(world: World) -> None:
    """A stop that cannot be confirmed: real containers, and a Docker CLI that cannot be reached."""
    r = world.results
    steps = [add_file("/workspace/half_done.py", "half\n"), {"stall": 90}]
    result = _run_stack(world, "quarantine", [*_opening(), _generate(1, steps)], launcher_args={"broken_stop": True}, opencode_timeout_seconds=12)
    attempts, run = result["attempts"], result["run"]
    r.check("an unconfirmed stop quarantines the attempt", len(attempts) == 1 and attempts[0][0] == "QUARANTINED", attempts[:1])
    r.check("the run ends and says why", run["state"] == "FAILED" and run["outcome"] == "quarantined", run["outcome"])
    r.check("nothing was restored", attempts[0][6] is False and "SAFE_TO_RESTORE" not in _phases(result) and "RESTORED" not in _phases(result), _phases(result))
    container = attempts[0][1]
    state = world.cli.inspect_container(container, expected_id=container)
    r.check("the writer really is still running, so restoring would have been wrong", state.presence is ContainerPresence.RUNNING, state.presence)
    copy = result["config"].coding_work_dir / run["id"]
    r.check("the disposable copy is kept for review", (copy / "half_done.py").is_file())
    r.check("the original project is unchanged", result["before"] == result["after"])
    r.check("the evidence of the failed stop is kept", bool(attempts[0][4]), attempts[0][4])
    stopped = ContainerTerminator(world.cli, container).terminate()
    r.check("the real Docker then stops it, confirmed", stopped.confirmed is True, stopped.evidence[:120])
    result["launcher"].sweep()
    r.check("nothing remains after the cleanup", world.leftovers() == {"containers": [], "networks": []}, world.leftovers())


def recovery(world: World) -> None:
    """The backend dies with a real writer running. A new one must stop it and undo the attempt."""
    r = world.results
    config = world.settings("recovery")
    engine_ = create_db_engine(config)
    init_db(engine_)
    sessions = create_session_factory(engine_)
    store = SqlAttemptStore(sessions, Redactor(config.secret_values()))
    real, project_id, run_id, copy = world.project(config)
    with sessions.begin() as s:
        project = repositories.create_project(s, project_id=project_id, name="recovery", requirement_text="r", workspace_path=str(real.project_dir(project_id)))
        run = repositories.create_run(s, project)
        run.id = run_id
    launcher = NoModelLauncher(config, world.cli)
    call_id = "call_recovery"
    from app.coding.audit import Checkpoint, take_snapshot

    Checkpoint.create(config.coding_checkpoints_dir, run_id, call_id, copy, take_snapshot(copy))
    session = launcher.start(LaunchRequest(run_id=run_id, call_id=call_id, workspace=copy, token_budget=50_000))
    container = session.container_id
    client: OpenCodeClient = session._client
    guard = AttemptGuard.open(store=store, project_id=project_id, run_id=run_id, call_id=call_id, task_key=f"{run_id}:1", container_id=container,
                              writer=session.writer, fingerprint=lambda: workspace_fingerprint(copy))  # fmt: skip
    created = client.create_session("recovery")
    steps = [add_file("/workspace/left_by_the_crash.py", "x\n"), {"stall": 120}]
    body = {"agent": PROMPTED_AGENT, "model": {"providerID": "openai", "modelID": MODEL_ID}, "parts": [{"type": "text", "text": f"Scripted check. {steps_marker(steps)}"}]}
    verdict = guard.submit(session.control, created["id"], body, use_message_id=True)
    r.check("a prompt is running when the backend dies", str(verdict.value) == "ACCEPTED", verdict)
    deadline = time.monotonic() + 30
    while time.monotonic() < deadline and not (copy / "left_by_the_crash.py").exists():
        time.sleep(0.25)
    r.check("the writer changed the copy before the crash", (copy / "left_by_the_crash.py").exists())
    r.check("the attempt is left unresolved", guard.phase is AttemptPhase.RUNNING, guard.phase)
    if launcher.forwarder is not None:
        launcher.forwarder.close()  # the process that held the connection is gone

    fresh = NoModelLauncher(config, world.cli)
    selector = ProviderSelector([ProviderOption("opencode_server, scripted model", fresh, frozenset({"code_generation"}), simulated=True)])
    runtime = CodingRuntime(selector=selector, store=SqlAttemptStore(sessions, Redactor(config.secret_values())), candidates=CandidateWorkspaces(WorkspaceFS(config.coding_work_dir, max_file_bytes=2_000_000, max_files=100)), sandbox=DockerSandbox.from_settings(config))
    finished = recover_attempts(runtime, config, sessions)
    r.check("startup recovery finished the attempt", len(finished) == 1 and finished[0].phase is AttemptPhase.RESTORED, [str(f.phase) for f in finished])
    r.check("termination was confirmed", finished[0].process_terminated is True, (finished[0].stop_evidence or "")[:120])
    gone = world.cli.inspect_container(container, expected_id=container)
    r.check("the writer is gone, by its full ID", gone.presence is ContainerPresence.NOT_FOUND, gone.detail)
    r.check("its change was undone", not (copy / "left_by_the_crash.py").exists())
    r.check("the original project is intact", real.read_text(project_id, "README.md") == "# The user's project\n" and (real.project_dir(project_id) / SENTINEL).is_file())
    r.check("nothing remains", world.leftovers() == {"containers": [], "networks": []}, world.leftovers())
    engine_.dispose()


def full_path(world: World) -> None:
    """Everything real except GPT's replies and the model's: real OpenCode writes the files through its tools."""
    r = world.results
    write_code = [add_file("/workspace/calculator.py", mvp.CALCULATOR)]
    write_tests = [add_file("/workspace/test_calculator.py", mvp.CALCULATOR_TESTS)]
    gpt = [*_opening(), _generate(1, write_code), _generate(2, write_tests), call("submit_implementation", {"summary": "Done."}), call("finish", {"summary": "Delivered."})]
    result = _run_stack(world, "full", gpt, openai_price_input_per_mtok=2.0, openai_price_output_per_mtok=10.0, run_budget_usd=1.5)
    run, report = result["run"], result["report"]
    r.check("the run completes through real OpenCode", run["state"] == "COMPLETED", (run["state"], run["outcome"], run["status_detail"]))
    r.check("both attempts were kept", [a[0] for a in result["attempts"]] == ["KEPT", "KEPT"], [a[0] for a in result["attempts"]])
    checks = report.get("verifications", [])
    r.check("Docker ran the files OpenCode wrote", bool(checks) and checks[0]["verdict"] == "PASSED" and checks[0]["passed"] == 5 and checks[0]["exit_code"] == 0, checks[:1])
    r.check("they were delivered, and the file already there is intact", {f["path"] for f in report.get("delivered_files", [])} == {"calculator.py", "test_calculator.py"} and (result["original"] / "README.md").is_file(), report.get("delivered_files"))
    r.check("the report says what was simulated", report.get("provenance", {}).get("fully_real") is False, report.get("provenance", {}).get("simulated"))
    ledger = _evidence(result, "ledger.jsonl")
    r.check("every model request is in the gateway's ledger", bool(ledger) and all(e["model"] == MODEL_ID for e in ledger), f"{len(ledger)} scripted answers")
    spent = sum(e["cost_usd"] or 0 for e in ledger)
    gpt_cost = sum(e["payload"].get("cost_usd") or 0 for e in result["events"] if e["type"] == "llm.response")
    r.check("the run's cost is GPT's plus the gateway's", abs(run["cost_usd"] - (spent + gpt_cost)) < 1e-6, f"run ${run['cost_usd']:.4f} = gateway ${spent:.4f} + scripted GPT ${gpt_cost:.4f}")
    r.check("nothing remains", world.leftovers() == {"containers": [], "networks": []}, world.leftovers())
    r.note("provider", report.get("selection", [{}])[0].get("provider") if report.get("selection") else None)
    sizes = _evidence(result, "request-sizes.jsonl")
    r.note("the real size of OpenCode's requests", {"requests": len(sizes), "bytes": [s["bytes"] for s in sizes], "tools": sorted({s["tools"] for s in sizes}), "worst_reservation_usd": round(max((s["bytes"] + 512) * 2 / 1e6 + result["config"].gateway_max_output_tokens * 10 / 1e6 for s in sizes), 4) if sizes else None})
    r.note("GPT turns and tool calls this run took", {"turns": run["turns"], "tool_calls": run["tool_calls"], "cost_usd": run["cost_usd"]})


# --- Interruption: a real stack, and a process that dies in each way it can ------------------------------


def hold(world: World) -> None:
    """Not a check. It starts a real stack with a request in flight, says so, and waits to be interrupted."""
    _config, launcher, session, _request, (_real, _project, _run, copy) = _start(world, "hold")
    client: OpenCodeClient = session._client
    try:
        created = client.create_session("hold")
        _prompt(created["id"], client, [add_file("/workspace/half_done.py", "half\n"), {"stall": 600}])
        deadline = time.monotonic() + 30
        while time.monotonic() < deadline and not (copy / "half_done.py").exists():
            time.sleep(0.25)
        time.sleep(1.0)  # the stalled request is now in flight
        assert world.marker is not None
        world.marker.write_text(json.dumps({"pid": os.getpid(), "writer": session.container_id, "ledger": str(launcher.plan.folder / "ledger"), "copy": str(copy), "root": str(world.root)}), encoding="utf-8")
        time.sleep(3600)
    finally:
        session.close()


SEND_CTRL_C = """
import ctypes, sys, time
kernel = ctypes.WinDLL("kernel32", use_last_error=True)
kernel.FreeConsole()
attached = kernel.AttachConsole(int(sys.argv[1]))
kernel.SetConsoleCtrlHandler(None, True)
sent = kernel.GenerateConsoleCtrlEvent(0, 0) if attached else 0
time.sleep(0.5)
kernel.FreeConsole()
sys.exit(0 if sent else 3)
"""


def _held_stack(world: World, label: str, *, deadline: float = 300.0, guard_deadline: float | None = None, guard_docker: str = "docker", watchdog: bool = True, under_powershell: bool = False) -> dict[str, Any]:
    folder = world.root / "interrupt" / label
    folder.mkdir(parents=True)
    marker, state = folder / "held.json", folder / "guard"
    command = [sys.executable, "-u", str(Path(__file__).resolve()), "--run", "--only", "hold", "--keep", "--deadline", str(deadline), "--marker", str(marker), "--guard-state", str(state), "--guard-docker", guard_docker]
    command += ["--guard-deadline", str(guard_deadline)] if guard_deadline else []
    command += [] if watchdog else ["--no-watchdog"]
    hidden = subprocess.STARTUPINFO()
    hidden.dwFlags |= subprocess.STARTF_USESHOWWINDOW
    log = folder / "child.log"
    if under_powershell:  # exactly as the check was hosted when Ctrl-C skipped its cleanup
        line = "& " + " ".join(f"'{part}'" for part in command) + f" *> '{log}'"
        host = subprocess.Popen(["powershell", "-NoProfile", "-NonInteractive", "-Command", line], creationflags=subprocess.CREATE_NEW_CONSOLE, startupinfo=hidden)
    else:
        with log.open("w", encoding="utf-8") as output:
            host = subprocess.Popen(command, stdout=output, stderr=subprocess.STDOUT, creationflags=subprocess.CREATE_NEW_CONSOLE, startupinfo=hidden)
    limit = time.monotonic() + 90
    while time.monotonic() < limit and not marker.is_file() and host.poll() is None:
        time.sleep(0.25)
    if not marker.is_file():
        raise StepFailed(f"{label}: the held stack never came up: {log.read_text(encoding='utf-8', errors='replace')[-400:] if log.is_file() else 'no log'}")
    return {"host": host, "state": state, **json.loads(marker.read_text(encoding="utf-8"))}


def _guard_report(state: Path, seconds: float = 45.0) -> dict[str, Any]:
    limit = time.monotonic() + seconds
    while time.monotonic() < limit:
        try:
            return dict(json.loads((state / REPORT_FILE).read_text(encoding="utf-8")))
        except (OSError, ValueError):
            time.sleep(0.25)
    return {}


def _judge_stop(world: World, label: str, held: dict[str, Any], *, status: str = "clean") -> None:
    r = world.results
    report = _guard_report(held["state"])
    r.check(f"{label}: the guard acted, independently", report.get("status") == status, {k: report.get(k) for k in ("trigger", "status", "reason")})
    gone = world.cli.inspect_container(held["writer"], expected_id=held["writer"])
    r.check(f"{label}: the writer is gone, by a separate read of its full ID", gone.presence is ContainerPresence.NOT_FOUND, gone.detail)
    r.check(f"{label}: no container or network remains", world.leftovers() == {"containers": [], "networks": []}, world.leftovers())
    writers = report.get("writers") or []
    r.check(f"{label}: the guard's own evidence shows the stop was read back", (not writers) or all("after: NOT_FOUND" in w["evidence"] for w in writers), [w["evidence"][-90:] for w in writers])


def interrupt(world: World) -> None:
    """Each way a run can end badly, with no model: the guard must leave a confirmed stop, or say it has none."""
    r = world.results
    helper = world.root / "send_ctrl_c.py"
    helper.write_text(SEND_CTRL_C, encoding="utf-8")

    held = _held_stack(world, "powershell", under_powershell=True)
    delivered = subprocess.run([sys.executable, "-I", str(helper), str(held["pid"])], timeout=20).returncode
    r.check("Ctrl-C under PowerShell, the failure of 31 October: the signal was delivered", delivered == 0, delivered)
    _judge_stop(world, "Ctrl-C under PowerShell", held)
    held["host"].wait(timeout=30)

    held = _held_stack(world, "killed")
    os.kill(held["pid"], signal.SIGTERM)
    _judge_stop(world, "killed outright", held)
    usage = read_ledger(Path(held["ledger"]) / "ledger.jsonl")
    r.check("killed outright: the attempt's ledger survives for recovery", Path(held["ledger"], "ledger.jsonl").is_file(), usage)
    r.check("killed outright: the request in flight when the gateway died is charged in full", usage["all_known"] is False and (usage["cost_usd"] or 0) > 0.05, usage)
    r.check("killed outright: the half-written work stays for recovery to undo", Path(held["copy"], "half_done.py").is_file())

    held = _held_stack(world, "deadline", deadline=40.0)
    code = held["host"].wait(timeout=60)
    r.check("its own deadline: the check stopped itself and said so", code == 3, code)
    _judge_stop(world, "its own deadline", held)

    held = _held_stack(world, "wedged", deadline=9999.0, guard_deadline=40.0, watchdog=False)
    code = held["host"].wait(timeout=60)
    r.check("a wedged check: the guard's deadline ended a process that could not end itself", code != 0, code)
    r.check("a wedged check: the guard says that is what happened", "deadline passed" in str(_guard_report(held["state"]).get("trigger")), _guard_report(held["state"]).get("trigger"))
    _judge_stop(world, "a wedged check", held)

    held = _held_stack(world, "unconfirmed", guard_docker="docker-is-not-installed-here")
    os.kill(held["pid"], signal.SIGTERM)
    report = _guard_report(held["state"])
    r.check("a stop it cannot confirm: not called clean", report.get("status") == "quarantined", {k: report.get(k) for k in ("status", "reason")})
    r.check("a stop it cannot confirm: a marker says so", (held["state"] / QUARANTINE_FILE).is_file())
    state = world.cli.inspect_container(held["writer"], expected_id=held["writer"])
    r.check("a stop it cannot confirm: the writer really is still running", state.presence is ContainerPresence.RUNNING, state.presence)
    r.check("a stop it cannot confirm: the work and the ledger are kept", Path(held["copy"], "half_done.py").is_file() and Path(held["ledger"], "ledger.jsonl").is_file())
    once = subprocess.run([sys.executable, "-B", "-m", "app.coding.guard_process", "--once", "--state", str(held["state"]), f"--network={MODEL_NETWORK}", f"--network={CORE_NETWORK}"], cwd=HERE.parent, timeout=60, capture_output=True)
    r.check("a stop it cannot confirm: a person's later stop with a working Docker succeeds", once.returncode == 0)
    _judge_stop(world, "after that stop", held)
    r.check("a stop it cannot confirm: the marker is resolved and kept", (held["state"] / RESOLVED_FILE).is_file() and not (held["state"] / QUARANTINE_FILE).exists())


def production_gateway(world: World) -> None:
    """The real gateway_server.py, not the scripted one, with a placeholder key and no route anywhere. Only the
    address it would connect to is missing, so a request that passes every check must fail closed and be charged."""
    r = world.results
    config = world.settings("production", openai_max_output_tokens=4_000, gateway_max_requests_per_task=3)
    launcher = NoModelLauncher(config, world.cli)
    project = world.project(config)
    request = LaunchRequest(run_id=project[2], call_id="call_production", workspace=project[3], token_budget=50_000, usd_budget=None)
    plan = launcher._new_plan(request)
    launcher._write_files(request, plan)
    args = OpenCodeLauncher._gateway_args(launcher, plan)
    r.check("it is the production entry point", any(a.endswith("/edge/gateway_server.py") for a in args), [a for a in args if a.endswith(".py")])
    at = args.index("--network")
    networks = [args[i + 1] for i, a in enumerate(args) if a == "--network"]
    r.check("its live networks are the model network and the route out", sorted(networks) == sorted([MODEL_NETWORK, EGRESS_NETWORK]), networks)
    args = [a for i, a in enumerate(args) if not (a == "--network" or (i and args[i - 1] == "--network"))]
    args[at:at] = ["--network", MODEL_NETWORK]  # the route out is left off: this check has none
    world.cli.create_network(MODEL_NETWORK, internal=True)
    cid = world.cli.run_detached(args, env={GATEWAY_TOKEN_ENV: plan.gateway_token, UPSTREAM_KEY_ENV: "placeholder-this-is-not-a-key"})
    try:
        deadline = time.monotonic() + 30
        answers: list[dict[str, Any]] = []
        while time.monotonic() < deadline:
            answers = _http_inside(cid, [{"id": "no token", "host": "127.0.0.1", "port": GATEWAY_PORT, "raw": _gateway_request(None)}])
            if answers and answers[0].get("status") == 401:
                break
            time.sleep(0.5)
        r.check("it starts, binds and refuses a request with no token", answers[0].get("status") == 401, answers[0].get("status"))
        stats = _exec_python(cid, "import json, ssl\nprint(json.dumps(ssl.create_default_context().cert_store_stats()))")
        r.check("its image trusts certificate authorities, so TLS verification can work", isinstance(stats, dict) and stats.get("x509_ca", 0) > 0, stats)
        model = (config.opencode_model or "").partition("/")[2]
        done = _http_inside(cid, [{"id": "wrong token", "host": "127.0.0.1", "port": GATEWAY_PORT, "raw": _gateway_request("w" * 43, model=model)},
                                  {"id": "the right token", "host": "127.0.0.1", "port": GATEWAY_PORT, "raw": _gateway_request(plan.gateway_token, model=model)}])  # fmt: skip
        r.check("a wrong token is refused", done[0].get("status") == 401, done[0].get("status"))
        r.check("with no route to OpenAI, a valid request fails closed", done[1].get("status") == 502, done[1])
        ledger = read_ledger(plan.folder / "ledger" / "ledger.jsonl")
        r.check("that request is on the ledger as unknown, charged in full", ledger["requests"] == 1 and ledger["all_known"] is False and ledger["charged_tokens"] > 0, ledger)
        logs = _docker("logs", cid)
        r.check("the key is in no log", "placeholder-this-is-not-a-key" not in logs.stdout + logs.stderr + (plan.folder / "ledger" / "gateway-audit.jsonl").read_text(encoding="utf-8"))
    finally:
        world.cli.remove(cid)
        world.cli.remove_network(MODEL_NETWORK)
        launcher.sweep()  # its attempt folder holds this gateway's token
    r.check("nothing remains", world.leftovers() == {"containers": [], "networks": []}, world.leftovers())


def rehearsal(world: World) -> None:
    """The paid run's own code and its exact limits, with a scripted GPT and model. Nothing here is billed."""
    r = world.results
    limits = {k: v for k, v in live_demo.LIMITS.items() if k not in ("openai_model", "opencode_enabled")}
    config = world.settings("rehearsal", **limits)
    out = world.root / "rehearsal" / "out"
    out.mkdir(parents=True)
    state = out / "guard"
    start_guard(state, deadline=600.0, networks=(MODEL_NETWORK, CORE_NETWORK))
    r.check("the guard is watching before anything starts", (state / READY_FILE).is_file())
    launcher = NoModelLauncher(config, world.cli)
    config.coding_work_dir.mkdir(parents=True, exist_ok=True)
    problem = live_demo.dry_launch(launcher, config)
    r.check("the dry launch starts the real containers and stops them", problem is None, problem)
    r.check("the dry launch leaves nothing", world.leftovers() == {"containers": [], "networks": []} and not list((config.coding_work_dir / "attempts").iterdir()), world.leftovers())
    selector = ProviderSelector([ProviderOption("opencode_server, scripted model", launcher, frozenset({"code_generation", "test_generation", "documentation"}), simulated=True)])
    gpt = [*_opening(), _generate(1, [add_file("/workspace/calculator.py", mvp.CALCULATOR)]), _generate(2, [add_file("/workspace/test_calculator.py", mvp.CALCULATOR_TESTS)]), call("submit_implementation", {"summary": "Done."}), call("finish", {"summary": "Delivered."})]
    summary = live_demo.run_demo(config, llm=FakeLLM(*gpt), coding=selector, sandbox=DockerSandbox.from_settings(config), out=out, request=live_demo.REQUEST, wait=120.0, cli=world.cli)
    r.check("the run completes under the exact live limits", summary["state"] == "COMPLETED", (summary["state"], summary["outcome"], summary["detail"]))
    r.check("it is delivered and verified", {"calculator.py", "test_calculator.py"} <= set(summary["delivered"]) and summary["report"].get("verifications", [{}])[0].get("verdict") == "PASSED", summary["delivered"])
    r.check("it stays inside the approved spend", 0 < summary["cost_usd"] <= live_demo.SPEND_LIMIT_USD, summary["cost_usd"])
    r.check("each attempt used fewer requests than its limit", bool(summary["gateway"]) and all(g["requests"] < limits["gateway_max_requests_per_task"] for g in summary["gateway"]), [g["requests"] for g in summary["gateway"]])
    r.check("the summary is written", (out / "summary.json").is_file())
    r.check("the report says it was simulated", summary["report"].get("provenance", {}).get("fully_real") is False)
    r.check("nothing remains", summary.get("left_behind") == {"containers": [], "networks": []}, summary.get("left_behind"))


SCENARIOS: dict[str, Callable[[World], None]] = {"boundaries": boundaries, "gateway": gateway, "engine": engine, "stop": stop_path, "cancel": cancel_path, "quarantine": quarantine, "recovery": recovery, "full": full_path,
                                                 "interrupt": interrupt, "production_gateway": production_gateway, "rehearsal": rehearsal, "hold": hold}  # fmt: skip
INTERNAL_SCENARIOS = frozenset({"hold"})


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--run", action="store_true", help="start real containers")
    parser.add_argument("--only", choices=sorted(SCENARIOS), action="append")
    parser.add_argument("--keep", action="store_true", help="keep the check's folder")
    parser.add_argument("--deadline", type=float, default=300.0, help="seconds for each scenario, after which the check stops everything and exits")
    parser.add_argument("--marker", type=Path, help=argparse.SUPPRESS)  # for `hold`
    parser.add_argument("--guard-state", type=Path, help=argparse.SUPPRESS)
    parser.add_argument("--guard-docker", default="docker", help=argparse.SUPPRESS)  # a CLI that cannot work, for the unconfirmed case
    parser.add_argument("--guard-deadline", type=float, help=argparse.SUPPRESS)
    parser.add_argument("--no-watchdog", action="store_true", help=argparse.SUPPRESS)  # a check that cannot end itself
    args = parser.parse_args()
    if not args.run:
        print("Dry run. With --run this starts, on two internal Docker networks only:", ", ".join(SCENARIOS))
        return 0
    results = Results()
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    root = CHECK_ROOT / stamp
    root.mkdir(parents=True)
    world = World(results, root, marker=args.marker)
    if world.leftovers() != {"containers": [], "networks": []}:
        print("refusing to start: DevPilot containers or networks already exist", world.leftovers())
        return 2
    unresolved = list(REPORTS.glob(f"*-guard/{QUARANTINE_FILE}"))
    if unresolved:
        print("refusing to start: a guard could not confirm a stop:", unresolved)
        return 2
    names = args.only or [n for n in SCENARIOS if n not in INTERNAL_SCENARIOS]
    started = time.monotonic()
    crashed: list[str] = []

    def report() -> Path:
        REPORTS.mkdir(parents=True, exist_ok=True)
        path = REPORTS / f"{stamp}-launcher-check.json"
        path.write_text(json.dumps({"stamp": stamp, "counts": results.counts, "seconds": round(time.monotonic() - started, 1), "rows": results.rows}, indent=1, default=str), encoding="utf-8")
        return path

    keep_awake()
    start_guard(args.guard_state or REPORTS / f"{stamp}-guard", deadline=args.guard_deadline or args.deadline * len(names) + 120.0, networks=(MODEL_NETWORK, CORE_NETWORK), docker=args.guard_docker)
    if not args.no_watchdog:
        threading.Thread(target=_watch, args=(world, report, args.deadline), daemon=True).start()
    for name in names:
        results.scenario = name
        world.scenario_began = time.monotonic()
        print(f"\n=== {name}", flush=True)
        try:
            SCENARIOS[name](world)
        except Exception as exc:
            crashed.append(name)
            results.check(f"{name} finished without an error", False, f"{type(exc).__name__}: {str(exc)[:300]}")
            _emergency_cleanup(world)
        except BaseException:  # Ctrl-C: the scenario's own `finally` ran first, if it could
            _emergency_cleanup(world)
            raise
    written = report()
    if not args.keep:
        shutil.rmtree(root, ignore_errors=True)
    print(f"\nTotals: {results.counts}  crashed: {crashed}  {time.monotonic() - started:.0f} s\nReport: {written}\nLeft behind: {world.leftovers()}")
    return 0 if not results.counts["FAIL"] and not crashed else 1


def expired(began: float, last: float, now: float, limit: float) -> str | None:
    """Why the check must stop now, if it must. time.monotonic() keeps counting through standby, so a gap is the sign."""
    if now - last > STANDBY_GAP_SECONDS:
        return f"the machine slept for about {now - last:.0f} s, so this run proves nothing"
    if now - began > limit:
        return f"the {limit:.0f} s deadline passed"
    return None


def _watch(world: World, report: Callable[[], Path], limit: float) -> None:
    """Runs on its own thread, so it ends the check whatever the main thread is blocked on."""
    last = time.monotonic()
    while True:
        time.sleep(1.0)
        now = time.monotonic()
        reason = expired(world.scenario_began, last, now, limit)
        last = now
        if reason is None:
            continue
        world.results.check("the check ended by itself", False, reason)
        print(f"\nStopping: {reason}\nTermination: {_emergency_cleanup(world)}\nReport: {report()}\nLeft behind: {world.leftovers()}", flush=True)
        os._exit(3)


def _emergency_cleanup(world: World) -> list[str]:
    """The order of OpenCodeSession.close(): the writer, confirmed by a separate read, then the edges, then the networks."""
    evidence = [ContainerTerminator(world.cli, cid).terminate().evidence for cid in world.cli.labelled_containers(CONTAINER_LABEL)]
    for cid in world.cli.labelled_containers(EDGE_LABEL):
        world.cli.remove(cid)
    for network in (MODEL_NETWORK, CORE_NETWORK):
        world.cli.remove_network(network)
    return evidence


if __name__ == "__main__":
    sys.exit(main())
