"""Regression tests for the first real build's failure (2026-11-03, run dcb942e8), for the token accounting, and for 429 handling.
Offline: fake upstreams and mocked HTTP only. The numbers in the first group come from that run's own audit and ledger files.

What happened: three of four attempts ended with "APIError: Too Many Requests". Every one of those 429s was DevPilot's own gateway
refusing ("the token budget is used up", never forwarded to OpenAI). The engine retried each five times because a 429 looks
temporary, the reason never reached it, recovery then retried the attempt, and about $0.27 of finished work was rolled back.

`opencode_retries` below is the pinned OpenCode 1.19.5's own retry rule, ported from its binary (read as bytes, never run). It is how
these tests know what the engine would do with each answer the gateway gives; `test_the_port_matches_the_pinned_binary` checks the
copied text against the binary itself when the binary is on this machine."""

from __future__ import annotations

import json
import math
import re
from pathlib import Path
from typing import Any

import httpx
import pytest

from app.coding.launcher import read_refusals
from app.coding.types import CodingOutcome, CodingStatus
from app.orchestrator.llm_client import LLMError, LLMErrorKind, OpenAIResponsesClient, rate_wait
from app.plan_cli import planning_settings, run_planning
from app.recovery.diagnosis import AttemptSignal, FailureCategory, classify_attempt
from app.recovery.engine import Action
from app.recovery.policy import RecoveryState, ToolOptions, decide
from app.selection.adaptive import AdaptiveSelector, EligibilityPolicy
from app.selection.catalog import CODING_CAPABILITIES, OPENCODE_NAME, opencode_profile
from app.selection.model import SelectionContext
from app.selection.selector import NoProvider, ProviderOption
from app.tools.adapters.coding import _CodingResult, _name_a_budget_refusal, attempt_cost
from edge.filter_policy import Refused
from edge.gateway_policy import (
    LONG_WAIT_STOP,
    LONGEST_FORWARDED_WAIT,
    QUOTA_STOP,
    STOP_STATUS,
    Budget,
    CheckedRequest,
    GatewayPolicy,
    Usage,
    answer_to_429,
    asked_wait,
    budget_refusal,
    error_facts,
    safe_headers,
)
from tests.helpers import FAKE_OPENAI_KEY, make_settings
from tests.unit.test_gateway import STREAM, FakeReply, Rig, _body, _request
from tests.unit.test_llm_client import MODEL, REQUEST, RESPONSE_BODY

# --- The pinned engine's retry rule ------------------------------------------------------------------------

BINARY = Path(r"D:\DevPilotCache\opencode\1.19.5\extracted\package\bin\opencode")
# Verbatim from the binary at byte 119,054,600 onwards (`ed`, `zc`, `Hc`, `Jc`, `Qc`), and the AI SDK's APICallError default at 115,288,904.
PATTERNS_TEXT = (
    r"/429|500|502|503|504|524/i",
    r"/rate increased too quickly|rate limit|rate-limit|rate_limit|too many requests/i",
    r"/overloaded|service unavailable|service_unavailable|service-unavailable|internal error|internal_error|internal server error|server error"
    r"|server_error|server-error|provider returned error|provider_returned_error|provider-returned-error/i",
    r"/terminated|fetch failed|failed to fetch|network[-_\s]error|upstream connect|connection error|connection refused|connection lost"
    r"|socket connection was closed|socket hang up|reset before headers|getaddrinfo|enotfound|eai_again|econnrefused|econnreset|etimedout/i",
    r"/^timeout$|\b(?:request|response|connection|network|stream|read) (?:timeout|timed out|time out)\b/i",
    r"/try your request again|retry your request|resource exhausted|resource_exhausted/i",
    r"/\btry again (?:later|in\b)|\b(?:currently|temporarily) at capacity\b/i",
)
STOP_BODY_PATTERNS = [re.compile(text[1:-2], re.IGNORECASE) for text in PATTERNS_TEXT]
SDK_RETRYABLE = "isRetryable:vr=a!=null&&(a===408||a===409||a===429||a>=500)"
MAX_RETRIES, FIRST_DELAY_MS, FACTOR, JITTER = 5, 2000, 2, 0.25


def opencode_retries(status: int, message: str, body: str) -> bool:
    """`od` for an HTTP error (an AI SDK APICallError): retried when retryable by status, 5xx, or a pattern matches message or body."""
    sdk_retryable = status in (408, 409, 429) or status >= 500
    return sdk_retryable or any(p.search(message) for p in STOP_BODY_PATTERNS) or any(p.search(body) for p in STOP_BODY_PATTERNS)


def opencode_delay_ms(attempt: int, headers: dict[str, str], jitter: float) -> float:
    """`td`: Retry-After-ms, then Retry-After (seconds), with no upper limit; else 2 s doubling, 25% jitter."""
    if "retry-after-ms" in headers:
        return float(headers["retry-after-ms"])
    if "retry-after" in headers:
        return math.ceil(float(headers["retry-after"]) * 1000)
    base = FIRST_DELAY_MS * FACTOR ** (attempt - 1)
    return math.ceil(base + base * JITTER * jitter)


def _message(body: bytes) -> str:
    """What the AI SDK makes the error's message: the OpenAI error's `message`, or the status text when there is none."""
    try:
        error = json.loads(body).get("error")
    except ValueError:
        return ""
    return error.get("message", "") if isinstance(error, dict) else str(error)


def test_the_port_matches_the_pinned_binary() -> None:
    if not BINARY.is_file():
        pytest.skip("the pinned OpenCode binary is not on this machine")
    with BINARY.open("rb") as binary:
        binary.seek(119_054_600)
        rule = binary.read(3_000).decode("utf-8", "replace")
        binary.seek(115_288_600)
        sdk = binary.read(1_200).decode("utf-8", "replace")
    for text in PATTERNS_TEXT:
        assert text in rule, text
    assert "zc=2000,Hc=2,Jc=0.25" in rule and "Qc=5" in rule and "if(!e.data.isRetryable&&!(r!==void 0&&r>=500)&&!is(e.data.message)&&!is(e.data.responseBody))return;" in rule
    assert SDK_RETRYABLE in sdk


def test_the_port_reproduces_what_the_first_run_did() -> None:
    """The old answer to a used-up budget: retried five times. The audit's gaps were 2.2, 5.0, 8.1, 19.7 and 32.6 s."""
    old = json.dumps({"error": "Too Many Requests"})
    assert opencode_retries(429, "Too Many Requests", old)
    gaps = [2.187, 4.963, 8.143, 19.680, 32.586]  # from the real gateway audit, attempt 4
    for attempt, gap in enumerate(gaps, start=1):
        assert opencode_delay_ms(attempt, {}, 0.0) / 1000 <= gap <= opencode_delay_ms(attempt, {}, 1.0) / 1000 + 0.05, attempt
    assert len(gaps) == MAX_RETRIES


@pytest.mark.parametrize(
    ("answer", "status"),
    [
        (budget_refusal("the token budget is used up").body, STOP_STATUS),
        (budget_refusal("the request budget is used up").body, STOP_STATUS),
        (budget_refusal("the spending budget is used up").body, STOP_STATUS),
        (QUOTA_STOP, STOP_STATUS),
        (LONG_WAIT_STOP, STOP_STATUS),
    ],
)
def test_every_answer_that_must_end_an_attempt_is_not_retried_by_the_engine(answer: bytes | None, status: int) -> None:
    assert answer is not None
    assert not opencode_retries(status, _message(answer), answer.decode())
    assert "exhausted" not in answer.decode()  # the engine's other path retries on that word


@pytest.mark.parametrize("code", ["access_terminated", "insufficient_quota", "billing_hard_limit_reached"])
def test_echoing_openai_own_words_could_restart_the_retries_so_the_gateway_does_not(code: str) -> None:
    openai_body = json.dumps({"error": {"message": "x", "type": code, "code": code}})
    if code == "access_terminated":
        assert opencode_retries(402, "x", openai_body)  # "terminated" is one of the engine's patterns
    status, own, _ = answer_to_429({"code": code}, {})
    assert status == STOP_STATUS and own == QUOTA_STOP and not opencode_retries(status, _message(own), own.decode())


# --- What the real run showed ----------------------------------------------------------------------------


def test_a_used_up_budget_is_not_answered_as_a_rate_limit_and_says_why() -> None:
    rig = Rig(policy=GatewayPolicy(model="gpt-test", max_output_tokens=1_000, max_requests=1, max_tokens=50_000))
    assert rig.send(_request(_body()))[0] == 200
    status, _, body = rig.send(_request(_body()))
    assert status == STOP_STATUS
    error = json.loads(body)["error"]
    assert error["code"] == "devpilot_budget_used_up" and "request budget is used up" in error["message"] and "cannot help" in error["message"]
    refused = rig.audit[-1]
    assert refused["forwarded"] is False and refused["status"] == STOP_STATUS and "budget is used up" in refused["reason"]
    assert len(rig.upstream.calls) == 1  # nothing reached OpenAI


AUDIT = [
    {"ts": 1.0, "method": "POST", "target": "/v1/responses", "status": 200, "forwarded": True, "upstream": {"status": 200}, "ms": 2045},
    {"ts": 2.0, "method": "POST", "target": "/v1/responses", "status": 429, "forwarded": False, "reason": "the token budget is used up", "ms": 1},  # as the real run wrote it
    {"ts": 3.0, "method": "POST", "target": "/v1/responses", "status": 429, "forwarded": False, "reason": "the token budget is used up", "ms": 1},
    {"ts": 4.0, "method": "POST", "target": "/v1/responses", "status": 402, "forwarded": False, "reason": "the spending budget is used up", "ms": 1},
    {"ts": 5.0, "method": "POST", "target": "/v1/responses", "status": 429, "forwarded": True, "upstream": {"status": 429, "code": "rate_limit_exceeded", "headers": {"retry-after": "2", "x-request-id": "req_1"}}},
    {"ts": 6.0, "method": "POST", "target": "/v1/responses", "status": 402, "forwarded": True, "upstream": {"status": 429, "code": "insufficient_quota"}},
]  # fmt: skip


def test_the_attempts_audit_names_what_the_gateway_refused_and_what_openai_said(tmp_path: Path) -> None:
    path = tmp_path / "gateway-audit.jsonl"
    path.write_text("\n".join([json.dumps(e) for e in AUDIT] + ["{broken", "[1]"]) + "\n", encoding="utf-8")
    found = read_refusals(path)
    assert found["budget_refusals"] == 3 and found["budget_reason"] == "the spending budget is used up" and found["upstream_429"] == 2
    assert found["upstream_rejections"] == [
        {"status": 429, "code": "rate_limit_exceeded", "headers": {"retry-after": "2", "x-request-id": "req_1"}, "answered": 429},
        {"status": 429, "code": "insufficient_quota", "answered": 402},
    ]
    assert read_refusals(tmp_path / "missing.jsonl") == {"budget_refusals": 0, "budget_reason": None, "upstream_429": 0, "upstream_rejections": []}


def test_recovery_no_longer_retries_an_attempt_the_gateway_refused() -> None:
    detail = "APIError: Too Many Requests: Too Many Requests"
    before = classify_attempt(AttemptSignal(1, CodingStatus.FAILED, "RESTORED", detail))
    assert Action.RETRY in before.actions  # what the real run did: it tried again, and met the same limit
    result = _CodingResult(CodingOutcome(status=CodingStatus.FAILED, detail=detail), phase="RESTORED", ledger={"budget_refusals": 6, "budget_reason": "the token budget is used up"})
    _name_a_budget_refusal(result)
    assert result.outcome.status is CodingStatus.LIMIT
    assert "6 request(s)" in (result.outcome.detail or "") and "token budget is used up" in (result.outcome.detail or "") and detail in (result.outcome.detail or "")
    after = classify_attempt(AttemptSignal(1, result.outcome.status, "RESTORED", result.outcome.detail or ""))
    assert after.category is FailureCategory.RESOURCE_EXHAUSTION and Action.RETRY not in after.actions
    assert decide(after, RecoveryState(attempts_used=1, max_attempts=2, repairs_used=0, max_repairs=1, tools=ToolOptions())).action is Action.STOP


def test_an_attempt_openai_rate_limited_is_not_started_again_automatically_and_keeps_the_facts() -> None:
    ledger = {"upstream_429": 6, "upstream_rejections": [{"status": 429, "code": "rate_limit_exceeded", "headers": {"retry-after": "2", "x-request-id": "req_9"}, "answered": 429}]}
    result = _CodingResult(CodingOutcome(status=CodingStatus.FAILED, detail="APIError: Rate limit reached"), phase="RESTORED", ledger=ledger)
    _name_a_budget_refusal(result)
    assert result.outcome.status is CodingStatus.LIMIT and "429" in (result.outcome.detail or "") and "req_9" in (result.outcome.detail or "") and "Retry-After 2" in (result.outcome.detail or "")
    diagnosis = classify_attempt(AttemptSignal(1, result.outcome.status, "RESTORED", result.outcome.detail or ""))
    assert decide(diagnosis, RecoveryState(attempts_used=1, max_attempts=2, repairs_used=0, max_repairs=1)).action is Action.STOP


@pytest.mark.parametrize(
    ("status", "kept", "ledger"),
    [
        (CodingStatus.FAILED, True, {"budget_refusals": 2, "upstream_429": 1}),  # a kept attempt is not a failure
        (CodingStatus.TIMEOUT, False, {"budget_refusals": 2}),  # another reason stays another reason
        (CodingStatus.FAILED, False, {"budget_refusals": 0, "upstream_429": 0}),  # a failure the gateway and OpenAI did not cause
        (CodingStatus.FAILED, False, {}),  # a launcher with no gateway
    ],
)
def test_only_a_failed_attempt_with_refusals_in_its_audit_is_renamed(status: CodingStatus, kept: bool, ledger: dict[str, Any]) -> None:
    result = _CodingResult(CodingOutcome(status=status, detail="boom"), kept=kept, ledger=ledger)
    _name_a_budget_refusal(result)
    assert result.outcome.status is status and result.outcome.detail == "boom"


def test_a_refused_attempt_is_charged_what_it_really_spent_and_an_unreadable_ledger_the_whole_allowance() -> None:
    assert attempt_cost({"requests": 6, "charged_tokens": 39_391, "cost_usd": 0.1095, "all_known": True, "budget_refusals": 6}, 0.30) == pytest.approx(0.1095)
    assert attempt_cost({"requests": 0, "charged_tokens": 0, "cost_usd": None, "all_known": False, "budget_refusals": 0}, 0.30) == 0.30


class _Launcher:
    def unavailable_reason(self) -> None:
        return None


def test_an_attempt_that_starts_with_room_for_too_few_requests_is_not_started(tmp_path: Path) -> None:
    """The real run's fourth attempt began with about 44,000 tokens left, was refused after three requests, and cost $0.066 to be rolled back."""
    settings = make_settings(tmp_path, gateway_max_output_tokens=4_000, openai_price_input_per_mtok=2.0, openai_price_output_per_mtok=10.0)
    profile = opencode_profile(settings)
    assert profile.min_tokens == 4 * 17_202 + 4_000
    policy = EligibilityPolicy(frozenset({"docker-internal"}), frozenset({"OPENAI_API_KEY"}), frozenset({"api.openai.com"}))
    selector = AdaptiveSelector([ProviderOption(OPENCODE_NAME, _Launcher(), CODING_CAPABILITIES, profile=profile)], policy=policy, memory_probe=lambda: None)  # type: ignore[arg-type]
    with pytest.raises(NoProvider) as refused:
        selector.choose("code_generation", SelectionContext(stack="python-cli", remaining_tokens=44_000))
    assert "tokens" in refused.value.excluded[OPENCODE_NAME]
    assert selector.choose("code_generation", SelectionContext(stack="python-cli", remaining_tokens=300_000)).provider == OPENCODE_NAME


# --- The token accounting ----------------------------------------------------------------------------------

# One real attempt (the first), request by request: the reservation the gateway wrote before forwarding (body bytes + 512, plus the
# 4,000-token output cap) and what OpenAI then reported. Bytes are a true upper bound on tokens, and about six times the real figure.
OBSERVED = [(25_449, 4_200, 79), (26_491, 4_392, 699), (32_292, 5_114, 79), (33_338, 5_306, 2_834), (44_347, 8_164, 95), (47_066, 8_372, 57)]


def _attempt_budget(max_tokens: int) -> Budget:
    return Budget(GatewayPolicy(model="m", max_output_tokens=4_000, max_requests=15, max_tokens=max_tokens, max_usd=0.30, price_input_per_mtok=2.0, price_output_per_mtok=10.0))


def test_a_reservation_is_a_hold_and_what_is_counted_is_what_openai_reported() -> None:
    budget = _attempt_budget(200_000)
    for reserved, used_in, used_out in OBSERVED:
        held = budget.reserve(CheckedRequest(b"{}", True, reserved - 4_000, 4_000))
        assert held.tokens == reserved and budget.outstanding == 1
        charge = budget.settle(held, Usage(used_in, used_out))
        assert charge.known and charge.tokens == used_in + used_out
    assert budget.tokens == sum(i + o for _, i, o in OBSERVED) == 39_391  # the real attempt's own ledger total, not the reservations
    assert budget.usd == pytest.approx(sum(i * 2 + o * 10 for _, i, o in OBSERVED) / 1e6) and budget.outstanding == 0


def test_unknown_usage_stays_charged_at_the_whole_reservation_in_tokens_and_dollars() -> None:
    budget = _attempt_budget(200_000)
    held = budget.reserve(CheckedRequest(b"{}", True, 21_449, 4_000))
    charge = budget.settle(held, None)
    assert not charge.known and budget.tokens == 25_449 and budget.usd == pytest.approx((21_449 * 2 + 4_000 * 10) / 1e6)


def test_cached_tokens_are_charged_at_the_full_input_price() -> None:
    """The configured prices are $2 in and $10 out. OpenAI reports cached tokens inside input_tokens; the gateway does not discount
    them, so its figure is never below the bill. A cached price is not configured anywhere, and it cannot be checked offline."""
    budget = _attempt_budget(200_000)
    held = budget.reserve(CheckedRequest(b"{}", True, 21_449, 4_000))
    budget.settle(held, Usage(4_386, 89, cached_tokens=4_327))
    assert budget.usd == pytest.approx((4_386 * 2 + 89 * 10) / 1e6)


def test_the_first_runs_token_limit_refused_where_the_dollars_would_have_allowed() -> None:
    budget = _attempt_budget(80_000)
    for reserved, used_in, used_out in OBSERVED:
        budget.settle(budget.reserve(CheckedRequest(b"{}", True, reserved - 4_000, 4_000)), Usage(used_in, used_out))
    with pytest.raises(Refused, match="token budget"):
        budget.reserve(CheckedRequest(b"{}", True, 45_000, 4_000))  # the seventh request, refused in the real run
    assert budget.usd + (45_000 * 2 + 4_000 * 10) / 1e6 < 0.30


def _grow(max_tokens: int, max_usd: float = 0.30) -> tuple[int, str]:
    """Requests that keep growing as the first run's did (+4,300 bytes each, real input about a sixth of the bytes, 1,000 output
    tokens). How many are admitted, and which limit refuses the next."""
    budget = Budget(GatewayPolicy(model="m", max_output_tokens=4_000, max_requests=15, max_tokens=max_tokens, max_usd=max_usd, price_input_per_mtok=2.0, price_output_per_mtok=10.0))
    ceiling = 21_449
    for admitted in range(100):
        try:
            held = budget.reserve(CheckedRequest(b"{}", True, ceiling, 4_000))
        except Refused as refused:
            return admitted, refused.reason
        budget.settle(held, Usage(ceiling // 6, 1_000))
        ceiling += 4_300
    raise AssertionError("never refused")


def test_at_the_new_limits_the_dollars_bind_before_the_tokens() -> None:
    admitted, reason = _grow(200_000)
    assert reason == "the spending budget is used up" and admitted >= 8  # the attempts that got anywhere needed 2 to 7
    assert _grow(80_000)[1] == "the token budget is used up"  # the first run's limit: tokens bound first
    assert _grow(200_000, max_usd=10.0)[1] in ("the request budget is used up", "the token budget is used up")  # without dollars, the other limits still hold


def test_a_long_attempt_is_ended_by_its_dollars_not_by_a_token_limit() -> None:
    """Could 200,000 tokens still reject a legitimate request? Only after the attempt's $0.30 would already have refused it."""
    budget = Budget(GatewayPolicy(model="m", max_output_tokens=4_000, max_requests=15, max_tokens=200_000, max_usd=None))
    ceiling, admitted = 21_449, 0
    while True:
        try:
            held = budget.reserve(CheckedRequest(b"{}", True, ceiling, 4_000))
        except Refused:
            break
        budget.settle(held, Usage(ceiling // 6, 1_000))
        admitted, ceiling = admitted + 1, ceiling + 4_300
    assert admitted >= _grow(200_000)[0]


# --- OpenAI's own 429s, through the gateway ----------------------------------------------------------------


class Sequence:
    """An upstream that answers with each reply in turn."""

    def __init__(self, *replies: FakeReply) -> None:
        self.replies = list(replies)
        self.calls: list[tuple[bytes, bool]] = []

    def send(self, body: bytes, *, stream: bool) -> FakeReply:
        self.calls.append((body, stream))
        return self.replies.pop(0)


def rate(headers: dict[str, str] | None = None, code: str | None = "rate_limit_exceeded") -> FakeReply:
    error = {"message": "Rate limit reached for the model in organization org-secret-name. sk-secret-123", "type": "requests", "code": code}
    return FakeReply(429, "application/json", json.dumps({"error": error}).encode(), headers)


def test_a_temporary_rate_limit_is_passed_on_once_for_the_engine_to_wait_out_with_the_pause_openai_asked() -> None:
    upstream = Sequence(rate({"retry-after": "2", "x-request-id": "req_1"}))
    rig = Rig(upstream)  # type: ignore[arg-type]
    status, headers, body = rig.send(_request(_body()))
    assert status == 429 and headers["retry-after"] == "2" and len(upstream.calls) == 1  # the gateway itself never retries
    assert opencode_retries(429, _message(body), body.decode()) and opencode_delay_ms(1, {"retry-after": headers["retry-after"]}, 0.5) == 2_000
    assert rig.audit[0]["upstream"] == {"status": 429, "code": "rate_limit_exceeded", "type": "requests", "headers": {"retry-after": "2", "x-request-id": "req_1"}}


def test_without_a_retry_after_the_engines_own_waits_are_short_and_end() -> None:
    rig = Rig(Sequence(rate()))  # type: ignore[arg-type]
    status, headers, _ = rig.send(_request(_body()))
    assert status == 429 and "retry-after" not in headers
    worst = sum(opencode_delay_ms(n, {}, 1.0) for n in range(1, MAX_RETRIES + 1)) / 1000
    assert worst <= 78.0  # 2.5 + 5 + 10 + 20 + 40 s at most, then the engine gives up: inside an attempt's 300 s


@pytest.mark.parametrize("asked", [{"retry-after": "60"}, {"retry-after-ms": "25000"}, {"retry-after": str(int(LONGEST_FORWARDED_WAIT) + 1)}])
def test_a_pause_longer_than_an_attempt_should_wait_ends_the_attempt_instead_of_being_waited_without_limit(asked: dict[str, str]) -> None:
    rig = Rig(Sequence(rate({**asked, "x-request-id": "req_9"})))  # type: ignore[arg-type]
    status, headers, body = rig.send(_request(_body()))
    assert status == STOP_STATUS and body == LONG_WAIT_STOP and "retry-after" not in headers
    assert not opencode_retries(status, _message(body), body.decode())
    assert rig.audit[0]["upstream"]["headers"]["x-request-id"] == "req_9"


@pytest.mark.parametrize("code", ["insufficient_quota", "billing_hard_limit_reached", "billing_not_active", "account_deactivated", "access_terminated"])
def test_an_exhausted_account_is_never_retried_and_the_engine_is_told_to_stop(code: str) -> None:
    rig = Rig(Sequence(rate({"retry-after": "1"}, code=code)))  # type: ignore[arg-type]
    status, _, body = rig.send(_request(_body()))
    assert status == STOP_STATUS and body == QUOTA_STOP and not opencode_retries(status, _message(body), body.decode())
    assert rig.audit[0]["upstream"]["code"] == code  # the real code stays in DevPilot's own record


def test_a_429_that_does_not_say_why_is_passed_on_as_it_came() -> None:
    for code in (None, "something_new"):
        upstream = Sequence(rate(code=code))
        status, _, body = Rig(upstream).send(_request(_body()))  # type: ignore[arg-type]
        assert status == 429 and b"org-secret-name" in body and len(upstream.calls) == 1  # OpenAI's own answer, for the engine only


def test_nothing_secret_reaches_the_gateways_records() -> None:
    headers = {"retry-after": "60", "x-request-id": "req_9", "x-ratelimit-remaining-tokens": "0", "authorization": "Bearer sk-nope", "set-cookie": "a=b"}
    rig = Rig(Sequence(rate(headers)))  # type: ignore[arg-type]
    rig.send(_request(_body()))
    assert rig.audit[0]["upstream"]["headers"] == {"retry-after": "60", "x-request-id": "req_9", "x-ratelimit-remaining-tokens": "0"}
    everything = json.dumps(rig.audit) + json.dumps(rig.journal)
    assert "sk-secret-123" not in everything and "sk-nope" not in everything and "set-cookie" not in everything and "org-secret-name" not in everything


def test_a_rejected_request_is_not_billed_and_leaves_nothing_reserved() -> None:
    rig = Rig(Sequence(rate({"retry-after": "3"})))  # type: ignore[arg-type]
    rig.send(_request(_body()))
    budget = rig.gateway.budget
    assert (budget.tokens, budget.usd, budget.outstanding, budget.requests) == (0, 0.0, 0, 1)  # one request counted, none billed
    assert rig.journal[0]["status"] == 0 and rig.journal[0]["usage_known"] is False  # the reservation was on disk before anything was sent
    assert rig.journal[-1]["usage_known"] is True and rig.journal[-1]["charged_tokens"] == 0


def test_every_engine_retry_is_a_new_request_so_the_request_budget_ends_a_rate_limited_attempt() -> None:
    rig = Rig(Sequence(rate(), rate(), rate(), rate()), GatewayPolicy(model="gpt-test", max_output_tokens=1_000, max_requests=3, max_tokens=50_000))  # type: ignore[arg-type]
    answers = [rig.send(_request(_body()))[0] for _ in range(4)]  # the engine's first try and its retries
    assert answers == [429, 429, 429, STOP_STATUS]
    last = json.loads(rig.send(_request(_body()))[2])
    assert last["error"]["code"] == "devpilot_budget_used_up" and not opencode_retries(STOP_STATUS, last["error"]["message"], json.dumps(last))


def test_other_failures_are_still_charged_in_full_as_before() -> None:
    rig = Rig(Sequence(FakeReply(500, "application/json", b'{"error":{"code":"server_error"}}')))  # type: ignore[arg-type]
    assert rig.send(_request(_body()))[0] == 502
    assert rig.gateway.budget.tokens >= 1_000 and rig.audit[0]["upstream"] == {"status": 500, "code": "server_error"}


def test_a_429_that_is_not_json_is_not_passed_on() -> None:
    assert Rig(Sequence(FakeReply(429, "text/html", b"<html>"))).send(_request(_body()))[0] == 502  # type: ignore[arg-type]


def test_a_200_is_unchanged() -> None:
    status, _, body = Rig(Sequence(FakeReply(200, "text/event-stream", STREAM))).send(_request(_body()))  # type: ignore[arg-type]
    assert status == 200 and body == STREAM


def test_the_helpers_keep_only_safe_facts() -> None:
    assert safe_headers({"Authorization": "Bearer x", "X-Request-Id": "req_1", "Retry-After": "3", "x-ratelimit-reset-tokens": "6m0s", "x-request-id-2": "no", "openai-processing-ms": "9"}) == {
        "x-request-id": "req_1", "retry-after": "3", "x-ratelimit-reset-tokens": "6m0s", "openai-processing-ms": "9"}  # fmt: skip
    assert safe_headers({"x-request-id": "req\r\nInjected: yes", "retry-after": "x" * 100}) == {}
    assert error_facts(b'{"error":{"code":"rate_limit_exceeded","type":"tokens","message":"Incorrect API key sk-abc"}}') == {"code": "rate_limit_exceeded", "type": "tokens"}
    assert error_facts(b'{"error":{"code":"has space","type":5}}') == {} and error_facts(b"nonsense") == {} and error_facts(b"[]") == {}


@pytest.mark.parametrize(
    ("headers", "expected"),
    [({}, None), ({"retry-after": "7"}, 7.0), ({"retry-after-ms": "1500"}, 1.5), ({"retry-after": "0"}, 0.0), ({"retry-after": "Wed, 21 Oct 2026 07:28:00 GMT"}, None),
     ({"retry-after": "-5"}, None), ({"retry-after": "nan"}, None), ({"retry-after": "inf"}, None), ({"retry-after-ms": "x", "retry-after": "4"}, 4.0)],
)  # fmt: skip
def test_the_pause_openai_asked_for(headers: dict[str, str], expected: float | None) -> None:
    assert asked_wait(headers) == expected


def test_answers_to_429_by_case() -> None:
    assert answer_to_429({"code": "rate_limit_exceeded"}, {"retry-after": "1.2"}) == (429, None, {"Retry-After": "2"})
    assert answer_to_429({"code": "rate_limit_exceeded"}, {}) == (429, None, {})
    assert answer_to_429({"code": "rate_limit_exceeded"}, {"retry-after": "20"}) == (429, None, {"Retry-After": "20"})
    assert answer_to_429({"code": "rate_limit_exceeded"}, {"retry-after": "20.5"}) == (STOP_STATUS, LONG_WAIT_STOP, {})
    assert answer_to_429({"type": "insufficient_quota"}, {}) == (STOP_STATUS, QUOTA_STOP, {})


# --- The GPT client ---------------------------------------------------------------------------------------


def _answers(*responses: httpx.Response) -> tuple[Any, list[httpx.Request]]:
    seen: list[httpx.Request] = []
    queue = list(responses)

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return queue.pop(0)

    return handler, seen


def _limited(code: str | None = "rate_limit_exceeded", **headers: str) -> httpx.Response:
    return httpx.Response(429, json={"error": {"message": "slow down", "type": "requests", "code": code}}, headers=headers)


def _client(tmp_path: Path, handler: Any, waits: list[float]) -> OpenAIResponsesClient:
    settings = make_settings(tmp_path, openai_model=MODEL, openai_max_retries=0, openai_max_output_tokens=1_000)
    return OpenAIResponsesClient(settings, http_client=httpx.Client(transport=httpx.MockTransport(handler)), sleep=waits.append)


def test_gpt_waits_out_a_temporary_rate_limit_once_as_asked(tmp_path: Path) -> None:
    handler, seen = _answers(_limited(**{"retry-after": "2"}), httpx.Response(200, json=RESPONSE_BODY))
    waits: list[float] = []
    assert _client(tmp_path, handler, waits).create(REQUEST).id == "resp_123"
    assert waits == [2.0] and len(seen) == 2


def test_gpt_gives_up_after_two_waits_and_keeps_what_openai_said(tmp_path: Path) -> None:
    headers = {"x-request-id": "req_abc123", "retry-after": "3", "x-ratelimit-remaining-requests": "0", "x-ratelimit-reset-requests": "1s", "authorization": "Bearer sk-no"}
    handler, seen = _answers(_limited(**headers), _limited(**headers), _limited(**headers), httpx.Response(200, json=RESPONSE_BODY))
    waits: list[float] = []
    with pytest.raises(LLMError) as info:
        _client(tmp_path, handler, waits).create(REQUEST)
    error = info.value
    assert error.kind is LLMErrorKind.RATE_LIMIT and error.resumable and len(seen) == 3 and waits == [3.0, 3.0]
    assert error.facts() == {"status_code": 429, "code": "rate_limit_exceeded", "request_id": "req_abc123", "retry_after": 3.0,
                             "rate_limit": {"x-ratelimit-remaining-requests": "0", "x-ratelimit-reset-requests": "1s"}}  # fmt: skip
    assert FAKE_OPENAI_KEY not in json.dumps(error.facts()) and "sk-no" not in json.dumps(error.facts())


def test_gpt_does_not_wait_for_a_retry_after_beyond_the_cap_and_says_when_to_come_back(tmp_path: Path) -> None:
    handler, seen = _answers(_limited(**{"retry-after": "90", "x-request-id": "req_1"}), httpx.Response(200, json=RESPONSE_BODY))
    waits: list[float] = []
    with pytest.raises(LLMError) as info:
        _client(tmp_path, handler, waits).create(REQUEST)
    assert waits == [] and len(seen) == 1 and info.value.retry_after == 90.0 and info.value.kind is LLMErrorKind.RATE_LIMIT


@pytest.mark.parametrize("code", ["insufficient_quota", "billing_hard_limit_reached", "account_deactivated", "access_terminated"])
def test_gpt_never_retries_exhausted_credit_or_a_hard_limit(tmp_path: Path, code: str) -> None:
    handler, seen = _answers(_limited(code, **{"retry-after": "1"}), httpx.Response(200, json=RESPONSE_BODY))
    waits: list[float] = []
    with pytest.raises(LLMError) as info:
        _client(tmp_path, handler, waits).create(REQUEST)
    assert info.value.kind is LLMErrorKind.QUOTA and info.value.code == code and waits == [] and len(seen) == 1


def test_gpt_does_not_retry_a_429_that_does_not_say_it_is_temporary_nor_other_errors(tmp_path: Path) -> None:
    for response in (_limited(None), _limited("something_new"), httpx.Response(500, json={"error": {"code": "server_error"}}), httpx.Response(401, json={"error": {"code": "invalid_api_key"}})):
        handler, seen = _answers(response, httpx.Response(200, json=RESPONSE_BODY))
        waits: list[float] = []
        with pytest.raises(LLMError):
            _client(tmp_path, handler, waits).create(REQUEST)
        assert waits == [] and len(seen) == 1


def test_rate_wait_follows_the_same_bounds() -> None:
    limited = LLMError(LLMErrorKind.RATE_LIMIT, "m", code="rate_limit_exceeded", retry_after=5.0)
    assert rate_wait(limited, 0, 0.0) == 5.0 and rate_wait(limited, 2, 0.0) is None and rate_wait(limited, 1, 38.0) is None
    assert rate_wait(LLMError(LLMErrorKind.QUOTA, "m", code="insufficient_quota"), 0, 0.0) is None
    assert rate_wait(LLMError(LLMErrorKind.RATE_LIMIT, "m", code=None), 0, 0.0) is None


def test_the_facts_of_a_failure_reach_the_runs_event_log_and_the_report(tmp_path: Path) -> None:
    from app.orchestrator.fake_llm import FakeLLM

    def limited(_request: Any, _n: int) -> Any:
        raise LLMError(LLMErrorKind.RATE_LIMIT, "OpenAI rate limit", status_code=429, code="rate_limit_exceeded", request_id="req_7", retry_after=45.0, rate_limit={"x-ratelimit-remaining-tokens": "0"})

    env = tmp_path / "t.env"
    env.write_text(f"OPENAI_API_KEY={FAKE_OPENAI_KEY}\nOPENAI_MODEL=m\nOPENAI_PRICE_INPUT_PER_MTOK=2\nOPENAI_PRICE_OUTPUT_PER_MTOK=10\nDATABASE_URL=sqlite:///{(tmp_path / 'd.db').as_posix()}\nWORKSPACES_DIR={tmp_path / 'w'}\n", encoding="utf-8")
    report = run_planning(planning_settings(env), FakeLLM(limited), "Build something small.", label="FakeLLM (scripted)", ask=lambda _q: "", emit=lambda _l: None, memory_probe=lambda: None)
    (stop,) = report["stops"]
    assert stop["type"] == "llm.error" and stop["kind"] == "RATE_LIMIT" and stop["resumable"] is True
    assert (stop["status_code"], stop["code"], stop["request_id"], stop["retry_after"], stop["rate_limit"]) == (429, "rate_limit_exceeded", "req_7", 45.0, {"x-ratelimit-remaining-tokens": "0"})
    assert report["run"]["pause_reason"] == "llm_error:rate_limit" and report["usage"]["spent_usd"] == 0 and FAKE_OPENAI_KEY not in json.dumps(report)
