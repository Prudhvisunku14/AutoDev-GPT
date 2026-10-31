"""The paid demonstration's script. Nothing here starts a container or contacts anything external."""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest

from app.coding import guard_process
from app.coding.types import Termination
from app.config import Settings

SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "live_demo.py"


@pytest.fixture(scope="module")
def demo() -> ModuleType:
    spec = importlib.util.spec_from_file_location("devpilot_live_demo", SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules["devpilot_live_demo"] = module
    spec.loader.exec_module(module)
    return module


@pytest.fixture(autouse=True)
def no_real_environment_file(demo: ModuleType, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """`live_settings` reads the repository's `.env`, which holds the real key, and `main` writes to the demo's own folder. No test may do either."""
    monkeypatch.setattr(demo, "REPO_ROOT", tmp_path)
    monkeypatch.setattr(demo, "DEMO_ROOT", tmp_path)


def test_these_tests_never_load_the_real_key(demo: ModuleType, tmp_path: Path) -> None:
    assert demo.REPO_ROOT == tmp_path and demo.live_settings(tmp_path, approved=False).openai_api_key is None


def test_the_limits_are_the_approved_ones_and_changing_one_fails_here(demo: ModuleType) -> None:
    assert demo.LIMITS == {
        "openai_model": "gpt-6.1-sol", "opencode_model": "openai/gpt-6.1-sol", "openai_price_input_per_mtok": 2.0, "openai_price_output_per_mtok": 10.0,
        "openai_max_output_tokens": 4_000, "openai_max_retries": 0, "run_budget_usd": 1.50, "run_max_tokens": 300_000, "max_turns": 14, "max_tool_calls": 14,
        "run_wall_clock_seconds": 1_200.0, "opencode_max_tokens_per_task": 100_000, "opencode_max_attempts_per_task": 2, "opencode_timeout_seconds": 300.0, "max_repair_attempts": 1,
        "gateway_max_requests_per_task": 15, "gateway_max_output_tokens": 8_000, "gateway_max_usd_per_task": 0.75, "opencode_enabled": True,
        "opencode_runtime_image": demo.RUNTIME_IMAGE, "opencode_manifest_file": demo.MANIFEST,
    }  # fmt: skip


def test_the_request_is_the_approved_sentence(demo: ModuleType) -> None:
    assert demo.REQUEST == "Create a Python calculator application supporting addition, subtraction, multiplication and division, with unit tests."


def test_the_limits_are_valid_settings(demo: ModuleType, tmp_path: Path) -> None:
    settings = Settings(_env_file=None, devpilot_api_token="t" * 32, coding_live_approved=False, **demo.LIMITS)  # type: ignore[call-arg]
    assert settings.cost_known and settings.run_budget_usd == demo.SPEND_LIMIT_USD and settings.coding_live_approved is False


def test_a_preflight_keeps_live_coding_unapproved(demo: ModuleType, tmp_path: Path) -> None:
    assert demo.live_settings(tmp_path, approved=False).coding_live_approved is False
    assert demo.live_settings(tmp_path, approved=True).coding_live_approved is True


def test_the_limits_win_over_whatever_the_environment_file_says(demo: ModuleType, tmp_path: Path) -> None:
    (tmp_path / ".env").write_text("RUN_BUDGET_USD=99\nOPENAI_MAX_RETRIES=5\nGATEWAY_MAX_REQUESTS_PER_TASK=400\nOPENCODE_MAX_TOKENS_PER_TASK=9000000\nOPENAI_MODEL=gpt-other\n", encoding="utf-8")
    settings = demo.live_settings(tmp_path, approved=False)
    assert settings.run_budget_usd == 1.50 and settings.openai_max_retries == 0 and settings.gateway_max_requests_per_task == 15
    assert settings.opencode_max_tokens_per_task == 100_000 and settings.openai_model == "gpt-6.1-sol"


@pytest.mark.parametrize("amount", ["1.51", "1.49", "100", "0"])
def test_only_the_approved_figure_is_accepted(demo: ModuleType, amount: str, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    monkeypatch.setattr(demo, "preflight", lambda *_: pytest.fail("nothing may run after a refused figure"))
    assert demo.main(["--approve-spend", amount]) == 2
    assert "Refused" in capsys.readouterr().out


class _Guarded:
    """A preflight with the given result, and alarms on everything that would cost money or start something."""

    def __init__(self, demo: ModuleType, monkeypatch: pytest.MonkeyPatch, tmp_path: Path, *, passed: bool) -> None:
        self.started: list[str] = []
        monkeypatch.setattr(demo, "DEMO_ROOT", tmp_path)
        monkeypatch.setattr(demo, "preflight", lambda *_: [("a check", passed, "")])
        monkeypatch.setattr(demo, "keep_awake", lambda: self.started.append("awake"))
        monkeypatch.setattr(demo, "start_guard", lambda *_, **__: self.started.append("guard"))
        monkeypatch.setattr(demo, "model_access", lambda: self.started.append("model_access") or True)
        monkeypatch.setattr(demo, "dry_launch", lambda *_: self.started.append("dry_launch"))
        monkeypatch.setattr(demo, "run_demo", lambda *_, **__: self.started.append("run_demo") or {"state": "COMPLETED", "left_behind": {"containers": [], "networks": []}})


def test_with_no_approval_nothing_external_or_paid_happens(demo: ModuleType, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    guarded = _Guarded(demo, monkeypatch, tmp_path, passed=True)
    assert demo.main([]) == 0
    assert guarded.started == [] and list(tmp_path.iterdir()) == []


def test_a_failed_preflight_stops_a_paid_run_before_anything_starts(demo: ModuleType, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    guarded = _Guarded(demo, monkeypatch, tmp_path, passed=False)
    assert demo.main(["--approve-spend", "1.50"]) == 1
    assert guarded.started == []


def test_the_paid_path_runs_in_the_safe_order(demo: ModuleType, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    guarded = _Guarded(demo, monkeypatch, tmp_path, passed=True)
    monkeypatch.setattr(demo.OpenCodeLauncher, "unavailable_reason", lambda _: None)
    assert demo.main(["--approve-spend", "1.50"]) == 0
    assert guarded.started == ["awake", "guard", "model_access", "dry_launch", "run_demo"]


def test_a_key_that_cannot_see_the_model_stops_before_any_cost(demo: ModuleType, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    guarded = _Guarded(demo, monkeypatch, tmp_path, passed=True)
    monkeypatch.setattr(demo, "model_access", lambda: False)
    assert demo.main(["--approve-spend", "1.50"]) == 1
    assert "run_demo" not in guarded.started and "dry_launch" not in guarded.started


def test_a_failed_dry_launch_stops_before_any_model_call(demo: ModuleType, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    guarded = _Guarded(demo, monkeypatch, tmp_path, passed=True)
    monkeypatch.setattr(demo.OpenCodeLauncher, "unavailable_reason", lambda _: None)
    monkeypatch.setattr(demo, "dry_launch", lambda *_: "the port is taken")
    assert demo.main(["--approve-spend", "1.50"]) == 1
    assert "run_demo" not in guarded.started


def test_a_run_that_leaves_something_behind_is_not_a_success(demo: ModuleType, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    _Guarded(demo, monkeypatch, tmp_path, passed=True)
    monkeypatch.setattr(demo.OpenCodeLauncher, "unavailable_reason", lambda _: None)
    monkeypatch.setattr(demo, "run_demo", lambda *_, **__: {"state": "COMPLETED", "left_behind": {"containers": ["x"], "networks": []}})
    assert demo.main(["--approve-spend", "1.50"]) == 1


def test_the_power_check_answers_on_this_platform() -> None:
    assert guard_process.on_mains_power() in (True, False, None)


def test_the_script_holds_no_key_and_prints_none(demo: ModuleType) -> None:
    source = SCRIPT.read_text(encoding="utf-8")
    assert "openai_api_key.get_secret_value" not in source and "sk-" not in source


def test_keep_awake_changes_no_setting(demo: ModuleType, monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[Any] = []

    class Kernel:
        def SetThreadExecutionState(self, flags: int) -> int:  # noqa: N802
            calls.append(flags)
            return 1

    import ctypes

    if sys.platform == "win32":
        monkeypatch.setattr(ctypes, "WinDLL", lambda *_, **__: Kernel())
        guard_process.keep_awake()
        assert calls == [0x80000003]


class _Launcher:
    def __init__(self, *, refused: bool = False, confirmed: bool = True) -> None:
        self.refused = refused
        self.confirmed = confirmed
        self.closed = False

    def start(self, request: Any) -> Any:
        from app.coding.launcher import LaunchRefused

        if self.refused:
            raise LaunchRefused("the port is taken")
        launcher = self

        class Writer:
            def terminate(self) -> Termination:
                return Termination(launcher.confirmed, "after: NOT_FOUND" if launcher.confirmed else "docker did not answer")

        class Session:
            writer = Writer()

            def close(self) -> None:
                launcher.closed = True

        return Session()


def test_a_dry_launch_starts_and_stops_and_leaves_no_folder(demo: ModuleType, tmp_path: Path) -> None:
    launcher, settings = _Launcher(), type("S", (), {"coding_work_dir": tmp_path})
    assert demo.dry_launch(launcher, settings) is None
    assert launcher.closed and not (tmp_path / "dry-launch").exists()


def test_a_refused_dry_launch_says_why(demo: ModuleType, tmp_path: Path) -> None:
    assert demo.dry_launch(_Launcher(refused=True), type("S", (), {"coding_work_dir": tmp_path})) == "the port is taken"
    assert not (tmp_path / "dry-launch").exists()


def test_a_dry_launch_that_cannot_be_confirmed_stopped_stops_the_paid_run(demo: ModuleType, tmp_path: Path) -> None:
    assert "could not be confirmed" in demo.dry_launch(_Launcher(confirmed=False), type("S", (), {"coding_work_dir": tmp_path}))
