"""The no-model check itself must not be able to reach a model. Proved with no container and no network."""

from __future__ import annotations

import base64
import importlib.util
import json
import re
import sys
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest

from app.coding.launcher import NETWORKS
from app.coding.opencode_container import CORE_NETWORK, MODEL_NETWORK
from edge.gateway_policy import Usage, UsageScanner
from tests.unit.test_launcher import FakeDocker, Rig

SCRIPTS = Path(__file__).resolve().parents[2] / "scripts"


def _load(name: str, path: Path) -> ModuleType:
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="module")
def check() -> ModuleType:
    return _load("devpilot_launcher_check", SCRIPTS / "opencode_launcher_check.py")


@pytest.fixture(scope="module")
def fake() -> ModuleType:
    return _load("devpilot_fake_model_gateway", SCRIPTS / "opencode_check" / "fake_model_gateway.py")


@pytest.fixture
def launcher(check: ModuleType, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Any:
    rig = Rig(tmp_path, monkeypatch, openai_api_key=None)
    built = check.NoModelLauncher(rig.settings, rig.launcher._cli)
    built.rig = rig
    return built


def _plan(tmp_path: Path) -> Any:
    class Plan:
        filter_name = "devpilot-filter-x"
        gateway_name = "devpilot-gateway-x"
        folder = tmp_path

    return Plan()


def test_the_check_uses_only_internal_networks(launcher: Any) -> None:
    assert launcher.networks() == ((CORE_NETWORK, True), (MODEL_NETWORK, True))
    assert all(internal for _, internal in launcher.networks())


def test_the_live_layout_is_unchanged_by_the_check() -> None:
    assert len(NETWORKS) == 4


def test_the_filter_gets_no_published_port_and_no_ingress_network(launcher: Any, tmp_path: Path) -> None:
    args = launcher._filter_args(_plan(tmp_path))
    assert "--publish" not in args and [args[i + 1] for i, a in enumerate(args) if a == "--network"] == [CORE_NETWORK]


def test_the_gateway_gets_only_the_internal_model_network(launcher: Any, tmp_path: Path) -> None:
    args = launcher._gateway_args(_plan(tmp_path))
    assert [args[i + 1] for i, a in enumerate(args) if a == "--network"] == [MODEL_NETWORK] and "--publish" not in args
    assert any("fake_model_gateway.py" in a for a in args) and not any("gateway_server.py" in a for a in args)


def test_the_gateway_is_given_a_token_and_no_key(launcher: Any) -> None:
    class Plan:
        gateway_token = "t" * 43

    assert set(launcher._gateway_env(Plan())) == {"DEVPILOT_GATEWAY_TOKEN"}


def test_the_check_refuses_to_run_holding_a_provider_key(check: ModuleType, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    rig = Rig(tmp_path, monkeypatch)  # this one holds the fake test key
    assert "no provider key" in check.NoModelLauncher(rig.settings, rig.launcher._cli).unavailable_reason()


def test_with_no_key_and_the_approved_image_the_check_may_run(check: ModuleType, launcher: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    assert "not the approved runtime image" in launcher.unavailable_reason()
    monkeypatch.setattr(check, "APPROVED_RUNTIME_IMAGES", frozenset({launcher.rig.settings.opencode_runtime_image}))
    assert launcher.unavailable_reason() is None


def test_the_check_never_asks_docker_for_more_than_the_live_layout_allows(launcher: Any) -> None:
    docker: FakeDocker = launcher.rig.docker
    assert docker.calls == []
    assert not re.search(r"--privileged|--cap-add|docker\.sock|--network[ =]host", (SCRIPTS / "opencode_launcher_check.py").read_text(encoding="utf-8"))


def test_the_check_names_no_provider_address(check: ModuleType) -> None:
    source = Path(check.__file__).read_text(encoding="utf-8") + (SCRIPTS / "opencode_check" / "fake_model_gateway.py").read_text(encoding="utf-8")
    assert "api.openai.com" not in source and "TlsUpstream" not in source.split("Only `TlsUpstream`")[-1]


def _input(*items: dict[str, Any]) -> dict[str, Any]:
    return {"tools": [{"type": "function", "name": "read"}], "input": list(items)}


def _user(text: str) -> dict[str, Any]:
    return {"role": "user", "content": [{"type": "input_text", "text": text}]}


def _marker(steps: list[dict[str, Any]]) -> str:
    return "DEVPILOT-STEPS:" + base64.b64encode(json.dumps(steps).encode()).decode() + ":END"


def test_a_prompt_names_its_steps_one_per_turn(fake: ModuleType) -> None:
    steps = [{"tool": "read"}, {"tool": "grep"}]
    prompt = _user(f"before {_marker(steps)} after")
    assert fake.step_from_prompt(_input(prompt)) == {"tool": "read"}
    assert fake.step_from_prompt(_input(prompt, {"type": "function_call_output", "output": "x"})) == {"tool": "grep"}
    assert fake.step_from_prompt(_input(prompt, {"type": "function_call_output"}, {"type": "function_call_output"})) == {"text": "Done."}


def test_only_the_newest_prompt_counts(fake: ModuleType) -> None:
    old = _user(_marker([{"tool": "old"}]))
    new = _user(_marker([{"tool": "new"}]))
    assert fake.step_from_prompt(_input(old, {"type": "function_call_output"}, new)) == {"tool": "new"}


def test_a_prompt_with_no_steps_falls_to_the_script(fake: ModuleType) -> None:
    assert fake.step_from_prompt(_input(_user("plain"))) is None
    assert fake.step_from_prompt({"input": "text"}) is None


def test_the_title_request_gets_a_title_and_leaves_the_script_alone(fake: ModuleType) -> None:
    upstream = fake.ScriptedUpstream([{"text": "first"}])
    title = upstream.send(json.dumps({"input": [_user("name this")], "tools": []}).encode(), stream=True)
    assert b"Scripted title" in title.read(100_000)
    answer = upstream.send(json.dumps(_input(_user("x"))).encode(), stream=True)
    assert b"first" in answer.read(100_000)


@pytest.mark.parametrize("build", ["text", "tool"])
def test_scripted_answers_carry_the_usage_the_gateway_reads(fake: ModuleType, build: str) -> None:
    body = fake.text_turn(1, "hi") if build == "text" else fake.tool_turn(1, "read", {"filePath": "/workspace/x"})
    scanner = UsageScanner()
    scanner.feed(body)
    assert scanner.usage == Usage(1_000, 100, 0)


def test_a_scripted_timeout_and_failure_are_what_the_gateway_handles(fake: ModuleType) -> None:
    upstream = fake.ScriptedUpstream([{"raise_timeout": True}, {"status": 401}])
    with pytest.raises(TimeoutError):
        upstream.send(json.dumps(_input(_user("x"))).encode(), stream=True)
    assert upstream.send(json.dumps(_input(_user("x"))).encode(), stream=True).status == 401


def test_the_patch_helpers_write_opencodes_format(check: ModuleType) -> None:
    text = check.add_file("/workspace/a.py", "x = 1\ny = 2\n")["arguments"]["patchText"]
    assert text == "*** Begin Patch\n*** Add File: /workspace/a.py\n+x = 1\n+y = 2\n*** End Patch"
    assert "*** Update File: /workspace/a.py\n@@\n-old\n+new" in check.update_file("/workspace/a.py", "old", "new")["arguments"]["patchText"]
    assert "*** Move to: /workspace/b.py" in check.move_file("/workspace/a.py", "/workspace/b.py")["arguments"]["patchText"]


def test_a_step_with_no_tool_call_is_no_refusal(check: ModuleType) -> None:
    assert check.refused([]) is False
    assert check.refused([{"status": "error"}]) is True
    assert check.refused([{"status": "error"}, {"status": "completed"}]) is False


class _Statuses:
    def __init__(self, status: dict[str, Any]) -> None:
        self.status = status
        self.calls = 0

    def statuses(self) -> dict[str, Any]:
        self.calls += 1
        return {"ses_x": self.status}


def test_a_session_that_is_retrying_stops_the_check_at_once(check: ModuleType) -> None:
    client = _Statuses({"type": "retry", "attempt": 1, "message": "Too Many Requests"})
    with pytest.raises(check.StepFailed, match="Too Many Requests"):
        check._wait_idle(client, "ses_x", seconds=60)
    assert client.calls == 1


def test_a_busy_session_is_waited_for_and_gives_up_at_the_limit(check: ModuleType, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(check.time, "sleep", lambda _: None)
    assert check._wait_idle(_Statuses({"type": "busy"}), "ses_x", seconds=0.01) is False


def test_the_engine_limits_exceed_what_its_own_script_needs(check: ModuleType) -> None:
    prompts = len(re.findall(r"\brun\(", Path(check.__file__).read_text(encoding="utf-8").split("def engine(world")[1].split("def _generate")[0]))
    assert prompts + 16 <= check.ENGINE_REQUESTS // 2, "two model turns a prompt; the loops in the script repeat some of these"


@pytest.mark.parametrize(
    ("began", "last", "now", "limit", "expected"),
    [(0.0, 9.0, 10.0, 60.0, None), (0.0, 59.5, 60.0, 60.0, None), (0.0, 60.0, 60.5, 60.0, "deadline"), (0.0, 10.0, 2700.0, 9999.0, "slept")],
)
def test_the_watchdog_stops_at_the_deadline_and_on_a_clock_gap(check: ModuleType, began: float, last: float, now: float, limit: float, expected: str | None) -> None:
    reason = check.expired(began, last, now, limit)
    assert (reason is None) if expected is None else (expected in reason)


def test_emergency_cleanup_stops_the_writer_first_and_keeps_the_proof(check: ModuleType, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    order: list[str] = []

    class Terminator:
        def __init__(self, _cli: Any, cid: str) -> None:
            self.cid = cid

        def terminate(self) -> Any:
            order.append(f"stop {self.cid}")
            return type("Done", (), {"evidence": f"gone {self.cid}"})()

    class Cli:
        def labelled_containers(self, label: str) -> list[str]:
            return ["writer"] if label == check.CONTAINER_LABEL else ["edge"]

        def remove(self, cid: str) -> None:
            order.append(f"remove {cid}")

        def remove_network(self, name: str) -> None:
            order.append(f"network {name}")

    monkeypatch.setattr(check, "ContainerTerminator", Terminator)
    world = check.World(check.Results(), tmp_path, cli=Cli())
    assert check._emergency_cleanup(world) == ["gone writer"]
    assert order == ["stop writer", "remove edge", f"network {check.MODEL_NETWORK}", f"network {check.CORE_NETWORK}"]
