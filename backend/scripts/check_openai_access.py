"""Check that OPENAI_API_KEY can see OPENAI_MODEL, without generating any tokens.

It calls GET /v1/models/{OPENAI_MODEL}, which is not billed. It does NOT prove that
quota is available: an exhausted quota only shows up on a generation request
(429 insufficient_quota). The key is never printed.

Run from the repo root after `. .\\scripts\\dev-env.ps1`:
    & $env:DEVPILOT_PYTHON backend\\scripts\\check_openai_access.py
"""

from __future__ import annotations

import sys

import openai

from app.config import Settings
from app.core.egress import EgressPolicy
from app.orchestrator.llm_client import DEFAULT_OPENAI_BASE_URL, llm_config_problem, map_openai_error


def main() -> int:
    settings = Settings()  # type: ignore[call-arg]  # reads the repo-root .env
    problem = llm_config_problem(settings, EgressPolicy.from_file(settings.egress_allowlist_file))
    if problem:
        print(f"Not configured: {problem}")
        return 1
    assert settings.openai_api_key is not None and settings.openai_model
    client = openai.OpenAI(
        api_key=settings.openai_api_key.get_secret_value(),
        base_url=settings.openai_base_url or DEFAULT_OPENAI_BASE_URL,
        timeout=30,
        max_retries=1,
    )
    try:
        model = client.models.retrieve(settings.openai_model)
    except openai.OpenAIError as exc:
        error = map_openai_error(exc)
        print(f"Model check failed ({error.kind}): {error.message}")
        return 2
    print(f"OK: the key can access model {model.id!r} (owned by {model.owned_by}). No tokens were used.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
