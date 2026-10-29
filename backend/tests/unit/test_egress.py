from __future__ import annotations

from pathlib import Path

import pytest

from app.core.egress import EgressConfigError, EgressPolicy
from tests.helpers import ALLOWLIST_FILE


@pytest.fixture
def policy() -> EgressPolicy:
    return EgressPolicy(
        {
            "backend": ["api.openai.com"],
            "github": ["github.com", "api.github.com"],
            "n8n": ["127.0.0.1:5678"],
            "sandbox_run": [],
        }
    )


@pytest.mark.parametrize(
    ("component", "url"),
    [
        ("backend", "https://api.openai.com/v1/responses"),
        ("backend", "https://API.OpenAI.com./v1/responses"),  # case and trailing dot
        ("github", "https://api.github.com/repos/o/r"),
        ("n8n", "http://127.0.0.1:5678/webhook/x"),
    ],
)
def test_allowlisted_destinations_pass(policy: EgressPolicy, component: str, url: str) -> None:
    decision = policy.check(component, url)
    assert decision.allowed, decision.reason


@pytest.mark.parametrize(
    ("component", "url", "reason"),
    [
        ("backend", "https://evil.example.com/", "not in the 'backend' allowlist"),
        ("backend", "https://api.openai.com.evil.com/", "not in the 'backend' allowlist"),
        ("backend", "https://api.github.com/", "not in the 'backend' allowlist"),
        ("backend", "https://api.openai.com:8443/", "not in the 'backend' allowlist"),
        ("backend", "http://api.openai.com/", "api.openai.com:80 is not in the 'backend' allowlist"),
        ("backend", "ftp://api.openai.com/", "scheme"),
        ("backend", "file:///etc/passwd", "scheme"),
        ("backend", "https://user:pw@api.openai.com/", "credentials"),
        ("backend", "https:///nohost", "no host"),
        ("backend", "https://api.openai.com:99999/", "malformed"),
        ("n8n", "http://127.0.0.1:8000/api/v1/projects", "not in the 'n8n' allowlist"),
        ("n8n", "http://localhost:5678/", "not in the 'n8n' allowlist"),
        ("sandbox_run", "https://pypi.org/", "not in the 'sandbox_run' allowlist"),
        ("unknown", "https://api.openai.com/", "unknown component"),
    ],
)
def test_everything_else_is_denied(policy: EgressPolicy, component: str, url: str, reason: str) -> None:
    decision = policy.check(component, url)
    assert not decision.allowed
    assert reason in decision.reason


@pytest.mark.parametrize(
    "url",
    [
        "https://10.0.0.5/",
        "https://192.168.1.1/",
        "https://169.254.169.254/latest/meta-data/",  # cloud metadata
        "https://127.0.0.1/",
        "https://[::1]/",
        "https://[fd00::1]/",
        "https://0.0.0.0/",
    ],
)
def test_private_and_metadata_addresses_are_denied(policy: EgressPolicy, url: str) -> None:
    assert not policy.check("backend", url).allowed


def test_public_ip_literal_can_be_listed_for_https_only() -> None:
    policy = EgressPolicy({"c": ["8.8.8.8"]})
    assert policy.check("c", "https://8.8.8.8/").allowed
    assert not policy.check("c", "http://8.8.8.8/").allowed


def test_plain_http_is_refused_even_on_an_explicitly_listed_public_port() -> None:
    decision = EgressPolicy({"c": ["example.com:80"]}).check("c", "http://example.com/")
    assert not decision.allowed
    assert "plain http" in decision.reason


@pytest.mark.parametrize(
    ("hosts", "message"),
    [
        (["10.0.0.1"], "private address"),
        (["169.254.169.254"], "private address"),
        (["127.0.0.1"], "explicit port"),
        (["https://api.openai.com"], "must be host or host:port"),
        (["api.openai.com/v1"], "must be host or host:port"),
        (["api.openai.com:notaport"], "bad entry"),
        ([":443"], "empty host"),
    ],
)
def test_invalid_allowlist_entries_are_rejected(hosts: list[str], message: str) -> None:
    with pytest.raises(EgressConfigError, match=message):
        EgressPolicy({"c": hosts})


def test_repo_allowlist_file_loads_with_all_components() -> None:
    policy = EgressPolicy.from_file(ALLOWLIST_FILE)
    assert policy.components == {
        "backend", "opencode", "github", "google_drive", "n8n",
        "sandbox_install", "sandbox_run", "document_parser",
    }
    assert policy.check("google_drive", "https://www.googleapis.com/drive/v3/files/x").allowed
    assert not policy.check("document_parser", "https://pypi.org/").allowed


@pytest.mark.parametrize(
    ("content", "message"),
    [
        ("components: {}", "non-empty 'components'"),
        ("just a string", "non-empty 'components'"),
        ("components:\n  c: api.openai.com\n", "list of strings"),
        ("components:\n  c: [1, 2]\n", "list of strings"),
        ("components: [unclosed", "not valid YAML"),
    ],
)
def test_malformed_allowlist_files_are_rejected(tmp_path: Path, content: str, message: str) -> None:
    path = tmp_path / "allow.yaml"
    path.write_text(content, encoding="utf-8")
    with pytest.raises(EgressConfigError, match=message):
        EgressPolicy.from_file(path)


def test_missing_allowlist_file_is_rejected(tmp_path: Path) -> None:
    with pytest.raises(EgressConfigError, match="not found"):
        EgressPolicy.from_file(tmp_path / "missing.yaml")


def test_null_component_means_no_destinations(tmp_path: Path) -> None:
    path = tmp_path / "allow.yaml"
    path.write_text("components:\n  sandbox_run:\n", encoding="utf-8")
    policy = EgressPolicy.from_file(path)
    assert not policy.check("sandbox_run", "https://pypi.org/").allowed
