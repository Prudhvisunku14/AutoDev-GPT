"""The isolated launcher, against a fake Docker. It never starts a container and never calls a model.

These tests prove what the launcher asks Docker for and how it reacts to what Docker answers. Whether
real containers, networks and the filter behave as defined needs the approved live run.
"""

from __future__ import annotations

import json
import threading
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pytest

from app.coding import launcher as launcher_module
from app.coding import opencode_container as container
from app.coding.launcher import EDGE_LABEL, EGRESS_NETWORK, INGRESS_NETWORK, NETWORKS, LaunchRefused, LaunchRequest, OpenCodeLauncher, edge_args, read_ledger
from app.coding.opencode_container import CORE_NETWORK, IMAGE_ENV_NAMES, MEMORY_BYTES, MODEL_NETWORK, NANO_CPUS, PASSWORD_ENV, PIDS_LIMIT, PRODUCTION_SERVER_ARGV, RUN_USER, VerifiedBinary, production_environment
from app.coding.opencode_policy import GATEWAY_TOKEN_PREFIX
from app.sandbox.docker_cli import DockerCli
from app.sandbox.process import DEFAULT_CAPTURE_BYTES, ProcessResult
from app.tools.adapters.opencode import OpenCodeError
from edge.filter_policy import TOKEN_ENV as FILTER_TOKEN_ENV
from edge.filter_policy import Policy
from edge.filter_server import load_config
from edge.gateway_policy import TOKEN_ENV as GATEWAY_TOKEN_ENV
from edge.gateway_policy import UPSTREAM_KEY_ENV, GatewayPolicy
from tests.helpers import FAKE_OPENAI_KEY, make_settings

IMAGE = "sha256:" + "c" * 64
RUN = "d" * 32


@dataclass(frozen=True)
class _Decision:
    allowed: bool
    reason: str = "not listed"


@dataclass(frozen=True)
class FakeEgress:
    """The real rule file holds this entry only in a working copy, so tests state the answer themselves."""

    allowed: bool = True

    def check(self, component: str, url: str) -> _Decision:
        return _Decision(self.allowed)


@dataclass
class FakeDocker:
    """Answers the calls the launcher makes, and records each with the secrets given by name."""

    calls: list[list[str]] = field(default_factory=list)
    env_seen: list[dict[str, str]] = field(default_factory=list)
    networks: dict[str, bool] = field(default_factory=dict)
    containers: dict[str, list[str]] = field(default_factory=dict)  # id -> run arguments
    fail_on: str | None = None  # the container name part whose start fails
    internal_lie: bool = False
    deployment: dict[str, Any] = field(default_factory=dict)
    next_id: int = 1

    def run(self, argv: Sequence[str], *, timeout: float, cancel: threading.Event | None = None, capture_bytes: int = DEFAULT_CAPTURE_BYTES, env: Mapping[str, str] | None = None) -> ProcessResult:
        args = list(argv[1:])
        self.calls.append(args)
        if env:
            self.env_seen.append(dict(env))
        verb = args[0]
        if verb == "network":
            return self._network(args)
        if verb == "run":
            return self._run(args)
        if verb == "container":
            return self._inspect(args)
        if verb == "rm":
            self.containers.pop(args[-1], None)
            return ProcessResult(returncode=0)
        if verb == "ps":
            label = args[-1].removeprefix("label=").removesuffix("=true")
            return ProcessResult(returncode=0, stdout="".join(f"{cid}\n" for cid, run in self.containers.items() if f"{label}=true" in run).encode())
        raise AssertionError(f"unexpected docker call: {args}")

    def _network(self, args: list[str]) -> ProcessResult:
        if args[1] == "create":
            self.networks[args[-1]] = "--internal" in args
            return ProcessResult(returncode=0, stdout=b"id\n")
        if args[1] == "inspect":
            internal = self.networks.get(args[-1], False) and not self.internal_lie
            return ProcessResult(returncode=0, stdout=json.dumps([{"Name": args[-1], "Internal": internal}]).encode())
        self.networks.pop(args[-1], None)
        return ProcessResult(returncode=0)

    def _run(self, args: list[str]) -> ProcessResult:
        name = args[args.index("--name") + 1]
        if self.fail_on and self.fail_on in name:
            return ProcessResult(returncode=125, stderr=b"docker: port is already allocated")
        container_id = f"{self.next_id:064x}"
        self.next_id += 1
        self.containers[container_id] = args
        return ProcessResult(returncode=0, stdout=f"{container_id}\n".encode())

    def _inspect(self, args: list[str]) -> ProcessResult:
        reference = args[-1]
        if reference not in self.containers:
            return ProcessResult(returncode=1, stderr=f"Error response from daemon: No such container: {reference}\n".encode())
        if "--format" in args:
            fields = (reference, "running", "true", 42, 0, "false")
            return ProcessResult(returncode=0, stdout=("|".join(map(str, fields)) + "\n").encode())
        return ProcessResult(returncode=0, stdout=json.dumps([{**self._as_inspected(self.containers[reference]), **self.deployment}]).encode())

    @staticmethod
    def _as_inspected(run: list[str]) -> dict[str, Any]:
        env = [run[i + 1] for i, a in enumerate(run) if a == "--env"]
        mounts = [dict(part.split("=", 1) if "=" in part else (part, "1") for part in run[i + 1].split(",")) for i, a in enumerate(run) if a == "--mount"]
        return {
            "Image": IMAGE,
            "HostConfig": {
                "NetworkMode": CORE_NETWORK, "Privileged": False, "ReadonlyRootfs": True, "PublishAllPorts": False, "Memory": MEMORY_BYTES,
                "MemorySwap": MEMORY_BYTES, "NanoCpus": NANO_CPUS, "PidsLimit": PIDS_LIMIT, "CapDrop": ["ALL"], "SecurityOpt": ["no-new-privileges"],
                "PortBindings": {}, "Tmpfs": {"/tmp": "rw,noexec,nosuid,nodev,size=256m,mode=1777"},
            },
            "Config": {"User": RUN_USER, "Env": [*env, *[f"{n}=x" for n in (PASSWORD_ENV,)], *[f"{n}=x" for n in sorted(IMAGE_ENV_NAMES)]], "Cmd": list(PRODUCTION_SERVER_ARGV), "Entrypoint": None},
            "Mounts": [{"Type": "bind", "Source": m["source"], "Destination": m["target"], "RW": "readonly" not in m} for m in mounts],
            "NetworkSettings": {"Networks": {CORE_NETWORK: {}, MODEL_NETWORK: {}}},
        }  # fmt: skip


class FakeClient:
    def __init__(self, *_: Any, **kwargs: Any) -> None:
        self.kwargs = kwargs
        self.closed = False

    def filter_health(self) -> None:
        return None

    def health(self) -> str:
        return "1.19.5"

    def close(self) -> None:
        self.closed = True


class Rig:
    def __init__(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, *, client: type = FakeClient, egress_allowed: bool = True, **overrides: Any) -> None:
        self.docker = FakeDocker()
        self.work = tmp_path / "work"
        self.work.mkdir(exist_ok=True)
        self.copy = self.work / RUN
        self.copy.mkdir(exist_ok=True)
        binary = tmp_path / "binary"
        binary.mkdir(exist_ok=True)
        (binary / "opencode").write_bytes(b"x")
        values: dict[str, Any] = {
            "opencode_enabled": True, "coding_live_approved": True, "opencode_runtime_image": IMAGE, "opencode_model": "openai/gpt-test",
            "opencode_manifest_file": tmp_path / "manifest.json", "openai_price_input_per_mtok": 2.0, "openai_price_output_per_mtok": 10.0,
            "gateway_max_usd_per_task": 0.5, **overrides,
        }  # fmt: skip
        self.settings = make_settings(tmp_path, **values)
        monkeypatch.setattr(launcher_module, "APPROVED_RUNTIME_IMAGES", frozenset({IMAGE}))
        monkeypatch.setattr(container, "APPROVED_RUNTIME_IMAGES", frozenset({IMAGE}))
        monkeypatch.setattr(launcher_module, "verified_binary", lambda _: VerifiedBinary(binary, container.PINNED_BINARY_SHA256))
        monkeypatch.setattr(launcher_module, "OpenCodeClient", client)
        monkeypatch.setattr(container, "protected_paths", lambda _s: (tmp_path / "protected",))
        monkeypatch.setattr(launcher_module, "OpenCodeProvider", lambda api, model: ("provider", api, model))
        self.launcher = OpenCodeLauncher(self.settings, DockerCli("docker", self.docker), FakeEgress(egress_allowed), sleep=lambda _: None)

    def request(self) -> LaunchRequest:
        return LaunchRequest(run_id=RUN, call_id="call_1", workspace=self.copy, token_budget=50_000)

    def start(self) -> Any:
        return self.launcher.start(self.request())

    def run_args(self, name_part: str) -> list[str]:
        return next(args for args in self.docker.containers.values() if name_part in args[args.index("--name") + 1])

    def everything(self) -> str:
        return json.dumps([self.docker.calls])


@pytest.fixture
def rig(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Rig:
    return Rig(tmp_path, monkeypatch)


# --- What keeps it from starting --------------------------------------------------------------------


def test_by_default_every_gate_is_closed(tmp_path: Path) -> None:
    reason = OpenCodeLauncher(make_settings(tmp_path), DockerCli("docker", FakeDocker()), FakeEgress(False)).unavailable_reason()
    assert reason is not None
    for gate in ("OPENCODE_ENABLED", "CODING_LIVE_APPROVED", "no approved runtime image", "OPENCODE_MANIFEST_FILE", "OPENCODE_MODEL", "egress allowlist"):
        assert gate in reason


@pytest.mark.parametrize(
    ("change", "reason"),
    [({"opencode_enabled": False}, "OPENCODE_ENABLED"), ({"coding_live_approved": False}, "approval"), ({"opencode_runtime_image": "sha256:" + "e" * 64}, "ripgrep"),
     ({"opencode_model": "anthropic/x"}, "openai/"), ({"opencode_manifest_file": None}, "MANIFEST"), ({"openai_api_key": None}, "OPENAI_API_KEY"),
     ({"openai_price_output_per_mtok": None}, "spending limit")],
)  # fmt: skip
def test_each_gate_alone_refuses_and_nothing_is_touched(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, change: dict[str, Any], reason: str) -> None:
    rig = Rig(tmp_path, monkeypatch, **change)
    with pytest.raises(LaunchRefused, match=reason):
        rig.start()
    assert rig.docker.calls == []


def test_an_address_the_allowlist_refuses_stops_the_start(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    rig = Rig(tmp_path, monkeypatch, egress_allowed=False)
    with pytest.raises(LaunchRefused, match="egress allowlist"):
        rig.start()
    assert rig.docker.calls == []


def test_a_filter_address_off_this_machine_stops_the_start(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    rig = Rig(tmp_path, monkeypatch, opencode_base_url="http://192.0.2.1:4096")
    with pytest.raises(LaunchRefused, match="loopback"):
        rig.start()


def test_the_shipped_code_approves_no_runtime_image() -> None:
    assert container.APPROVED_RUNTIME_IMAGES == frozenset()


# --- What it asks Docker for ------------------------------------------------------------------------


def test_opencode_gets_only_internal_networks_and_no_port(rig: Rig) -> None:
    rig.start()
    args = rig.run_args("opencode")
    networks = [args[i + 1] for i, a in enumerate(args) if a == "--network"]
    assert networks == [CORE_NETWORK, MODEL_NETWORK] and rig.docker.networks[CORE_NETWORK] is True and rig.docker.networks[MODEL_NETWORK] is True
    assert "--publish" not in args and "-p" not in args


def test_only_the_filter_publishes_a_port_and_only_on_loopback(rig: Rig) -> None:
    rig.start()
    published = [(args, args[i + 1]) for args in rig.docker.containers.values() for i, a in enumerate(args) if a == "--publish"]
    assert len(published) == 1 and published[0][1] == "127.0.0.1:4096:8080" and "filter" in " ".join(published[0][0])


def test_each_network_is_shared_only_as_designed(rig: Rig) -> None:
    rig.start()

    def nets(name: str) -> set[str]:
        args = rig.run_args(name)
        return {args[i + 1] for i, a in enumerate(args) if a == "--network"}

    assert nets("filter") == {INGRESS_NETWORK, CORE_NETWORK} and nets("gateway") == {EGRESS_NETWORK, MODEL_NETWORK}
    assert not nets("filter") & nets("gateway"), "the filter must not be able to reach the gateway"


def test_only_the_copy_is_mounted_for_writing_in_opencode(rig: Rig) -> None:
    rig.start()
    args = rig.run_args("opencode")
    mounts = [args[i + 1] for i, a in enumerate(args) if a == "--mount"]
    writable = [m for m in mounts if not m.endswith(",readonly")]
    assert len(writable) == 1 and f"source={rig.copy}," in writable[0] and writable[0].endswith("target=/workspace")
    assert str(rig.settings.workspaces_dir) not in " ".join(mounts)


def test_the_real_project_can_never_be_the_mount(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    rig = Rig(tmp_path, monkeypatch)
    real = rig.settings.workspaces_dir / RUN
    real.mkdir(parents=True)
    with pytest.raises(LaunchRefused, match="did not start"):
        rig.launcher.start(LaunchRequest(run_id=RUN, call_id="c", workspace=real, token_budget=1))
    assert rig.docker.containers == {}


def test_a_project_that_could_configure_the_engine_is_refused_at_the_mount(rig: Rig) -> None:
    (rig.copy / "opencode.json").write_text("{}", encoding="utf-8")
    with pytest.raises(LaunchRefused):
        rig.start()
    assert rig.docker.containers == {} and rig.docker.networks == {}


def test_project_config_stays_off_and_the_install_guard_stays_on(rig: Rig) -> None:
    rig.start()
    joined = " ".join(rig.run_args("opencode"))
    assert "OPENCODE_DISABLE_PROJECT_CONFIG=1" in joined and "target=/opt/devpilot/xdg-config/opencode,readonly" in joined


def test_each_edge_container_is_hardened(rig: Rig) -> None:
    rig.start()
    for name in ("filter", "gateway"):
        joined = " ".join(rig.run_args(name))
        for flag in ("--cap-drop ALL", "--security-opt no-new-privileges", "--read-only", f"--user {RUN_USER}", "--pull never", "--memory 256m", "--pids-limit 64"):
            assert flag in joined, (name, flag)
        assert "--privileged" not in joined and "docker.sock" not in joined


# --- Secrets ----------------------------------------------------------------------------------------


def test_no_secret_is_on_any_command_line(rig: Rig) -> None:
    rig.start()
    text = rig.everything()
    assert FAKE_OPENAI_KEY not in text
    for env in rig.docker.env_seen:
        for value in env.values():
            assert value not in text


def test_each_container_gets_only_its_own_secrets(rig: Rig) -> None:
    rig.start()
    gateway, opencode, filter_ = rig.docker.env_seen
    assert set(gateway) == {GATEWAY_TOKEN_ENV, UPSTREAM_KEY_ENV} and gateway[UPSTREAM_KEY_ENV] == FAKE_OPENAI_KEY
    assert set(opencode) == {PASSWORD_ENV}
    assert set(filter_) == {FILTER_TOKEN_ENV, PASSWORD_ENV} and filter_[PASSWORD_ENV] == opencode[PASSWORD_ENV]


def test_the_openai_key_never_reaches_opencode_or_its_files(rig: Rig) -> None:
    rig.start()
    assert UPSTREAM_KEY_ENV not in rig.run_args("opencode")
    for path in rig.work.rglob("*.json"):
        assert FAKE_OPENAI_KEY not in path.read_text(encoding="utf-8")


def test_opencode_authenticates_to_the_gateway_with_a_per_run_token(rig: Rig) -> None:
    rig.start()
    config = next(rig.work.rglob("opencode.json"))
    options = json.loads(config.read_text(encoding="utf-8"))["provider"]["openai"]["options"]
    token = rig.docker.env_seen[0][GATEWAY_TOKEN_ENV]
    assert options["apiKey"] == token and token.startswith(GATEWAY_TOKEN_PREFIX) and options["baseURL"].endswith(":8081/v1")


def test_every_secret_is_fresh_for_each_attempt(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    rig = Rig(tmp_path, monkeypatch)
    rig.start()
    rig.launcher.start(LaunchRequest(run_id=RUN, call_id="call_2", workspace=rig.copy, token_budget=1_000))
    first, second = rig.docker.env_seen[0:3], rig.docker.env_seen[3:6]
    assert all(a[k] != b[k] for a, b in zip(first, second, strict=True) for k in a if k != UPSTREAM_KEY_ENV)


def test_the_gateway_is_given_this_runs_model_and_budget(rig: Rig) -> None:
    rig.start()
    config = json.loads(next(rig.work.rglob("gateway.json")).read_text(encoding="utf-8"))
    assert config["model"] == "gpt-test" and config["max_tokens"] == 50_000 and config["max_requests"] == rig.settings.gateway_max_requests_per_task


@pytest.mark.parametrize(("left", "expected"), [(0.2, 0.2), (3.0, 0.5), (None, 0.5)])
def test_an_attempt_may_spend_only_what_the_run_has_left(rig: Rig, left: float | None, expected: float) -> None:
    rig.launcher.start(LaunchRequest(run_id=RUN, call_id="c", workspace=rig.copy, token_budget=50_000, usd_budget=left))
    assert json.loads(next(rig.work.rglob("gateway.json")).read_text(encoding="utf-8"))["max_usd"] == expected


def test_the_gateway_config_the_launcher_writes_is_one_the_gateway_accepts(rig: Rig) -> None:
    rig.start()
    policy = GatewayPolicy.from_config(json.loads(next(rig.work.rglob("gateway.json")).read_text(encoding="utf-8")))
    assert policy.model == "gpt-test" and policy.max_usd == 0.5


def test_the_filter_config_the_launcher_writes_is_one_the_filter_accepts(rig: Rig) -> None:
    rig.start()
    path = next(rig.work.rglob("filter.json"))
    assert Policy.from_config(load_config(path)).model_id == "gpt-test"


def test_the_filter_is_given_this_runs_model_and_rules(rig: Rig) -> None:
    rig.start()
    config = json.loads(next(rig.work.rglob("filter.json")).read_text(encoding="utf-8"))
    assert config["provider_id"] == "openai" and config["model_id"] == "gpt-test" and config["directory"] == "/workspace"
    assert config["upstream_host"].startswith("devpilot-opencode-") and config["upstream_port"] == 4096


# --- When something goes wrong ----------------------------------------------------------------------


@pytest.mark.parametrize("failing", ["gateway", "opencode", "filter"])
def test_a_failed_start_leaves_nothing_behind(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, failing: str) -> None:
    rig = Rig(tmp_path, monkeypatch)
    rig.docker.fail_on = failing
    with pytest.raises(LaunchRefused, match="port is already allocated"):
        rig.start()
    assert rig.docker.containers == {} and rig.docker.networks == {} and not (rig.work / "attempts" / "x").exists()
    assert list((rig.work / "attempts").iterdir()) == []


def test_a_network_that_is_not_internal_stops_the_start(rig: Rig) -> None:
    rig.docker.internal_lie = True
    with pytest.raises(LaunchRefused, match="route out"):
        rig.start()
    assert rig.docker.containers == {}


def test_a_container_that_differs_from_its_definition_stops_the_start(rig: Rig) -> None:
    rig.docker.deployment = {"NetworkSettings": {"Networks": {CORE_NETWORK: {}, "bridge": {}}}}
    with pytest.raises(LaunchRefused, match="not as defined"):
        rig.start()
    assert rig.docker.containers == {} and rig.docker.networks == {}


def test_a_filter_that_never_answers_stops_the_start(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from app.coding.types import CodingStatus

    class Silent(FakeClient):
        def filter_health(self) -> None:
            raise OpenCodeError(CodingStatus.UNAVAILABLE, "no answer")

    rig = Rig(tmp_path, monkeypatch, client=Silent)
    with pytest.raises(LaunchRefused, match="did not answer in time"):
        rig.start()
    assert rig.docker.containers == {} and rig.docker.networks == {}


# --- Stopping ---------------------------------------------------------------------------------------


def test_closing_stops_everything_and_removes_the_folder(rig: Rig) -> None:
    session = rig.start()
    folder = next((rig.work / "attempts").iterdir())
    session.close()
    assert rig.docker.containers == {} and rig.docker.networks == {} and not folder.exists() and session._client.closed


def test_the_audit_logs_outlive_the_attempt_and_hold_no_secret(rig: Rig) -> None:
    session = rig.start()
    folder = next((rig.work / "attempts").iterdir())
    (folder / "ledger" / "filter-audit.jsonl").write_text('{"status": 403}\n', encoding="utf-8")
    session.close()
    kept = rig.work / "evidence" / folder.name / "filter-audit.jsonl"
    assert kept.read_text(encoding="utf-8") == '{"status": 403}\n'
    assert sorted(p.name for p in kept.parent.iterdir()) == ["filter-audit.jsonl"]


def test_only_logs_are_writable_in_the_edge_containers(rig: Rig) -> None:
    rig.start()
    for name in ("filter", "gateway"):
        args = rig.run_args(name)
        mounts = [args[i + 1] for i, a in enumerate(args) if a == "--mount"]
        writable = [m for m in mounts if not m.endswith(",readonly")]
        assert len(writable) == 1 and writable[0].endswith("target=/var/devpilot/ledger") and "\\workspaces" not in writable[0]


def test_the_writer_is_stopped_before_anything_else(rig: Rig) -> None:
    session = rig.start()
    rig.docker.calls.clear()
    session.close()
    removed = [args[-1] for args in rig.docker.calls if args[0] == "rm"]
    assert removed[0] == session.container_id


def test_a_stop_that_cannot_be_confirmed_leaves_the_evidence_in_place(rig: Rig) -> None:
    session = rig.start()
    folder = next((rig.work / "attempts").iterdir())

    class Unconfirmed:
        def terminate(self) -> Any:
            from app.coding.types import Termination

            return Termination(False, "docker did not answer")

    session.writer = Unconfirmed()
    session.close()
    assert folder.is_dir() and rig.docker.networks != {}


def test_closing_never_raises(rig: Rig) -> None:
    session = rig.start()

    class Broken:
        def terminate(self) -> Any:
            raise RuntimeError("docker exploded")

    session.writer = Broken()
    session.close()


def test_the_stop_uses_the_full_id_and_an_independent_read(rig: Rig) -> None:
    session = rig.start()
    assert len(session.container_id) == 64
    rig.docker.calls.clear()
    assert session.writer.terminate().confirmed is True
    assert rig.docker.calls[-1][:2] == ["container", "inspect"]


def test_a_recovered_attempt_with_no_id_cannot_be_confirmed(rig: Rig) -> None:
    assert rig.launcher.writer_for(None).terminate().confirmed is False


def test_the_sweep_removes_what_a_crash_left(rig: Rig) -> None:
    rig.start()
    rig.launcher.sweep()
    assert rig.docker.containers == {} and rig.docker.networks == {} and not (rig.work / "attempts").exists()


# --- The rest ---------------------------------------------------------------------------------------


def test_the_ledger_is_totalled(tmp_path: Path) -> None:
    path = tmp_path / "ledger.jsonl"
    path.write_text(json.dumps({"charged_tokens": 10, "usage_known": True, "cost_usd": 0.5}) + "\n" + json.dumps({"charged_tokens": 5, "usage_known": True, "cost_usd": 0.25}) + "\n", encoding="utf-8")
    assert read_ledger(path) == {"requests": 2, "charged_tokens": 15, "cost_usd": 0.75, "all_known": True}


def test_a_missing_or_damaged_ledger_says_so(tmp_path: Path) -> None:
    assert read_ledger(tmp_path / "none.jsonl")["all_known"] is False
    path = tmp_path / "bad.jsonl"
    path.write_text("not json\n", encoding="utf-8")
    assert read_ledger(path)["all_known"] is False


def test_edge_args_never_take_a_secret_value() -> None:
    args = edge_args("n", networks=("a",), publish=(), env_names=("SECRET_NAME",), mounts=(), argv=("python",))
    assert args[args.index("--env") + 1] == "SECRET_NAME" and f"{EDGE_LABEL}=true" in args


def test_all_four_networks_are_named_with_the_two_internal_ones_closed() -> None:
    assert dict(NETWORKS) == {INGRESS_NETWORK: False, CORE_NETWORK: True, MODEL_NETWORK: True, EGRESS_NETWORK: False}


def test_production_environment_is_unchanged_by_the_launcher(rig: Rig) -> None:
    rig.start()
    args = rig.run_args("opencode")
    assert {f"{n}={v}" for n, v in production_environment()} <= set(args)
