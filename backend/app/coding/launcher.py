"""Starting and stopping the isolated coding containers (ARCHITECTURE §6, SECURITY T3, T4).

One attempt gets its own set, and nothing outlives the attempt:

    backend --127.0.0.1:4096--> [API filter] --devpilot-core--> [OpenCode] --devpilot-model--> [gateway] --> api.openai.com

- OpenCode publishes no port and sits only on two internal networks, which have no route out. It holds
  neither the OpenAI key nor the filter's token. It mounts only the run's disposable copy, writable.
- The filter is the only thing the backend can reach. It alone holds OpenCode's password.
- The gateway alone holds the OpenAI key, and it counts this attempt's budget.

Every secret is fresh for the attempt and reaches docker by name, never on a command line.

A live start stays refused until a person has approved it, and `unavailable_reason` says what is
missing: an approved runtime image (the images on disk have no ripgrep, so OpenCode would try to
download it), the verified binary, a model, and the approval itself. Those checks stand in front of
`build_production_args`, which refuses by itself as well.

On stopping, the writer goes first and is confirmed by an independent read (`ContainerTerminator`).
The attempt's folder is removed only after that. If the stop cannot be confirmed, the folder and the
networks stay, and the attempt's guard quarantines the run.
"""

from __future__ import annotations

import hashlib
import json
import logging
import secrets
import shutil
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol

from app.coding.container_stop import ContainerTerminator
from app.coding.opencode_contract import FILTER_HEALTH_PATH, endpoint_problem
from app.coding.opencode_container import (
    APPROVED_RUNTIME_IMAGES,
    CONTAINER_LABEL,
    CORE_NETWORK,
    MODEL_NETWORK,
    OPENCODE_GITIGNORE,
    PASSWORD_ENV,
    RUN_USER,
    WORKSPACE_TARGET,
    ContainerPolicyError,
    ProductionContainer,
    RuntimeImage,
    build_production_args,
    deployment_problems,
    network_problems,
    verified_binary,
)
from app.coding.opencode_policy import GATEWAY_TOKEN_PREFIX, managed_config, session_rules
from app.coding.session_control import SessionControl, WriterControl
from app.coding.types import CodingProvider, Termination
from app.config import Settings
from app.core.egress import EgressPolicy
from app.sandbox.docker_cli import DockerCli, SandboxError
from app.sandbox.policy import APPROVED_IMAGES
from app.tools.adapters.opencode import PROVIDER_NAME, OpenCodeClient, OpenCodeError, OpenCodeProvider, session_control
from edge.filter_policy import TOKEN_ENV as FILTER_TOKEN_ENV
from edge.gateway_policy import TOKEN_ENV as GATEWAY_TOKEN_ENV
from edge.gateway_policy import UPSTREAM_KEY_ENV

logger = logging.getLogger("devpilot.coding")

INGRESS_NETWORK = "devpilot-ingress"  # the backend reaches the filter here, on loopback only
EGRESS_NETWORK = "devpilot-egress"  # the gateway reaches OpenAI here
# (name, internal): only the two networks that hold OpenCode must have no route out.
NETWORKS: tuple[tuple[str, bool], ...] = ((INGRESS_NETWORK, False), (CORE_NETWORK, True), (MODEL_NETWORK, True), (EGRESS_NETWORK, False))
EDGE_LABEL = "devpilot.edge"
EDGE_MOUNT = "/opt/edge"
LEDGER_MOUNT = "/var/devpilot/ledger"
FILTER_PORT = 8080
GATEWAY_PORT = 8081
OPENCODE_PORT = 4096
_HEALTH_ATTEMPTS = 60
_HEALTH_POLL_SECONDS = 0.5
EDGE_SOURCE = Path(__file__).resolve().parents[2] / "edge"
_EDGE_FILES = ("__init__.py", "filter_policy.py", "filter_server.py", "gateway_policy.py", "gateway_server.py")


class LaunchRefused(Exception):
    """The containers were not started, or were all removed again. The message holds no secret."""


@dataclass(frozen=True)
class LaunchRequest:
    run_id: str
    call_id: str
    workspace: Path  # the run's disposable copy
    token_budget: int  # what the run may still spend
    usd_budget: float | None = None  # the same in dollars, when prices are known


class CodingSession(Protocol):
    provider: CodingProvider
    writer: WriterControl
    control: SessionControl
    container_id: str | None
    usage: dict[str, Any]  # the gateway's own totals, after `close`

    def close(self) -> None:
        """End everything the session started. Never raises."""
        ...


class CodingLauncher(Protocol):
    def unavailable_reason(self) -> str | None:
        """Why no attempt can start right now, or None."""
        ...

    def start(self, request: LaunchRequest) -> CodingSession:
        """Start one attempt's containers, or raise LaunchRefused having left nothing behind."""
        ...

    def writer_for(self, container_id: str | None) -> WriterControl:
        """A stop for a container a previous backend started. Startup recovery uses it."""
        ...

    def usage_for(self, run_id: str, call_id: str) -> dict[str, Any]:
        """What an attempt's gateway recorded, for an attempt a stopped backend never accounted for."""
        ...

    def sweep(self) -> None:
        """Remove what a stopped backend left, after recovery. Only called when nothing is quarantined."""
        ...


@dataclass
class _Plan:
    """One attempt's fresh names, secrets and folders. Never logged."""

    suffix: str
    folder: Path
    filter_token: str
    password: str
    gateway_token: str

    @property
    def opencode_name(self) -> str:
        return f"devpilot-opencode-{self.suffix}"

    @property
    def filter_name(self) -> str:
        return f"devpilot-filter-{self.suffix}"

    @property
    def gateway_name(self) -> str:
        return f"devpilot-gateway-{self.suffix}"


def edge_args(name: str, *, networks: tuple[str, ...], publish: tuple[str, ...], env_names: tuple[str, ...], mounts: tuple[tuple[Path, str, bool], ...], argv: tuple[str, ...]) -> list[str]:
    """`docker run` for the filter or the gateway. Hardened like OpenCode's container, with less room."""
    args = [
        "run", "--detach", "--name", name, "--pull", "never", "--label", f"{EDGE_LABEL}=true",
        "--cap-drop", "ALL", "--security-opt", "no-new-privileges", "--read-only",
        "--tmpfs", "/tmp:rw,noexec,nosuid,nodev,size=16m,mode=1777", "--user", RUN_USER,
        "--memory", "256m", "--memory-swap", "256m", "--cpus", "0.5", "--pids-limit", "64",
        "--ulimit", "nofile=512:512", "--init",
    ]  # fmt: skip
    for network in networks:
        args += ["--network", network]
    for port in publish:
        args += ["--publish", port]
    for env_name in env_names:
        args += ["--env", env_name]
    for host, target, read_only in mounts:
        args += ["--mount", f"type=bind,source={host},target={target}" + (",readonly" if read_only else "")]
    return [*args, APPROVED_IMAGES["python-runner"].reference, *argv]


class OpenCodeSession:
    """One attempt's running containers."""

    def __init__(
        self,
        *,
        provider: CodingProvider,
        control: SessionControl,
        cli: DockerCli,
        container_id: str,
        edges: tuple[str, ...],
        folder: Path,
        client: OpenCodeClient,
        networks: tuple[tuple[str, bool], ...] = NETWORKS,
    ) -> None:
        self.provider = provider
        self.control = control
        self.container_id: str | None = container_id
        self.writer: WriterControl = ContainerTerminator(cli, container_id)
        self._cli = cli
        self._edges = edges
        self._folder = folder
        self._client = client
        self._networks = networks
        self.usage: dict[str, Any] = {}

    def close(self) -> None:
        self._client.close()
        stopped = _stop_quietly(self.writer)
        for edge in self._edges:
            self._cli.remove(edge)
        self.usage = read_ledger(self._folder / "ledger" / "ledger.jsonl")
        if stopped.confirmed:  # nothing may still be writing; otherwise everything stays for the review
            # The logs hold no secret and no prompt, and they are the evidence of what was refused and spent.
            shutil.copytree(self._folder / "ledger", self._folder.parent.parent / "evidence" / self._folder.name, dirs_exist_ok=True)
            shutil.rmtree(self._folder, ignore_errors=True)
            for name, _ in reversed(self._networks):
                self._cli.remove_network(name)
        else:
            logger.error("the coding container could not be confirmed stopped: %s", stopped.evidence)


def _stop_quietly(writer: WriterControl) -> Termination:
    try:
        return writer.terminate()
    except Exception as exc:  # close never raises: a failed stop is an unconfirmed stop
        return Termination(False, f"the stop raised {type(exc).__name__}")


def ledger_rows(path: Path) -> tuple[list[dict[str, Any]], bool]:
    """Each request once: what it settled for, or its whole reservation if the gateway died before it settled.
    The second value is False when a line could not be read."""
    rows: dict[Any, dict[str, Any]] = {}
    readable = True
    for index, line in enumerate(path.read_text(encoding="utf-8").splitlines()):
        try:
            entry = json.loads(line)
        except ValueError:
            entry = None
        if isinstance(entry, dict):
            rows[entry.get("request", f"line {index}")] = entry
        else:
            readable = False
    return list(rows.values()), readable


def read_ledger(path: Path) -> dict[str, Any]:
    """The gateway's own totals. Unreadable lines count as nothing known, never as nothing spent."""
    totals: dict[str, Any] = {"requests": 0, "charged_tokens": 0, "cost_usd": None, "all_known": True}
    if not path.is_file():
        return {**totals, "all_known": False}
    rows, readable = ledger_rows(path)
    totals["all_known"] = readable
    for entry in rows:
        totals["requests"] += 1
        totals["charged_tokens"] += entry.get("charged_tokens") or 0
        totals["all_known"] = totals["all_known"] and bool(entry.get("usage_known"))
        if entry.get("cost_usd") is not None:
            totals["cost_usd"] = (totals["cost_usd"] or 0.0) + entry["cost_usd"]
    return totals


class OpenCodeLauncher:
    """The real launcher. The no-model check runs it on real containers through a subclass that only
    removes from this layout (scripts/opencode_launcher_check.py). The live path needs the approvals."""

    def __init__(self, settings: Settings, cli: DockerCli, egress: EgressPolicy, *, sleep: Callable[[float], None] = time.sleep) -> None:
        self._settings = settings
        self._cli = cli
        self._egress = egress
        self._sleep = sleep

    @property
    def name(self) -> str:
        return PROVIDER_NAME

    def unavailable_reason(self) -> str | None:
        s = self._settings
        missing: list[str] = []
        if not s.opencode_enabled:
            missing.append("OPENCODE_ENABLED is false")
        if not s.coding_live_approved:
            missing.append("CODING_LIVE_APPROVED is false: live coding, Docker networks and model calls need a person's approval")
        if s.opencode_runtime_image not in APPROVED_RUNTIME_IMAGES:
            missing.append("no approved runtime image: the images on disk have no ripgrep, so OpenCode would try to download it")
        if s.opencode_manifest_file is None:
            missing.append("OPENCODE_MANIFEST_FILE is not set")
        if not (s.opencode_model or "").startswith("openai/"):
            missing.append("OPENCODE_MODEL is not an openai/<model> name")
        if s.openai_api_key is None:
            missing.append("OPENAI_API_KEY is not set")
        if not s.cost_known:
            missing.append("OPENAI_PRICE_INPUT_PER_MTOK and OPENAI_PRICE_OUTPUT_PER_MTOK are not set, so no spending limit could be enforced")
        if s.openai_max_output_tokens is None:
            missing.append("OPENAI_MAX_OUTPUT_TOKENS is not set, so a GPT call has no upper bound")
        if s.openai_max_retries != 0:
            missing.append("OPENAI_MAX_RETRIES must be 0: a retry the SDK makes by itself could be billed without being counted")
        endpoint = endpoint_problem(s, self._egress)
        if endpoint is not None:
            missing.append(endpoint)
        return "; ".join(missing) or None

    def writer_for(self, container_id: str | None) -> WriterControl:
        return ContainerTerminator(self._cli, container_id or "")

    def usage_for(self, run_id: str, call_id: str) -> dict[str, Any]:
        return read_ledger(self._attempt_folder(run_id, call_id) / "ledger" / "ledger.jsonl")

    def sweep(self) -> None:
        for label in (CONTAINER_LABEL, EDGE_LABEL):
            for container_id in self._cli.labelled_containers(label):
                self._cli.remove(container_id)
        for name, _ in reversed(self.networks()):
            self._cli.remove_network(name)
        attempts = self._settings.coding_work_dir / "attempts"
        for folder in attempts.iterdir() if attempts.is_dir() else ():
            if (folder / "ledger").is_dir():
                shutil.copytree(folder / "ledger", self._settings.coding_work_dir / "evidence" / folder.name, dirs_exist_ok=True)
        shutil.rmtree(attempts, ignore_errors=True)

    def networks(self) -> tuple[tuple[str, bool], ...]:
        return NETWORKS

    def _attempt_folder(self, run_id: str, call_id: str) -> Path:
        key = hashlib.sha256(f"{run_id}:{call_id}".encode()).hexdigest()[:32]
        return self._settings.coding_work_dir / "attempts" / key

    def start(self, request: LaunchRequest) -> OpenCodeSession:
        reason = self.unavailable_reason()
        if reason is not None:
            raise LaunchRefused(reason)
        plan = self._new_plan(request)
        started: list[str] = []
        try:
            return self._start(request, plan, started)
        except (SandboxError, ContainerPolicyError, OSError, OpenCodeError) as exc:
            detail = exc.message if isinstance(exc, OpenCodeError) else str(exc)
            self._undo(plan, started)
            raise LaunchRefused(f"the coding containers did not start: {detail}") from exc
        except BaseException:
            self._undo(plan, started)
            raise

    def _new_plan(self, request: LaunchRequest) -> _Plan:
        folder = self._attempt_folder(request.run_id, request.call_id)
        return _Plan(
            suffix=folder.name[:16],
            folder=folder,
            filter_token=secrets.token_urlsafe(32),
            password=secrets.token_urlsafe(32),
            gateway_token=GATEWAY_TOKEN_PREFIX + secrets.token_urlsafe(32),
        )

    def _start(self, request: LaunchRequest, plan: _Plan, started: list[str]) -> OpenCodeSession:
        s = self._settings
        container = self._write_files(request, plan)
        opencode_args = build_production_args(container, s)  # refuses before anything exists
        networks = self.networks()
        for name, internal in networks:
            self._cli.create_network(name, internal=internal)
        problems = [p for name, internal in networks if internal for p in network_problems(name, self._cli.inspect_network(name))]
        if problems:
            raise SandboxError("; ".join(problems))
        started.append(self._cli.run_detached(self._gateway_args(plan), env=self._gateway_env(plan)))
        opencode_id = self._cli.run_detached(opencode_args, env={PASSWORD_ENV: plan.password})
        started.append(opencode_id)
        problems = deployment_problems(self._cli.inspect_json(opencode_id) or {}, container)
        if problems:
            raise SandboxError("the OpenCode container is not as defined: " + "; ".join(problems[:5]))
        filter_id = self._cli.run_detached(self._filter_args(plan), env={FILTER_TOKEN_ENV: plan.filter_token, PASSWORD_ENV: plan.password})
        started.append(filter_id)
        client = OpenCodeClient(self._filter_url(filter_id), token=plan.filter_token, directory=WORKSPACE_TARGET)
        try:
            self._wait_for_filter(client)
        except BaseException:
            client.close()
            raise
        provider = OpenCodeProvider(client, model=s.opencode_model or "")
        edges = tuple(cid for cid in started if cid != opencode_id)
        return OpenCodeSession(provider=provider, control=session_control(client), cli=self._cli, container_id=opencode_id, edges=edges, folder=plan.folder, client=client, networks=networks)

    def _filter_url(self, filter_id: str) -> str:
        """Where the backend reaches the filter: the published loopback port."""
        return self._settings.opencode_base_url

    def _gateway_env(self, plan: _Plan) -> dict[str, str]:
        return {GATEWAY_TOKEN_ENV: plan.gateway_token, UPSTREAM_KEY_ENV: _key(self._settings)}

    def _write_files(self, request: LaunchRequest, plan: _Plan) -> ProductionContainer:
        """Everything the containers read, in a fresh folder under the work root."""
        s = self._settings
        model = s.opencode_model or ""
        folders = {name: plan.folder / name for name in ("config", "guard", "edge", "ledger")}
        (folders["edge"] / "edge").mkdir(parents=True)
        for name in ("config", "guard", "ledger"):
            folders[name].mkdir()
        gateway_url = f"http://{plan.gateway_name}:{GATEWAY_PORT}/v1"
        config = managed_config(model=model, gateway_base_url=gateway_url, gateway_token=plan.gateway_token)
        (folders["config"] / "opencode.json").write_text(json.dumps(config), encoding="utf-8")
        (folders["guard"] / ".gitignore").write_text(OPENCODE_GITIGNORE, encoding="utf-8", newline="\n")
        for name in _EDGE_FILES:
            shutil.copyfile(EDGE_SOURCE / name, folders["edge"] / "edge" / name)
        provider_id, _, model_id = model.partition("/")
        filter_config = {"directory": WORKSPACE_TARGET, "provider_id": provider_id, "model_id": model_id, "session_rules": session_rules(),
                         "upstream_host": plan.opencode_name, "upstream_port": OPENCODE_PORT}  # fmt: skip
        (folders["edge"] / "filter.json").write_text(json.dumps(filter_config), encoding="utf-8")
        gateway_config = {
            "model": model_id, "max_output_tokens": s.gateway_max_output_tokens, "max_requests": s.gateway_max_requests_per_task,
            "max_tokens": min(s.opencode_max_tokens_per_task, request.token_budget), "max_usd": _tightest(s.gateway_max_usd_per_task, request.usd_budget),
            "price_input_per_mtok": s.openai_price_input_per_mtok, "price_output_per_mtok": s.openai_price_output_per_mtok,
        }  # fmt: skip
        (folders["edge"] / "gateway.json").write_text(json.dumps(gateway_config), encoding="utf-8")
        image = RuntimeImage(image_id=s.opencode_runtime_image or "", ripgrep="/usr/local/bin/rg")
        return ProductionContainer(
            name=plan.opencode_name, image=image, binary=verified_binary(s.opencode_manifest_file or Path()), work_root=s.coding_work_dir,
            config_dir=folders["config"], global_config_dir=folders["guard"], work_copy=request.workspace,
        )  # fmt: skip

    def _filter_args(self, plan: _Plan) -> list[str]:
        port = self._settings.opencode_base_url.rsplit(":", 1)[-1].strip("/")
        return edge_args(
            plan.filter_name, networks=(INGRESS_NETWORK, CORE_NETWORK), publish=(f"127.0.0.1:{port}:{FILTER_PORT}",),
            env_names=(FILTER_TOKEN_ENV, PASSWORD_ENV), mounts=((plan.folder / "edge", EDGE_MOUNT, True), (plan.folder / "ledger", LEDGER_MOUNT, False)),
            argv=("python", "-I", "-B", f"{EDGE_MOUNT}/edge/filter_server.py", "--config", f"{EDGE_MOUNT}/filter.json",
                  "--listen-host", "0.0.0.0", "--listen-port", str(FILTER_PORT), "--audit", f"{LEDGER_MOUNT}/filter-audit.jsonl"),
        )  # fmt: skip

    def _gateway_args(self, plan: _Plan) -> list[str]:
        return edge_args(
            plan.gateway_name, networks=(EGRESS_NETWORK, MODEL_NETWORK), publish=(),
            env_names=(GATEWAY_TOKEN_ENV, UPSTREAM_KEY_ENV),
            mounts=((plan.folder / "edge", EDGE_MOUNT, True), (plan.folder / "ledger", LEDGER_MOUNT, False)),
            argv=("python", "-I", "-B", f"{EDGE_MOUNT}/edge/gateway_server.py", "--config", f"{EDGE_MOUNT}/gateway.json",
                  "--listen-host", "0.0.0.0", "--listen-port", str(GATEWAY_PORT), "--audit", f"{LEDGER_MOUNT}/gateway-audit.jsonl",
                  "--ledger", f"{LEDGER_MOUNT}/ledger.jsonl"),
        )  # fmt: skip

    def _wait_for_filter(self, client: OpenCodeClient) -> None:
        last = "no answer"
        for attempt in range(_HEALTH_ATTEMPTS):
            if attempt:
                self._sleep(_HEALTH_POLL_SECONDS)
            try:
                client.filter_health()
                client.health()
                return
            except OpenCodeError as exc:
                last = exc.message
        raise SandboxError(f"{FILTER_HEALTH_PATH} and OpenCode did not answer in time: {last}")

    def _undo(self, plan: _Plan, started: list[str]) -> None:
        for container_id in reversed(started):
            self._cli.remove(container_id)
        for name, _ in reversed(self.networks()):
            self._cli.remove_network(name)
        shutil.rmtree(plan.folder, ignore_errors=True)


def _tightest(*limits: float | None) -> float | None:
    known = [limit for limit in limits if limit is not None]
    return min(known) if known else None


def _key(settings: Settings) -> str:
    assert settings.openai_api_key is not None  # unavailable_reason checked it
    return settings.openai_api_key.get_secret_value()

