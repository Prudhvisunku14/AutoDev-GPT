"""The optional integrations every entry point wires the same way: `python -m app`, `devpilot build` and scripts/live_server.py.

Each one is enabled only by its own setting, and is off by default:
- REPOSITORY_SOURCE=github: public repository search and import, each call approval-gated.
- DRIVE_SOURCE=google: Google Drive and the RAG tools, which fail closed until encryption and the parser are approved.
- N8N_ENABLED=true: signed n8n triggers, approval-gated unless N8N_AUTO_APPROVE=true.

Building them never contacts anything. Passing the result to `create_app(**integrations_for(...))` keeps the tool registry
and the routes identical across entry points for the same settings.
"""

from __future__ import annotations

from typing import Any

from app.config import Settings
from app.core.egress import EgressPolicy
from app.n8n.runtime import build_n8n_runtime
from app.repos.source import build_repository_source
from app.sandbox.runner import SandboxRunner
from app.tools.adapters.drive import build_drive_runtime


def integrations_for(settings: Settings, sandbox: SandboxRunner | None) -> dict[str, Any]:
    egress = EgressPolicy.from_file(settings.egress_allowlist_file)
    return {
        "repositories": build_repository_source(settings, egress),
        "drive": build_drive_runtime(settings, egress, sandbox),
        "n8n_runtime": build_n8n_runtime(settings, egress),
    }
