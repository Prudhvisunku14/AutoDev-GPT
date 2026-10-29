from __future__ import annotations

import pytest

from app.core.redaction import MASK, Redactor


def test_known_secret_is_masked_anywhere() -> None:
    r = Redactor(["supersecretvalue123"])
    assert r.redact_text("prefix-supersecretvalue123-suffix") == f"prefix-{MASK}-suffix"


def test_short_known_values_are_not_substring_masked() -> None:
    assert Redactor(["abc"]).redact_text("abcdef") == "abcdef"


def test_longest_known_secret_wins() -> None:
    r = Redactor(["secretvalue", "secretvalue-extended"])
    assert r.redact_text("x secretvalue-extended y") == f"x {MASK} y"


@pytest.mark.parametrize(
    "token",
    [
        "sk-" + "A1b2" * 10,
        "sk-proj-" + "x" * 30,
        "ghp_" + "a" * 36,
        "gho_" + "B" * 36,
        "github_pat_" + "c" * 40,
        "ya29." + "d" * 40,
        "1//" + "e" * 40,
    ],
)
def test_token_patterns_are_masked_without_being_known(token: str) -> None:
    out = Redactor().redact_text(f"value {token} end")
    assert token not in out
    assert MASK in out


def test_bearer_and_basic_credentials_are_masked() -> None:
    out = Redactor().redact_text("Authorization: Bearer abcdefghijkl12345 / Basic dXNlcjpwYXNzd29yZA==")
    assert "abcdefghijkl12345" not in out
    assert "dXNlcjpwYXNzd29yZA" not in out


@pytest.mark.parametrize(
    "text",
    ["api_key=hunter2hunter2", "password: 'hunter2'", 'GITHUB_TOKEN="hunter2"', "client_secret=hunter2&x=1"],
)
def test_sensitive_key_value_pairs_are_masked(text: str) -> None:
    out = Redactor().redact_text(text)
    assert "hunter2" not in out
    assert MASK in out


@pytest.mark.parametrize(
    "text",
    [
        "Build a todo app with tokens_in=5 and max_tokens 100. The password reset page needs tests.",
        "A calculator that performs basic arithmetic on two numbers.",
        "The bearer instrument is described in basic terms.",
    ],
)
def test_ordinary_text_is_untouched(text: str) -> None:
    assert Redactor().redact_text(text) == text


def test_nested_structures_are_redacted() -> None:
    r = Redactor(["knownsecret999"])
    data = {
        "api_key": "anything",
        "nested": {"password": "pw", "note": "uses knownsecret999"},
        "items": ["ghp_" + "z" * 36, 42, None],
        "tuple": ("knownsecret999",),
        "token": None,
        "tokens_in": 12,
    }
    out = r.redact(data)
    assert out["api_key"] == MASK
    assert out["nested"]["password"] == MASK
    assert out["nested"]["note"] == f"uses {MASK}"
    assert out["items"] == [MASK, 42, None]
    assert out["tuple"] == (MASK,)
    assert out["token"] is None  # nothing to hide
    assert out["tokens_in"] == 12


def test_input_is_not_mutated() -> None:
    data = {"password": "pw", "list": ["sk-" + "q" * 30]}
    Redactor().redact(data)
    assert data == {"password": "pw", "list": ["sk-" + "q" * 30]}
