"""What the n8n tools and routes share: the client, the registered workflows, the signing secret and the replay guard.

`build_n8n_runtime` wires nothing unless N8N_ENABLED=true. Enabled, a missing secret or an empty workflow list does not
stop the app; it is reported by `problems()`, shown at GET /api/v1/integrations/n8n/status, and every trigger is refused
until it is fixed. A malformed workflow file does stop the app, like a malformed egress allowlist.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any
from urllib.parse import urlsplit

from app.config import Settings
from app.core.egress import EgressPolicy
from app.n8n.client import HttpN8nClient, N8nClient, N8nRefused, N8nUnavailable, check_base_url
from app.n8n.signing import ReplayGuard
from app.n8n.workflows import WorkflowCatalog

CALLBACK_PATH = "/integrations/n8n/callback"
RECENT_EXECUTIONS = 5


@dataclass
class N8nRuntime:
    client: N8nClient
    catalog: WorkflowCatalog
    secret: str | None
    callback_url: str | None = None
    auto_approve: bool = False
    config_problems: list[str] = field(default_factory=list)
    guard: ReplayGuard = field(default_factory=ReplayGuard)

    def problems(self) -> list[str]:
        found = list(self.config_problems)
        if not self.secret:
            found.append("N8N_WEBHOOK_SECRET is not set, so triggers cannot be signed and callbacks cannot be verified")
        if not len(self.catalog):
            found.append("no workflow is registered in N8N_WORKFLOWS_FILE")
        return found

    def health(self) -> dict[str, Any]:
        """Each registered workflow against n8n's own list: present, active, and its recent executions. Reads only."""
        report: dict[str, Any] = {"source": self.client.name, "simulated": self.client.simulated, "problems": self.problems(), "auto_approve": self.auto_approve,
                                  "callback_url": self.callback_url, "reachable": False, "error": None, "workflows": []}  # fmt: skip
        try:
            remote = self.client.workflows()
        except (N8nUnavailable, N8nRefused) as exc:
            report["error"] = str(exc)
            report["workflows"] = [{**spec.to_dict(), "found": None, "active": None} for spec in self.catalog]
            report["ready"] = False
            return report
        report["reachable"] = True
        by_path = {path: w for w in remote for path in w.webhook_paths}
        for spec in self.catalog:
            match = by_path.get(spec.path)
            entry: dict[str, Any] = {**spec.to_dict(), "found": match is not None, "active": bool(match and match.active),
                                     "workflow_id": match.id if match else None, "name": match.name if match else None, "recent_executions": []}  # fmt: skip
            if match is not None:
                try:
                    entry["recent_executions"] = [e.to_dict() for e in self.client.executions(match.id, limit=RECENT_EXECUTIONS)]
                except (N8nUnavailable, N8nRefused) as exc:
                    entry["executions_error"] = str(exc)
            report["workflows"].append(entry)
        report["ready"] = not report["problems"] and all(w["found"] and w["active"] for w in report["workflows"])
        return report


def check_callback_url(url: str) -> str:
    parts = urlsplit(url.strip())
    if parts.scheme not in ("http", "https") or not parts.hostname or parts.username or parts.password or parts.query or parts.fragment:
        raise ValueError("N8N_CALLBACK_URL must be an http or https URL with no credentials or query")
    if parts.path != CALLBACK_PATH:
        raise ValueError(f"N8N_CALLBACK_URL must end with {CALLBACK_PATH}")
    return url.strip()


def build_n8n_runtime(settings: Settings, egress: EgressPolicy, *, client: N8nClient | None = None) -> N8nRuntime | None:
    if not settings.n8n_enabled:
        return None
    catalog = WorkflowCatalog.from_file(settings.n8n_workflows_file)  # raises WorkflowConfigError: fail fast
    problems: list[str] = []
    base = check_base_url(settings.n8n_base_url)
    decision = egress.check("n8n", f"{base}/webhook/x")
    if not decision.allowed:
        problems.append(f"N8N_BASE_URL is not in the egress allowlist's n8n entry: {decision.reason}")
    callback = check_callback_url(settings.n8n_callback_url) if settings.n8n_callback_url else None
    if callback is None and any(spec.callback for spec in catalog):
        problems.append("a registered workflow reports back, but N8N_CALLBACK_URL is not set")
    secret = settings.n8n_webhook_secret.get_secret_value() if settings.n8n_webhook_secret else None
    if client is None:
        client = HttpN8nClient(base, egress, api_key=settings.n8n_api_key, timeout=settings.n8n_timeout_seconds, max_bytes=settings.n8n_max_response_bytes)
    return N8nRuntime(client, catalog, secret, callback, settings.n8n_auto_approve, problems)
