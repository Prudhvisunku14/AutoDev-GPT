from __future__ import annotations

from pathlib import Path

import pytest
from pydantic import ValidationError

from app.config import REPO_ROOT, Settings
from tests.helpers import API_TOKEN, FAKE_OPENAI_KEY, make_settings


def test_relative_paths_resolve_against_repo_root(tmp_path: Path) -> None:
    s = make_settings(tmp_path, workspaces_dir=Path("ws"), egress_allowlist_file=Path("config/egress_allowlist.yaml"))
    assert s.workspaces_dir == (REPO_ROOT / "ws").resolve()
    assert s.egress_allowlist_file == (REPO_ROOT / "config" / "egress_allowlist.yaml").resolve()


def test_relative_sqlite_path_resolves_against_repo_root(tmp_path: Path) -> None:
    s = make_settings(tmp_path, database_url="sqlite:///./data/devpilot.db")
    assert s.database_path == (REPO_ROOT / "data" / "devpilot.db").resolve()
    assert s.resolved_database_url.endswith("/data/devpilot.db")


def test_in_memory_sqlite(tmp_path: Path) -> None:
    s = make_settings(tmp_path, database_url="sqlite:///:memory:")
    assert s.database_path is None
    assert s.resolved_database_url == "sqlite://"


def test_api_token_is_required() -> None:
    with pytest.raises(ValidationError, match="devpilot_api_token"):
        Settings(_env_file=None)  # type: ignore[call-arg]


def test_short_api_token_is_rejected(tmp_path: Path) -> None:
    with pytest.raises(ValidationError, match="at least 32 characters"):
        make_settings(tmp_path, devpilot_api_token="short")


@pytest.mark.parametrize(
    "overrides",
    [
        {"devpilot_api_token": "fakeShortTok123"},  # too short: rejected, must not be echoed
        {"devpilot_api_token": API_TOKEN, "devpilot_port": "fakeShortTok123"},
        {"devpilot_api_token": API_TOKEN, "openai_price_input_per_mtok": "fakeShortTok123"},
    ],
)
def test_validation_errors_never_echo_values(tmp_path: Path, overrides: dict[str, object]) -> None:
    with pytest.raises(ValidationError) as info:
        make_settings(tmp_path, **overrides)
    assert "fakeShortTok123" not in str(info.value)
    assert FAKE_OPENAI_KEY not in str(info.value)


@pytest.mark.parametrize("host", ["0.0.0.0", "192.168.1.10", "example.com", "::"])
def test_non_loopback_host_is_rejected(tmp_path: Path, host: str) -> None:
    with pytest.raises(ValidationError, match="loopback"):
        make_settings(tmp_path, devpilot_host=host)


@pytest.mark.parametrize("host", ["127.0.0.1", "::1", "localhost"])
def test_loopback_hosts_are_accepted(tmp_path: Path, host: str) -> None:
    assert make_settings(tmp_path, devpilot_host=host).devpilot_host == host


def test_non_sqlite_database_is_rejected(tmp_path: Path) -> None:
    with pytest.raises(ValidationError, match="sqlite"):
        make_settings(tmp_path, database_url="postgresql://u:p@localhost/db")


def test_port_range_is_validated(tmp_path: Path) -> None:
    with pytest.raises(ValidationError):
        make_settings(tmp_path, devpilot_port=70000)


def test_log_level_is_normalized_and_validated(tmp_path: Path) -> None:
    assert make_settings(tmp_path, log_level="debug").log_level == "DEBUG"
    with pytest.raises(ValidationError):
        make_settings(tmp_path, log_level="verbose")


def test_empty_secrets_mean_unset(tmp_path: Path) -> None:
    s = make_settings(tmp_path, openai_api_key="", github_token="  ")
    assert s.openai_api_key is None
    assert s.github_token is None


def test_secret_values_lists_configured_secrets_only(tmp_path: Path) -> None:
    s = make_settings(tmp_path, github_token="")
    assert set(s.secret_values()) == {API_TOKEN, FAKE_OPENAI_KEY}


def test_secrets_are_hidden_in_repr(tmp_path: Path) -> None:
    s = make_settings(tmp_path)
    assert API_TOKEN not in repr(s)
    assert FAKE_OPENAI_KEY not in repr(s)


def test_cors_origins_are_split_on_commas(tmp_path: Path) -> None:
    s = make_settings(tmp_path, devpilot_cors_origins="http://127.0.0.1:5173, http://localhost:5173,")
    assert s.cors_origins == ["http://127.0.0.1:5173", "http://localhost:5173"]


def test_env_file_values_are_loaded_and_unknown_keys_ignored(tmp_path: Path) -> None:
    env_file = tmp_path / ".env"
    env_file.write_text(f"DEVPILOT_API_TOKEN={API_TOKEN}\nLOG_LEVEL=warning\nUNKNOWN_KEY=ignored\n", encoding="utf-8")
    s = Settings(_env_file=env_file)  # type: ignore[call-arg]
    assert s.devpilot_api_token.get_secret_value() == API_TOKEN
    assert s.log_level == "WARNING"


def test_env_example_matches_the_settings_model(monkeypatch: pytest.MonkeyPatch) -> None:
    # Guards against .env.example drifting from Settings. The token is the only required value.
    monkeypatch.setenv("DEVPILOT_API_TOKEN", API_TOKEN)
    s = Settings(_env_file=REPO_ROOT / ".env.example")  # type: ignore[call-arg]
    assert s.openai_api_key is None
    assert s.github_token is None
    assert s.devpilot_host == "127.0.0.1"
    assert s.devpilot_port == 8000
    assert s.egress_allowlist_file.is_file()
