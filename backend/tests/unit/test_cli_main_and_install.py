"""The command's entry point, `devpilot setup`, and the files a new user runs: setup.cmd, setup.ps1, devpilot.cmd, .gitignore. Offline."""

from __future__ import annotations

import shutil
import subprocess
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

from app.cli import credentials, runtime
from app.cli import main as cli_main
from app.cli import setup as cli_setup
from app.cli.paths import Paths, paths as make_paths
from app.cli.userconfig import UserConfig, load
from app.coding import opencode_container

ROOT = Path(__file__).resolve().parents[3]
FAKE_KEY = "sk-test" + "S" * 40


@pytest.fixture(autouse=True)
def _forget_local_approvals() -> Iterator[None]:
    yield
    opencode_container._LOCALLY_APPROVED.clear()


class IO:
    def __init__(self, *answers: str) -> None:
        self.answers, self.lines, self.asked = list(answers), [], []

    def ask(self, prompt: str) -> str:
        self.asked.append(prompt)
        return self.answers.pop(0) if self.answers else ""

    def say(self, text: str = "") -> None:
        self.lines.append(text)

    @property
    def text(self) -> str:
        return "\n".join(self.lines)


# --- The command ------------------------------------------------------------------------------------


def test_help_lists_the_commands_and_the_honest_limits(capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit) as stop:
        cli_main.main(["--help"])
    out = capsys.readouterr().out
    assert stop.value.code == 0
    for text in ("build", "doctor", "setup", "devpilot build", "Builds are accepted only for stacks DevPilot can", "static websites (HTML, CSS, JavaScript) once `setup --web`", "React, FastAPI and full-stack builds are declined", "--approve-spend", "verify-site", "inspect"):
        assert text in out
    assert "D:\\" not in out


def test_no_command_prints_the_help_and_succeeds(capsys: pytest.CaptureFixture[str], tmp_path: Path) -> None:
    assert cli_main.main(["--home", str(tmp_path)]) == 0
    assert "usage: devpilot" in capsys.readouterr().out


def test_a_broken_settings_file_is_reported_not_a_traceback(capsys: pytest.CaptureFixture[str], tmp_path: Path) -> None:
    (tmp_path / "config.toml").write_text("not = [toml", encoding="utf-8")
    assert cli_main.main(["--home", str(tmp_path), "doctor"]) == 2
    assert "Settings problem" in capsys.readouterr().out


def test_an_unaffordably_large_cap_is_refused_before_anything_else(capsys: pytest.CaptureFixture[str], tmp_path: Path) -> None:
    assert cli_main.main(["--home", str(tmp_path), "build", "Create a python tool that does something useful", "--max-usd", "50"]) == 2
    assert "at most $2.00" in capsys.readouterr().out


def test_doctor_exits_non_zero_when_something_blocks(monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], tmp_path: Path) -> None:
    from app.cli import doctor

    flags: list[bool] = []
    monkeypatch.setattr(doctor, "run_checks", lambda p, c, check_key=False: flags.append(check_key) or [doctor.Check("Docker running", "fail", "x", "start Docker Desktop")])
    assert cli_main.main(["--home", str(tmp_path), "doctor"]) == 1
    assert "to fix: start Docker Desktop" in capsys.readouterr().out
    monkeypatch.setattr(doctor, "run_checks", lambda p, c, check_key=False: flags.append(check_key) or [doctor.Check("Docker running", "ok")])
    assert cli_main.main(["--home", str(tmp_path), "doctor"]) == 0
    assert cli_main.main(["--home", str(tmp_path), "doctor", "--check-key"]) == 0
    assert flags == [False, False, True]  # OpenAI is contacted only when --check-key is given


def test_the_stored_key_can_be_forgotten(monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], tmp_path: Path) -> None:
    (tmp_path / "credentials.bin").write_bytes(b"x")
    assert cli_main.main(["--home", str(tmp_path), "forget-key"]) == 0 and "deleted" in capsys.readouterr().out
    assert cli_main.main(["--home", str(tmp_path), "forget-key"]) == 0 and "No key" in capsys.readouterr().out


# --- devpilot setup ---------------------------------------------------------------------------------


@pytest.fixture
def p(tmp_path: Path) -> Paths:
    return make_paths(tmp_path / "home")


def stub_provisioning(monkeypatch: pytest.MonkeyPatch, calls: list[str], *, approved: bool = False) -> None:
    monkeypatch.setattr(runtime, "fetch_ripgrep", lambda paths, **k: calls.append("ripgrep"))
    monkeypatch.setattr(runtime, "fetch_opencode", lambda paths, **k: calls.append("opencode"))
    monkeypatch.setattr(runtime, "build_image", lambda paths, **k: calls.append("build"))
    monkeypatch.setattr(runtime, "load_approval", lambda paths, **k: ("sha256:" + "a" * 64, None) if approved else (None, "not audited"))
    monkeypatch.setattr(runtime, "audit_and_approve", lambda paths, **k: calls.append("audit") or runtime.Approval("sha256:" + "a" * 64, runtime.RUNTIME_TAG, "now", "29", {}))

    def docker(args: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess(["docker", *args], 0, "29.9.2\n", "")

    monkeypatch.setattr(runtime, "docker", docker)
    monkeypatch.setattr(cli_setup, "run_checks", lambda paths, config: [])


def test_setup_asks_for_the_key_hidden_the_model_and_the_prices_and_stores_none_in_plain_text(monkeypatch: pytest.MonkeyPatch, p: Paths) -> None:
    calls: list[str] = []
    stub_provisioning(monkeypatch, calls)
    stored: list[str] = []
    monkeypatch.setattr(credentials, "store_key", lambda path, key: stored.append(key))
    hidden: list[str] = []
    io = IO("", "2.5", "11", "")  # model (default), input price, output price, approve the image
    code = cli_setup.setup_command(p, UserConfig(), io=io, ask_secret=lambda prompt: hidden.append(prompt) or FAKE_KEY)  # type: ignore[arg-type]
    assert code == 0 and stored == [FAKE_KEY] and "hidden" in hidden[0]
    assert FAKE_KEY not in io.text  # never echoed
    saved = load(p.config)
    assert saved.model == "gpt-6.1-sol" and saved.price_input_per_mtok == 2.5 and saved.price_output_per_mtok == 11.0
    assert FAKE_KEY not in p.config.read_text(encoding="utf-8")
    assert calls == ["ripgrep", "opencode", "build", "audit"]
    assert any("Approve the image" in q for q in io.asked)


def test_setup_refuses_to_approve_an_image_the_person_declines(monkeypatch: pytest.MonkeyPatch, p: Paths) -> None:
    calls: list[str] = []
    stub_provisioning(monkeypatch, calls)
    monkeypatch.setenv(credentials.ENV_VAR, FAKE_KEY)
    io = IO("", "2", "10", "n")
    assert cli_setup.setup_command(p, UserConfig(), io=io, key_from_env=True) == 1
    assert "was not approved" in io.text and "audit" not in calls


def test_setup_skips_the_build_when_the_image_is_already_audited_unless_asked_to_rebuild(monkeypatch: pytest.MonkeyPatch, p: Paths) -> None:
    calls: list[str] = []
    stub_provisioning(monkeypatch, calls, approved=True)
    monkeypatch.setenv(credentials.ENV_VAR, FAKE_KEY)
    config = UserConfig(model="m", price_input_per_mtok=1.0, price_output_per_mtok=2.0)
    assert cli_setup.setup_command(p, config, io=IO(), interactive=False, key_from_env=True) == 0
    assert "build" not in calls and "audit" not in calls
    calls.clear()
    assert cli_setup.setup_command(p, config, io=IO(), interactive=False, key_from_env=True, rebuild=True, assume_yes=True) == 0
    assert calls == ["ripgrep", "opencode", "build", "audit"]


def test_setup_without_a_terminal_or_key_stops_with_what_to_do(monkeypatch: pytest.MonkeyPatch, p: Paths) -> None:
    stub_provisioning(monkeypatch, [])
    io = IO()
    assert cli_setup.setup_command(p, UserConfig(), io=io, interactive=False) == 1
    assert "OPENAI_API_KEY" in io.text
    monkeypatch.delenv(credentials.ENV_VAR, raising=False)
    io2 = IO()
    assert cli_setup.setup_command(p, UserConfig(), io=io2, interactive=False, key_from_env=True) == 1 and "--key-from-env was given" in io2.text


def test_setup_reports_docker_not_running_in_plain_words(monkeypatch: pytest.MonkeyPatch, p: Paths) -> None:
    calls: list[str] = []
    stub_provisioning(monkeypatch, calls)
    monkeypatch.setattr(runtime, "docker", lambda args, **k: subprocess.CompletedProcess(["docker", *args], 1, "", "cannot connect"))
    monkeypatch.setenv(credentials.ENV_VAR, FAKE_KEY)
    io = IO()
    code = cli_setup.setup_command(p, UserConfig(model="m", price_input_per_mtok=1.0, price_output_per_mtok=2.0), io=io, interactive=False, key_from_env=True)
    assert code == 1 and "start Docker Desktop" in io.text and "build" not in calls


def test_setup_stops_cleanly_when_a_download_fails_verification(monkeypatch: pytest.MonkeyPatch, p: Paths) -> None:
    stub_provisioning(monkeypatch, [])

    def tampered(paths: Paths, **k: Any) -> None:
        raise runtime.ProvisionError("the ripgrep download does not match its pinned SHA-256, so it was discarded")

    monkeypatch.setattr(runtime, "fetch_ripgrep", tampered)
    monkeypatch.setenv(credentials.ENV_VAR, FAKE_KEY)
    io = IO()
    assert cli_setup.setup_command(p, UserConfig(model="m", price_input_per_mtok=1.0, price_output_per_mtok=2.0), io=io, interactive=False, key_from_env=True) == 1
    assert "Setup stopped: the ripgrep download does not match its pinned SHA-256" in io.text


# --- The files a new user runs ----------------------------------------------------------------------


def test_the_windows_wrappers_run_the_setup_script_and_the_environments_python() -> None:
    setup_cmd = (ROOT / "setup.cmd").read_text(encoding="utf-8")
    assert "setup.ps1" in setup_cmd and "-ExecutionPolicy Bypass" in setup_cmd and "%*" in setup_cmd
    devpilot_cmd = (ROOT / "devpilot.cmd").read_text(encoding="utf-8")
    assert ".venv\\Scripts\\python.exe" in devpilot_cmd and "-m app.cli %*" in devpilot_cmd and "Run setup.cmd first" in devpilot_cmd


def test_the_setup_script_checks_the_requirements_and_never_handles_the_key() -> None:
    script = (ROOT / "setup.ps1").read_text(encoding="utf-8")
    for need in ("Python 3.12", "git", "docker", "venv", "requirements.lock", "app.cli setup"):
        assert need in script, need
    code = "\n".join(line for line in script.splitlines() if not line.lstrip().startswith("#")).lower()  # comments may explain; code may not touch the key
    assert "openai_api_key" not in code and "read-host" not in code and "sk-" not in code and "set-content" not in code  # only `devpilot setup` ever asks for it
    assert "D:\\" not in script and "D:/" not in script


@pytest.mark.skipif(shutil.which("powershell") is None, reason="needs Windows PowerShell")
def test_the_setup_script_parses() -> None:
    command = f"$e = $null; [System.Management.Automation.Language.Parser]::ParseFile('{ROOT / 'setup.ps1'}', [ref]$null, [ref]$e) | Out-Null; if ($e) {{ $e | ForEach-Object {{ $_.Message }}; exit 1 }}"
    done = subprocess.run(["powershell", "-NoProfile", "-Command", command], capture_output=True, text=True, timeout=60)
    assert done.returncode == 0, done.stdout + done.stderr


@pytest.mark.skipif(shutil.which("git") is None or not (ROOT / ".git").exists(), reason="needs git and the repository")
@pytest.mark.parametrize(
    ("path", "ignored"),
    [(".env", True), (".env.local", True), ("devpilot-output/app.zip", True), (".venv/Scripts/python.exe", True), ("data/devpilot.db", True), ("approved-runtime.json", True),
     ("credentials.bin", True), ("somekey.pem", True), ("workspaces/x/app.py", True), ("setup.cmd", False), ("setup.ps1", False), ("devpilot.cmd", False),
     (".env.example", False), ("backend/app/cli/Dockerfile.runtime", False), ("backend/app/cli/main.py", False), ("examples/prompts.md", False)],
)  # fmt: skip
def test_secrets_caches_databases_and_outputs_are_ignored_and_the_installer_is_not(path: str, ignored: bool) -> None:
    done = subprocess.run(["git", "check-ignore", "-q", "--no-index", path], cwd=ROOT, capture_output=True, timeout=30)
    assert (done.returncode == 0) is ignored, path
