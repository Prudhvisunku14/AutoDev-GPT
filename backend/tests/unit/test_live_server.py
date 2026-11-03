"""scripts/live_server.py: the dashboard server for real builds. Nothing here is real: Docker, the guard, keep-awake, the model
check, the dry launch, the coding engine and the server are all replaced, and the settings never read the repository's .env."""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest

from app.orchestrator.start_policy import SessionBudget
from app.sandbox.fake import FakeSandbox
from tests.helpers import make_settings

SCRIPTS = Path(__file__).resolve().parents[2] / "scripts"


@pytest.fixture(scope="module")
def server_module() -> ModuleType:
    spec = importlib.util.spec_from_file_location("devpilot_live_server", SCRIPTS / "live_server.py")
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules["devpilot_live_server"] = module
    spec.loader.exec_module(module)
    return module


class Launcher:
    def __init__(self, *_: Any, **__: Any) -> None:
        pass

    name = "opencode_server"

    def unavailable_reason(self) -> str | None:
        return None


@pytest.fixture
def server(server_module: ModuleType, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> tuple[ModuleType, list[str]]:
    calls: list[str] = []
    live_demo, live_build = server_module.live_demo, server_module.live_build
    monkeypatch.setattr(live_demo, "preflight", lambda settings, cli, limit: [("preflight", True, "")])
    monkeypatch.setattr(live_build, "machine_checks", lambda: [])
    monkeypatch.setattr(server_module, "_port_free", lambda port: True)
    monkeypatch.setattr(server_module, "keep_awake", lambda: calls.append("keep_awake"))
    monkeypatch.setattr(server_module, "start_guard", lambda state, *, deadline, networks: calls.append(f"guard {deadline:.0f}"))
    monkeypatch.setattr(live_demo, "model_access", lambda: calls.append("model_access") or True)
    monkeypatch.setattr(live_demo, "dry_launch", lambda launcher, settings: calls.append("dry_launch"))
    monkeypatch.setattr(server_module, "OpenCodeLauncher", Launcher)
    monkeypatch.setattr(server_module.DockerSandbox, "from_settings", classmethod(lambda cls, settings: FakeSandbox()))
    return server_module, calls


def _settings(tmp_path: Path) -> Any:
    def make(root: Path, *, approved: bool) -> Any:
        return make_settings(tmp_path, require_spend_approval=True, coding_live_approved=approved, run_budget_usd=0.50, run_wall_clock_seconds=1_200.0)

    return make


def test_without_approval_it_runs_the_preflight_only(server: tuple[ModuleType, list[str]], tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    module, calls = server
    served: list[Any] = []
    assert module.main([], settings_for=_settings(tmp_path), serve=lambda app, s: served.append(app), root=tmp_path / "root") == 0
    assert calls == [] and served == []
    assert "Preflight only" in capsys.readouterr().out


@pytest.mark.parametrize("argv", [["--approve-session", "0.10"], ["--approve-session", "50"], ["--approve-session", "1", "--hours", "0"], ["--approve-session", "1", "--hours", "9"]])
def test_figures_outside_the_bounds_are_refused(server: tuple[ModuleType, list[str]], tmp_path: Path, argv: list[str]) -> None:
    module, calls = server
    assert module.main(argv, settings_for=_settings(tmp_path), serve=lambda app, s: None, root=tmp_path / "root") == 2
    assert calls == []


def test_a_failed_check_refuses_before_anything_starts(server: tuple[ModuleType, list[str]], tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    module, calls = server
    monkeypatch.setattr(module.live_demo, "preflight", lambda settings, cli, limit: [("the OpenAI key is configured", False, "")])
    assert module.main(["--approve-session", "1"], settings_for=_settings(tmp_path), serve=lambda app, s: None, ask=lambda q: "serve live", root=tmp_path / "root") == 1
    assert calls == []


def test_an_unconfirmed_stop_from_an_earlier_session_refuses(server: tuple[ModuleType, list[str]], tmp_path: Path) -> None:
    module, calls = server
    marker = tmp_path / "root" / "guard" / "20261104T000000Z" / "quarantine.json"
    marker.parent.mkdir(parents=True)
    marker.write_text("{}", encoding="utf-8")
    assert module.main(["--approve-session", "1"], settings_for=_settings(tmp_path), serve=lambda app, s: None, ask=lambda q: "serve live", root=tmp_path / "root") == 1
    assert calls == []


def test_anything_but_the_typed_phrase_starts_nothing(server: tuple[ModuleType, list[str]], tmp_path: Path) -> None:
    module, calls = server
    served: list[Any] = []
    assert module.main(["--approve-session", "1"], settings_for=_settings(tmp_path), serve=lambda app, s: served.append(app), ask=lambda q: "yes", root=tmp_path / "root") == 0
    assert calls == [] and served == []


def test_a_key_that_cannot_see_the_model_stops_it(server: tuple[ModuleType, list[str]], tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    module, calls = server
    monkeypatch.setattr(module.live_demo, "model_access", lambda: False)
    served: list[Any] = []
    assert module.main(["--approve-session", "1"], settings_for=_settings(tmp_path), serve=lambda app, s: served.append(app), ask=lambda q: "serve live", root=tmp_path / "root") == 1
    assert served == []


def test_it_serves_with_the_session_budget_and_required_approval(server: tuple[ModuleType, list[str]], tmp_path: Path) -> None:
    module, calls = server
    served: list[Any] = []
    code = module.main(["--approve-session", "1.00", "--hours", "1.5"], settings_for=_settings(tmp_path), serve=lambda app, s: served.append(app), ask=lambda q: " serve live ", root=tmp_path / "root")
    assert code == 0
    assert calls == ["keep_awake", "guard 5400", "model_access", "dry_launch"]  # the guard watches before any model call
    app = served[0]
    assert isinstance(app.state.start_policy, SessionBudget)
    assert app.state.settings.require_spend_approval is True
    assert app.state.coding_selector is not None


def test_the_servers_configuration_satisfies_every_launcher_check_once_live_coding_is_approved(server_module: ModuleType, tmp_path: Path) -> None:
    """Every setting `python -m app` lacks (engine, runtime image, manifest, model, token and retry limits) is supplied by the
    server's constants. A fake key stands in for the real one, which this test never reads. What remains is the person's approval."""
    from app.coding.launcher import config_problems
    from app.core.egress import EgressPolicy

    approved = make_settings(tmp_path, coding_live_approved=True, **server_module.LIMITS)
    assert config_problems(approved, EgressPolicy.from_file(approved.egress_allowlist_file)) == []
    unapproved = make_settings(tmp_path, coding_live_approved=False, **server_module.LIMITS)
    problems = config_problems(unapproved, EgressPolicy.from_file(unapproved.egress_allowlist_file))
    assert len(problems) == 1 and problems[0].startswith("CODING_LIVE_APPROVED is false")  # the one gate only a person opens


def test_the_limits_are_the_proven_terminal_builds(server_module: ModuleType) -> None:
    assert server_module.RUN_LIMIT_USD == 0.50
    assert server_module.LIMITS["run_budget_usd"] == 0.50 and server_module.LIMITS["require_spend_approval"] is True
    assert {k: v for k, v in server_module.LIMITS.items() if k != "require_spend_approval"} == server_module.live_build.LIMITS
