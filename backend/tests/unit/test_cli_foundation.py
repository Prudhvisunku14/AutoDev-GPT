"""The devpilot CLI's foundations: portable paths, the user's settings, and the key store. Offline; the key here is fake."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

from app.cli import credentials
from app.cli import paths as paths_module
from app.cli.paths import HOME_ENV, default_home, paths
from app.cli.userconfig import DEFAULT_CAP_USD, MAX_CAP_USD, ConfigError, UserConfig, load, save, validate

FAKE_KEY = "sk-test" + "Q" * 40


def test_the_home_is_the_users_own_not_a_machine_constant(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.delenv(HOME_ENV, raising=False)
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "Local"))
    assert default_home() == tmp_path / "Local" / "DevPilot"
    monkeypatch.setenv(HOME_ENV, str(tmp_path / "custom"))
    assert default_home() == (tmp_path / "custom").resolve()
    monkeypatch.delenv(HOME_ENV)
    monkeypatch.delenv("LOCALAPPDATA")
    assert default_home().name == "devpilot"


def test_every_path_is_under_the_home_and_none_is_a_hardcoded_drive(tmp_path: Path) -> None:
    p = paths(tmp_path / "home")
    p.ensure()
    for attribute in ("config", "credentials", "approved_runtime", "tools", "opencode_cache", "ripgrep_cache", "database", "workspaces", "checkpoints", "work", "guard", "ledger"):
        assert getattr(p, attribute).is_relative_to(p.home), attribute
    assert p.work.is_dir() and p.database.parent.is_dir()
    source = Path(paths_module.__file__ or "").read_text(encoding="utf-8")
    assert "D:\\" not in source and "D:/" not in source


def test_settings_round_trip_and_unknown_settings_are_refused(tmp_path: Path) -> None:
    file = tmp_path / "config.toml"
    config = UserConfig(model="gpt-test", price_input_per_mtok=2.5, price_output_per_mtok=10.0, run_cap_usd=0.75, output_dir=str(tmp_path / 'out "x"'))
    save(file, config)
    assert load(file) == config
    assert load(tmp_path / "missing.toml") == UserConfig()
    file.write_text('model = "x"\nsurprise = 1\n', encoding="utf-8")
    with pytest.raises(ConfigError, match="unknown settings: surprise"):
        load(file)
    file.write_text("not toml [", encoding="utf-8")
    with pytest.raises(ConfigError, match="cannot be read"):
        load(file)


def test_the_spending_cap_and_prices_are_validated() -> None:
    assert UserConfig().run_cap_usd == DEFAULT_CAP_USD == 0.50
    for bad in (0, -1, MAX_CAP_USD + 0.01):
        with pytest.raises(ConfigError):
            validate(UserConfig(run_cap_usd=bad))
    with pytest.raises(ConfigError):
        validate(UserConfig(price_input_per_mtok=0))
    with pytest.raises(ConfigError):
        validate(UserConfig(model="openai/gpt-x"))  # the provider prefix is added by DevPilot
    assert not UserConfig(price_input_per_mtok=1.0).prices_set and UserConfig(price_input_per_mtok=1.0, price_output_per_mtok=2.0).prices_set


def test_the_environment_key_wins_and_is_never_in_the_source_description(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    file = tmp_path / "credentials.bin"
    assert credentials.load_key(file) is None and credentials.source(file) is None
    monkeypatch.setenv(credentials.ENV_VAR, f"  {FAKE_KEY}  ")
    assert credentials.load_key(file) == FAKE_KEY
    assert FAKE_KEY not in (credentials.source(file) or "")


def test_a_malformed_key_is_refused_before_anything_is_stored(tmp_path: Path) -> None:
    assert credentials.plausible(FAKE_KEY) and not credentials.plausible("hello") and not credentials.plausible("sk-short")
    with pytest.raises(credentials.CredentialError, match="does not look like"):
        credentials.store_key(tmp_path / "c.bin", "not-a-key")
    assert not (tmp_path / "c.bin").exists()


@pytest.mark.skipif(sys.platform != "win32", reason="the encrypted store is Windows DPAPI")
def test_the_stored_key_is_encrypted_on_disk_and_comes_back(tmp_path: Path) -> None:
    file = tmp_path / "credentials.bin"
    credentials.store_key(file, FAKE_KEY)
    raw = file.read_bytes()
    assert FAKE_KEY.encode() not in raw and b"sk-test" not in raw  # not plain text, not even a recognisable prefix
    assert credentials.stored_key(file) == FAKE_KEY
    assert credentials.source(file) == "the encrypted store"
    file.write_bytes(raw[:-3] + b"xyz")  # damaged: unreadable, never a wrong key
    assert credentials.stored_key(file) is None
    assert credentials.forget(file) and not credentials.forget(file)


def test_the_key_store_is_unavailable_off_windows_and_says_to_use_the_environment(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setattr(credentials.sys, "platform", "linux")
    with pytest.raises(credentials.CredentialError, match="OPENAI_API_KEY"):
        credentials.store_key(tmp_path / "c.bin", FAKE_KEY)
    assert credentials.stored_key(tmp_path / "c.bin") is None
