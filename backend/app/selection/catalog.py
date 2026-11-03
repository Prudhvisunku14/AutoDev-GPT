"""The tools that really exist, what is known about each, and the policy that governs them.

Nothing here is invented to make a comparison look richer. There is one real coding provider. A number
that has never been measured stays None and is scored cautiously: no real model has run through OpenCode,
so its quality, duration and cost are unknown, and it is IMPLEMENTED rather than VERIFIED.

The simulated provider declares `python-cli`, `python-fastapi` and `react-vite`; OpenCode claims only `python-cli`, where it has been shown. Only `python-cli` has an approved verification runtime;
the others are blocked before selection until their runtime images are built and approved.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from app.coding.simulated import PROVIDER_NAME as SIMULATED_NAME
from app.selection.adaptive import AdaptiveSelector, EligibilityPolicy, ScoringConfig
from app.selection.model import BUILD, REPAIR, ToolProfile, ToolStatus
from app.selection.selector import ProviderOption, ProviderSelector, Selector

if TYPE_CHECKING:
    from app.config import Settings
    from app.core.egress import EgressPolicy

OPENCODE_NAME = "opencode_server"
CODING_CAPABILITIES = frozenset({"code_generation", "test_generation", "documentation"})
_ALL_CAPABILITIES = dict.fromkeys(CODING_CAPABILITIES, 1.0)
# What a tool can write. Whether that work can run is a separate gate: generate_code stops a run before any selection
# when a stack's verification recipe has no approved runtime (app/verification/recipes.py).
_DECLARED_STACKS = {"python-cli": 1.0, "python-fastapi": 1.0, "react-vite": 1.0}
# OpenCode claims only the stacks it has been shown working in (Stage 2). It gains one after a real run proves it.
_SHOWN_STACKS = {"python-cli": 1.0}
_KINDS = {BUILD: 1.0, REPAIR: 1.0}
DOCKER_INTERNAL, IN_PROCESS = "docker-internal", "in-process"
OPENAI_KEY_NAME, OPENAI_HOST = "OPENAI_API_KEY", "api.openai.com"
# The largest OpenCode request measured on real containers, 2026-10-31, and what the gateway adds around it.
_MEASURED_REQUEST_TOKENS = 16_690 + 512
# OpenCode 1 GB, API filter 256 MB, gateway 256 MB: the containers' own limits.
_OPENCODE_MEMORY_MB = 1_536
# An attempt is several requests, one for each tool call and one to finish. The first real run (2026-11-03) made 2 to 6 of them in an
# attempt that got anywhere, and the gateway reserves each request at its byte size, about five times what it really uses. An attempt
# that starts with room for fewer than this cannot be expected to finish, so it is not started: it would spend money to be rolled back.
_ATTEMPT_REQUESTS = 4

PLANNED_TOOLS = (
    ToolProfile("opencode_via_n8n", ToolStatus.PLANNED, _ALL_CAPABILITIES, _DECLARED_STACKS, _KINDS, environment="planned"),
    ToolProfile("gpt_writer", ToolStatus.PLANNED, _ALL_CAPABILITIES, _DECLARED_STACKS, _KINDS, environment="planned"),
)


def opencode_profile(settings: Settings) -> ToolProfile:
    """What is known about OpenCode through the gateway. The floor is the first request's reservation."""
    floor = None
    if settings.openai_price_input_per_mtok is not None and settings.openai_price_output_per_mtok is not None:
        floor = (_MEASURED_REQUEST_TOKENS * settings.openai_price_input_per_mtok + settings.gateway_max_output_tokens * settings.openai_price_output_per_mtok) / 1_000_000
    return ToolProfile(
        OPENCODE_NAME, ToolStatus.IMPLEMENTED, _ALL_CAPABILITIES, _SHOWN_STACKS, _KINDS, environment=DOCKER_INTERNAL, credentials=frozenset({OPENAI_KEY_NAME}), network=frozenset({OPENAI_HOST}),
        min_cost_usd=floor, min_tokens=_ATTEMPT_REQUESTS * _MEASURED_REQUEST_TOKENS + settings.gateway_max_output_tokens, memory_mb=_OPENCODE_MEMORY_MB,
    )  # fmt: skip


SIMULATED_PROFILE = ToolProfile(SIMULATED_NAME, ToolStatus.IMPLEMENTED, _ALL_CAPABILITIES, _DECLARED_STACKS, _KINDS, environment=IN_PROCESS, seconds=0.0, cost_usd=0.0, min_cost_usd=0.0, memory_mb=16)


def eligibility_policy(settings: Settings, egress: EgressPolicy) -> EligibilityPolicy:
    """From configuration only. A credential is checked for presence, and its value is never read."""
    credentials = {OPENAI_KEY_NAME} if settings.openai_api_key is not None else set()
    reachable = settings.coding_live_approved and egress.check("opencode", f"https://{OPENAI_HOST}/v1/responses").allowed
    return EligibilityPolicy(approved_environments=frozenset({DOCKER_INTERNAL, IN_PROCESS}), configured_credentials=frozenset(credentials), approved_network=frozenset({OPENAI_HOST}) if reachable else frozenset())


def build_selector(settings: Settings, egress: EgressPolicy, options: list[ProviderOption]) -> Selector:
    """`SELECTION_POLICY=fixed` keeps the old preference order, for comparison or as a way back."""
    if settings.selection_policy == "fixed":
        return ProviderSelector(options)
    return AdaptiveSelector(options, policy=eligibility_policy(settings, egress), config=ScoringConfig.from_settings(settings), planned=PLANNED_TOOLS)
