"""The free model check: every kind of failure is told apart, with the real HTTP status and a sanitised error code, and the key never shows.

The real OpenAI SDK runs against an in-memory `httpx.MockTransport`. Nothing here touches the network, a stored key or `.env`.
"""

from __future__ import annotations

import json
import subprocess
from collections.abc import Callable
from pathlib import Path
from typing import Any

import httpx
import openai
import pytest

from app.cli import credentials, doctor, runtime
from app.cli import setup as cli_setup
from app.cli.build import approval_text
from app.cli.engine import describe_access_failure, model_access
from app.cli.paths import paths as make_paths
from app.cli.userconfig import ConfigError, UserConfig, load, save, validate


class IO:
    """A scripted terminal: answers in order, and everything said."""

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


FAKE_KEY = "sk-proj-" + "Z9" * 30  # fake, and long enough that it is no "plain identifier" under 64 characters
MODEL = "gpt-6.1-sol"
Handler = Callable[[httpx.Request], httpx.Response]


def run(handler: Handler, key: str = FAKE_KEY, **kwargs: Any) -> str | None:
    def factory(**options: Any) -> openai.OpenAI:
        return openai.OpenAI(**options, http_client=httpx.Client(transport=httpx.MockTransport(handler)))

    return model_access(key, MODEL, client_factory=factory, **kwargs)


def error(status: int, *, code: Any = None, kind: Any = None, message: str = "x", headers: dict[str, str] | None = None) -> Handler:
    body = {"error": {"message": message, "type": kind, "code": code, "param": None}}
    return lambda request: httpx.Response(status, json=body, headers=headers or {})


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in ("OPENAI_BASE_URL", "OPENAI_ORG_ID", "OPENAI_PROJECT_ID", "OPENAI_API_KEY"):
        monkeypatch.delenv(name, raising=False)


def test_a_key_that_can_see_the_model_passes_and_only_the_model_endpoint_is_asked() -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json={"id": MODEL, "object": "model", "created": 0, "owned_by": "openai"})

    assert run(handler) is None
    assert [(r.method, str(r.url)) for r in seen] == [("GET", f"https://api.openai.com/v1/models/{MODEL}")]  # free: no generation request


def test_the_key_goes_only_to_openai_whatever_the_environment_says(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("OPENAI_BASE_URL", "https://evil.example/v1")
    seen: list[str] = []
    run(lambda request: seen.append(request.url.host) or httpx.Response(200, json={"id": MODEL, "object": "model", "created": 0, "owned_by": "openai"}))
    assert seen == ["api.openai.com"]


def test_a_rejected_key_says_401_and_the_code_but_never_the_masked_key_openai_quotes() -> None:
    quoted = f"Incorrect API key provided: {FAKE_KEY[:8]}****{FAKE_KEY[-4:]}. You can find your API key at https://platform.openai.com."
    out = run(error(401, code="invalid_api_key", kind="invalid_request_error", message=quoted, headers={"x-request-id": "req_abc123"}), key_source="the encrypted store")
    assert out is not None
    assert "HTTP 401" in out and "invalid_api_key" in out and "req_abc123" in out and "the encrypted store" in out
    assert "forget-key" in out
    assert FAKE_KEY not in out and FAKE_KEY[:8] + "****" not in out and "Incorrect API key" not in out  # OpenAI's message text is never shown


def test_a_valid_key_without_permission_is_a_403_not_a_bad_key() -> None:
    out = run(error(403, code="insufficient_permissions", message="You have insufficient permissions for this operation."))
    assert out is not None and "HTTP 403" in out and "insufficient_permissions" in out
    assert "rejected the API key" not in out and "accepted the key" in out


def test_an_unknown_model_is_a_404_naming_the_model() -> None:
    out = run(error(404, code="model_not_found", kind="invalid_request_error"))
    assert out is not None and "HTTP 404" in out and "model_not_found" in out and repr(MODEL) in out
    assert "key" not in out.lower().replace("this key", "")  # not blamed on the key


@pytest.mark.parametrize(("code", "needle"), [("insufficient_quota", "billing"), ("rate_limit_exceeded", "rate limited")])
def test_a_429_is_told_apart_from_a_bad_key_and_from_a_dead_network(code: str, needle: str) -> None:
    out = run(error(429, code=code))
    assert out is not None and "HTTP 429" in out and code in out and needle in out and "internet" not in out


def test_a_server_error_is_not_blamed_on_the_key_or_the_network() -> None:
    out = run(error(503, message="overloaded"))
    assert out is not None and "HTTP 503" in out and "not a key problem" in out and "internet" not in out


def test_a_dead_network_and_a_timeout_are_reported_as_such_without_a_status() -> None:
    def refuse(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("no route")

    def slow(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("slow")

    dead, late = run(refuse), run(slow)
    assert dead is not None and "could not be reached" in dead and "not a key problem" in dead and "HTTP" not in dead
    assert late is not None and "timeout" in late and "HTTP" not in late


@pytest.mark.parametrize("bad", ["has spaces", "x" * 65, "line\nbreak", "<script>", "", 7, None])
def test_an_error_code_that_is_not_a_plain_identifier_is_dropped(bad: Any) -> None:
    out = run(error(401, code=bad, kind=bad))
    assert out is not None and "HTTP 401" in out and "code" not in out.split("HTTP 401")[1].split(".")[0]
    assert "<script>" not in out and "\n" not in out


def test_a_code_that_happens_to_equal_the_key_is_still_redacted() -> None:
    short = "sk-" + "q" * 30  # a plain identifier, so it would pass the filter
    out = run(error(401, code=short), key=short)
    assert out is not None and short not in out and "[key]" in out


def test_an_organization_or_project_in_the_environment_is_named_not_shown(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("OPENAI_ORG_ID", "org-secretvalue")
    monkeypatch.setenv("OPENAI_PROJECT_ID", "proj_secretvalue")
    out = run(error(401, code="invalid_api_key"))
    assert out is not None and "OPENAI_ORG_ID and OPENAI_PROJECT_ID" in out and "secretvalue" not in out


def test_a_failure_before_any_answer_is_still_reported_without_the_key() -> None:
    out = describe_access_failure(openai.OpenAIError(f"boom {FAKE_KEY}"), MODEL, key=FAKE_KEY)
    assert "before OpenAI answered" in out and "OpenAIError" in out and FAKE_KEY not in out  # the exception's own text is not repeated


# --- the engine's refusal -------------------------------------------------------------------------------


def test_the_engine_refusal_carries_the_status_and_says_nothing_was_billed(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    from app.cli import engine

    monkeypatch.setattr(engine, "model_access", lambda key, model, **k: describe_access_failure(_status(401, "invalid_api_key"), model, key=key))
    monkeypatch.setattr("app.coding.guard_process.keep_awake", lambda: None)
    monkeypatch.setattr("app.coding.guard_process.start_guard", lambda *a, **k: None)
    paths = make_paths(tmp_path / "home")
    live = engine.LiveEngine(paths, UserConfig(model=MODEL, price_input_per_mtok=2.0, price_output_per_mtok=10.0), key=FAKE_KEY, image_id="sha256:" + "a" * 64, manifest=tmp_path / "m.json", cap_usd=0.5, say=lambda text: None)
    with pytest.raises(engine.EngineRefused) as caught:
        live.__enter__()
    text = str(caught.value)
    assert "HTTP 401" in text and "invalid_api_key" in text and text.endswith("Nothing was billed.") and ".." not in text and FAKE_KEY not in text


def _status(status: int, code: str) -> openai.APIStatusError:
    response = httpx.Response(status, json={"error": {"message": "m", "type": None, "code": code}}, request=httpx.Request("GET", "https://api.openai.com/v1/models/x"))
    return openai.AuthenticationError("m", response=response, body={"code": code})


# --- doctor --check-key ---------------------------------------------------------------------------------


def test_doctor_contacts_openai_only_when_asked_and_reports_the_reason(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    where = make_paths(tmp_path / "home")
    where.ensure()
    monkeypatch.setenv(credentials.ENV_VAR, FAKE_KEY)
    monkeypatch.setattr(runtime, "opencode_ok", lambda paths: None)
    config = UserConfig(model=MODEL, price_input_per_mtok=2.0, price_output_per_mtok=10.0, prices_source="typed")
    asked: list[str] = []

    def failing(key: str, model: str, **kwargs: Any) -> str:
        asked.append(model)
        return "OpenAI rejected the API key: HTTP 401, code invalid_api_key"

    quiet = lambda command, **k: subprocess.CompletedProcess(command, 1, "", "")  # noqa: E731
    common: dict[str, Any] = {"run": quiet, "which": lambda name: None, "port_free": lambda port: True, "free_gb": lambda path: 100.0, "on_mains": lambda: True, "model_check": failing}
    assert not [c for c in doctor.run_checks(where, config, **common) if "free check" in c.name] and asked == []  # the default never contacts OpenAI
    found = {c.name: c for c in doctor.run_checks(where, config, check_key=True, **common)}
    result = found["OpenAI key can see the model (free check)"]
    assert asked == [MODEL] and result.status == "fail" and "HTTP 401" in result.detail and FAKE_KEY not in doctor.render(list(found.values()))


def test_doctor_check_key_passes_when_openai_accepts_the_key(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    where = make_paths(tmp_path / "home")
    where.ensure()
    monkeypatch.setenv(credentials.ENV_VAR, FAKE_KEY)
    monkeypatch.setattr(runtime, "opencode_ok", lambda paths: None)
    quiet = lambda command, **k: subprocess.CompletedProcess(command, 1, "", "")  # noqa: E731
    config = UserConfig(model=MODEL, price_input_per_mtok=2.0, price_output_per_mtok=10.0)
    checks = doctor.run_checks(where, config, run=quiet, which=lambda n: None, port_free=lambda p: True, free_gb=lambda p: 100.0, on_mains=lambda: True,
                               check_key=True, model_check=lambda key, model, **k: None)  # fmt: skip
    assert {c.name: c.status for c in checks}["OpenAI key can see the model (free check)"] == "ok"


# --- prices are never silently official ------------------------------------------------------------------


def test_prices_pressed_through_are_recorded_as_defaults_and_say_so(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    where = make_paths(tmp_path / "home")
    monkeypatch.setenv(credentials.ENV_VAR, FAKE_KEY)
    io = IO("", "", "")  # model, input price, output price: Enter on all
    cli_setup.configure(where, UserConfig(), io, key_from_env=True, ask_secret=lambda p: "", interactive=True, model=None, input_price=None, output_price=None)
    saved = load(where.config)
    assert saved.prices_source == "default" and "NOT an OpenAI-published rate" in saved.prices_note
    assert "NOT an official OpenAI rate" in io.text
    assert "NOT an OpenAI-published rate" in approval_text(saved, 0.5)  # shown again where the spending is approved


def test_typed_prices_are_recorded_as_typed_and_one_default_is_still_a_default(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    where = make_paths(tmp_path / "home")
    monkeypatch.setenv(credentials.ENV_VAR, FAKE_KEY)
    cli_setup.configure(where, UserConfig(), IO("", "3", "12"), key_from_env=True, ask_secret=lambda p: "", interactive=True, model=None, input_price=None, output_price=None)
    assert load(where.config).prices_source == "typed"
    cli_setup.configure(where, UserConfig(), IO("", "3", ""), key_from_env=True, ask_secret=lambda p: "", interactive=True, model=None, input_price=None, output_price=None)
    assert load(where.config).prices_source == "default"  # one unverified figure makes the pair unverified
    cli_setup.configure(where, UserConfig(), IO(), key_from_env=True, ask_secret=lambda p: "", interactive=False, model="m", input_price=1.0, output_price=2.0)
    assert load(where.config).prices_source == "typed"  # passed as flags


def test_a_config_from_before_the_source_was_recorded_loads_and_is_labelled_unrecorded(tmp_path: Path) -> None:
    where = make_paths(tmp_path / "home")
    where.config.parent.mkdir(parents=True)
    where.config.write_text('model = "gpt-6.1-sol"\nprice_input_per_mtok = 2.0\nprice_output_per_mtok = 10.0\nrun_cap_usd = 0.5\n', encoding="utf-8")
    config = load(where.config)
    assert config.prices_source is None and "source not recorded" in config.prices_note
    save(where.config, config)
    assert "prices_source" not in where.config.read_text(encoding="utf-8")  # saving does not invent a source


def test_the_price_source_is_validated() -> None:
    with pytest.raises(ConfigError, match="prices_source"):
        validate(UserConfig(prices_source="official"))
    assert json.loads(json.dumps(UserConfig(prices_source="typed").prices_source)) == "typed"


def test_the_spending_figures_themselves_are_unchanged() -> None:
    from app.cli.userconfig import DEFAULT_CAP_USD, DEFAULT_PRICE_INPUT, DEFAULT_PRICE_OUTPUT, MAX_CAP_USD

    assert (DEFAULT_PRICE_INPUT, DEFAULT_PRICE_OUTPUT, DEFAULT_CAP_USD, MAX_CAP_USD) == (2.0, 10.0, 0.50, 2.00)
