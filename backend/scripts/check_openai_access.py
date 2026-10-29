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
from pydantic import ValidationError

from app.config import REPO_ROOT, Settings
from app.core.egress import EgressPolicy
from app.orchestrator.llm_client import DEFAULT_OPENAI_BASE_URL, llm_config_problem, map_openai_error

# This check needs only the OpenAI settings, so the API token is not required here.
_PLACEHOLDER_API_TOKEN = "model-access-check-placeholder-token"


def main() -> int:
    env_file = REPO_ROOT / ".env"
    if not env_file.is_file():
        print(f"Not configured: {env_file} does not exist. Copy .env.example to .env and fill it in.")
        return 1
    try:
        settings = Settings(_env_file=env_file, devpilot_api_token=_PLACEHOLDER_API_TOKEN)  # type: ignore[call-arg]
    except ValidationError as exc:
        print("The .env file has invalid values (values are not shown):")
        for err in exc.errors(include_input=False):
            print(f"  {'.'.join(str(p) for p in err['loc']).upper()}: {err['msg']}")
        return 1
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
        print(_HINTS.get(error.kind, ""))
        return 2
    print(f"OK: the key can access model {model.id!r} (owned by {model.owned_by}). No tokens were used.")
    return 0


_HINTS = {
    "AUTH": "Hint: the key is wrong, revoked, or has no access to this project. Re-copy it into .env (no quotes or spaces).",
    "MODEL_NOT_FOUND": "Hint: OPENAI_MODEL is misspelled or not enabled for this key. Use a model ID listed for your project.",
    "QUOTA": "Hint: the account or project has no remaining quota or billing.",
    "RATE_LIMIT": "Hint: too many requests right now; wait a minute and retry.",
    "TIMEOUT": "Hint: no answer within 30 s; check the internet connection or proxy.",
    "TRANSIENT": "Hint: network or server problem; retry shortly.",
}


if __name__ == "__main__":
    sys.exit(main())
