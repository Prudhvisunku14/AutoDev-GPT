"""`devpilot doctor`: every requirement checked locally, with a fix for each failure. Docker is a fake; the key is fake and never printed."""

from __future__ import annotations

import json
import subprocess
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

from app.cli import credentials, doctor, runtime
from app.cli.paths import Paths, paths as make_paths
from app.cli.userconfig import UserConfig
from app.coding import opencode_container

TAG_ID = "sha256:" + "c" * 64
FAKE_KEY = "sk-test" + "K" * 40
CONFIG = UserConfig(model="gpt-test", price_input_per_mtok=2.0, price_output_per_mtok=10.0)


@pytest.fixture(autouse=True)
def _forget_local_approvals() -> Iterator[None]:
    yield
    opencode_container._LOCALLY_APPROVED.clear()


@pytest.fixture
def p(tmp_path: Path) -> Paths:
    where = make_paths(tmp_path / "home")
    where.ensure()
    return where


class Docker:
    def __init__(self, *, daemon: bool = True, sandbox: bool = True, tag_id: str | None = TAG_ID, containers: str = "", networks: str = "") -> None:
        self.daemon, self.sandbox, self.tag_id, self.containers, self.networks = daemon, sandbox, tag_id, containers, networks

    def __call__(self, command: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        args = command[1:]
        done = lambda out="", code=0: subprocess.CompletedProcess(command, code, out, "")  # noqa: E731
        if args[0] == "version":
            return done("29.9.2\n") if self.daemon else done("", 1)
        if args[:2] == ["image", "inspect"]:
            if args[2] == "python:3.12-alpine":
                return done("sha256:" + "d" * 64 + "\n") if self.sandbox else done("", 1)
            return done(f"{self.tag_id}\n") if self.tag_id else done("", 1)
        if args[0] == "ps":
            return done(self.containers)
        if args[:2] == ["network", "ls"]:
            return done(self.networks)
        return done()


def approve(p: Paths, image_id: str = TAG_ID) -> None:
    p.approved_runtime.write_text(json.dumps({"image_id": image_id, "tag": runtime.RUNTIME_TAG}), encoding="utf-8")


def healthy(p: Paths, monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    approve(p)
    monkeypatch.setenv(credentials.ENV_VAR, FAKE_KEY)
    monkeypatch.setattr(runtime, "opencode_ok", lambda paths: None)
    return {"run": Docker(), "which": lambda name: f"C:\\tools\\{name}.exe", "port_free": lambda port: True, "free_gb": lambda path: 100.0, "on_mains": lambda: True}


def by_name(checks: list[doctor.Check]) -> dict[str, doctor.Check]:
    return {c.name: c for c in checks}


def test_a_complete_installation_passes_every_check(p: Paths, monkeypatch: pytest.MonkeyPatch) -> None:
    checks = doctor.run_checks(p, CONFIG, **healthy(p, monkeypatch))
    assert [c for c in checks if c.status != "ok"] == [], doctor.render(checks)
    assert doctor.blockers(checks) == [] and doctor.render(checks).endswith("Ready to build.")


def test_without_docker_the_doctor_says_to_install_it_and_does_not_guess_about_images(p: Paths, monkeypatch: pytest.MonkeyPatch) -> None:
    kwargs = healthy(p, monkeypatch) | {"which": lambda name: None if name == "docker" else "x"}
    found = by_name(doctor.run_checks(p, CONFIG, **kwargs))
    assert found["Docker installed"].status == "fail" and "Docker Desktop" in found["Docker installed"].fix
    assert found["OpenCode runtime image audited and approved"].status == "fail"


def test_a_docker_that_is_installed_but_not_running_is_a_failure_with_the_fix(p: Paths, monkeypatch: pytest.MonkeyPatch) -> None:
    kwargs = healthy(p, monkeypatch) | {"run": Docker(daemon=False)}
    found = by_name(doctor.run_checks(p, CONFIG, **kwargs))
    assert found["Docker running"].status == "fail" and "start Docker Desktop" in found["Docker running"].fix


def test_a_missing_sandbox_image_and_an_unapproved_or_replaced_runtime_image_fail(p: Paths, monkeypatch: pytest.MonkeyPatch) -> None:
    kwargs = healthy(p, monkeypatch) | {"run": Docker(sandbox=False, tag_id="sha256:" + "e" * 64)}
    found = by_name(doctor.run_checks(p, CONFIG, **kwargs))
    assert found["Sandbox image python:3.12-alpine"].status == "fail"
    image = found["OpenCode runtime image audited and approved"]
    assert image.status == "fail" and "rebuilt or replaced" in image.detail and "setup.cmd" in image.fix


def test_a_missing_key_model_or_prices_each_fail_and_point_at_setup(p: Paths, monkeypatch: pytest.MonkeyPatch) -> None:
    kwargs = healthy(p, monkeypatch)
    monkeypatch.delenv(credentials.ENV_VAR)
    found = by_name(doctor.run_checks(p, UserConfig(), **kwargs))
    assert found["OpenAI API key"].status == "fail" and "OPENAI_API_KEY" in found["OpenAI API key"].fix
    assert found["Model and prices"].status == "fail" and found["Coding engine configuration"].status == "fail"


def test_the_keys_value_is_never_printed_even_when_it_is_malformed(p: Paths, monkeypatch: pytest.MonkeyPatch) -> None:
    kwargs = healthy(p, monkeypatch)
    monkeypatch.setenv(credentials.ENV_VAR, "hunter2-not-a-key-but-secret")
    text = doctor.render(doctor.run_checks(p, CONFIG, **kwargs))
    assert "hunter2" not in text and "does not look like an OpenAI key" in text
    monkeypatch.setenv(credentials.ENV_VAR, FAKE_KEY)
    assert FAKE_KEY not in doctor.render(doctor.run_checks(p, CONFIG, **kwargs))


def test_an_unverified_opencode_binary_fails(p: Paths, monkeypatch: pytest.MonkeyPatch) -> None:
    kwargs = healthy(p, monkeypatch)
    monkeypatch.setattr(runtime, "opencode_ok", lambda paths: "the binary's hash does not match its manifest")
    found = by_name(doctor.run_checks(p, CONFIG, **kwargs))
    assert found["OpenCode binary verified"].status == "fail" and "hash does not match" in found["OpenCode binary verified"].detail


def test_leftovers_and_a_battery_are_warnings_but_an_unconfirmed_stop_a_full_disk_and_a_busy_port_block(p: Paths, monkeypatch: pytest.MonkeyPatch) -> None:
    kwargs = healthy(p, monkeypatch) | {"run": Docker(containers="abc123\n", networks="devpilot-model\nbridge\n"), "on_mains": lambda: False}
    found = by_name(doctor.run_checks(p, CONFIG, **kwargs))
    assert found["No leftover DevPilot containers or networks"].status == "warn" and found["Mains power"].status == "warn"
    assert doctor.blockers(list(found.values())) == []

    marker = p.guard / "20261104T000000Z" / "quarantine.json"
    marker.parent.mkdir(parents=True)
    marker.write_text("{}", encoding="utf-8")
    blocked = by_name(doctor.run_checks(p, CONFIG, **(kwargs | {"free_gb": lambda path: 1.0, "port_free": lambda port: False})))
    assert {n for n, c in blocked.items() if c.status == "fail"} == {"No unconfirmed container stop", "Disk space", "Port 4096 free"}


def test_a_network_policy_that_no_longer_allows_openai_fails(p: Paths, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    kwargs = healthy(p, monkeypatch)
    from app.cli import engine

    real = engine.build_settings

    def narrowed(*args: Any, **kw: Any) -> Any:
        allowlist = tmp_path / "egress.yaml"
        allowlist.write_text("components:\n  backend:\n    - example.org\n", encoding="utf-8")
        return real(*args, **kw).model_copy(update={"egress_allowlist_file": allowlist})

    monkeypatch.setattr(doctor, "build_settings", narrowed)
    found = by_name(doctor.run_checks(p, CONFIG, **kwargs))
    assert found["Network policy (OpenAI and the local filter only)"].status == "fail"


def test_every_failure_in_the_report_is_followed_by_how_to_fix_it(p: Paths, monkeypatch: pytest.MonkeyPatch) -> None:
    kwargs = healthy(p, monkeypatch) | {"which": lambda name: None, "run": Docker(daemon=False)}
    monkeypatch.delenv(credentials.ENV_VAR)
    checks = doctor.run_checks(p, UserConfig(), **kwargs)
    assert doctor.blockers(checks) and all(c.fix for c in doctor.blockers(checks))
    text = doctor.render(checks)
    assert text.count("to fix:") >= len(doctor.blockers(checks)) and "FAILED: a build cannot start" in text
