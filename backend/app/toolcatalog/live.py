"""Whether each tool is usable on this backend right now, from its running configuration only.

Nothing is contacted and nothing is started: these are the same checks the capabilities route and the launchers make
(GPT's configuration check, the coding launcher's own reasons, the sandbox the app was given, the website verifier's image
approval, and each integration's own problems). A tool backed by a fake reports `simulated`, never `ready`.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Literal

Status = Literal["ready", "simulated", "not_configured", "blocked", "planned"]
REAL_SANDBOX = "DockerSandbox"


@dataclass(frozen=True)
class Readiness:
    status: Status
    detail: str

    def to_dict(self) -> dict[str, str]:
        return {"status": self.status, "detail": self.detail}


def readiness(tool_id: str, state: Any) -> Readiness:
    check = _CHECKS.get(tool_id)
    if check is None:
        return Readiness("blocked", "DevPilot has no readiness check for this tool")
    return check(state)


def _gpt(state: Any) -> Readiness:
    problem = state.llm_problem()
    if problem is not None:
        return Readiness("not_configured", problem)
    if getattr(state, "scripted_llm", False):
        return Readiness("simulated", "GPT is replaced by scripted replies (FakeLLM) in this backend")
    return Readiness("ready", f"{state.gpt_label}; every call is bounded by the run's approved spending limit")


def _opencode(state: Any) -> Readiness:
    from app.coding.launcher import config_problems

    selector = state.coding_selector
    if selector is None:
        problems = config_problems(state.settings, state.egress)
        if problems:
            return Readiness("not_configured", "the coding engine is not connected. Still needed: " + "; ".join(problems))
        return Readiness("not_configured", "the settings are complete, but this backend was started without the coding engine: start it with scripts/live_server.py")
    options = tuple(getattr(selector, "options", ()))
    reasons = {option.name: option.launcher.unavailable_reason() for option in options}
    available = [option for option in options if reasons[option.name] is None]
    if not available:
        return Readiness("blocked", "; ".join(f"{name}: {why}" for name, why in reasons.items()) or "no coding provider is configured")
    real = [option.name for option in available if not option.simulated]
    if not real:
        return Readiness("simulated", "only a simulated coding provider is connected: " + ", ".join(option.name for option in available))
    return Readiness("ready", "connected: " + ", ".join(real))


def _docker(state: Any) -> Readiness:
    sandbox = state.sandbox
    if sandbox is None:
        return Readiness("not_configured", "no sandbox is configured in this backend, so nothing can run tests")
    kind = type(sandbox).__name__
    if kind != REAL_SANDBOX:
        return Readiness("simulated", f"tests are replayed or faked ({kind}), not run in Docker")
    return Readiness("ready", "Docker sandbox connected")


def _web_verifier(state: Any) -> Readiness:
    from app.verification.recipes import readiness as stack_readiness

    ok, why = stack_readiness("static-html")
    if not ok:
        return Readiness("blocked", why)
    sandbox = _docker(state)
    if sandbox.status != "ready":
        return Readiness(sandbox.status, sandbox.detail)
    return Readiness("ready", why)


def _n8n(state: Any) -> Readiness:
    runtime = getattr(state, "n8n", None)
    if runtime is None:
        return Readiness("not_configured", "N8N_ENABLED is not true in this backend")
    problems = runtime.problems()
    if problems:
        return Readiness("blocked", "; ".join(problems))
    if runtime.client.simulated:
        return Readiness("simulated", f"a simulated n8n ({runtime.client.name}) answers the triggers")
    return Readiness("ready", f"{len(runtime.catalog)} workflow(s) registered; reachability is shown by the n8n health check")


def _github(state: Any) -> Readiness:
    source = getattr(state, "repositories", None)
    if source is None:
        return Readiness("not_configured", "REPOSITORY_SOURCE is off in this backend")
    if "Fixture" in type(source).__name__:
        return Readiness("simulated", "repositories come from recorded fixtures, not GitHub")
    return Readiness("ready", "public repositories can be searched and read; private ones need the user's GitHub connection")


def _google_drive(state: Any) -> Readiness:
    runtime = getattr(state, "drive", None)
    if runtime is None:
        return Readiness("not_configured", "DRIVE_SOURCE is off in this backend")
    if getattr(runtime, "simulated", False):
        return Readiness("simulated", "a simulated Drive with fixture documents")
    authorizer = runtime.authorizer
    blocker = getattr(authorizer, "blocker", None)
    why = blocker() if callable(blocker) else None
    if why:
        return Readiness("blocked", why)
    return Readiness("ready", "Google consent is configured; each project still asks the user for its own folders")


_CHECKS = {"gpt": _gpt, "opencode": _opencode, "docker": _docker, "web_verifier": _web_verifier, "n8n": _n8n, "github": _github, "google_drive": _google_drive}
