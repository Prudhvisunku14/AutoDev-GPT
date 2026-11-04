"""Start the backend: `backend\\.venv\\Scripts\\python.exe -m app`."""

from __future__ import annotations

import uvicorn

from app.coding.launcher import OpenCodeLauncher
from app.config import get_settings
from app.core.egress import EgressPolicy
from app.integrations import integrations_for
from app.main import create_app
from app.sandbox.runner import DockerSandbox
from app.selection.catalog import CODING_CAPABILITIES, build_selector, opencode_profile
from app.selection.selector import ProviderOption


def main() -> None:
    settings = get_settings()
    sandbox = DockerSandbox.from_settings(settings)
    # Off by default. Once enabled, the launcher itself refuses to start until a run is approved.
    coding = None
    if settings.opencode_enabled:
        egress = EgressPolicy.from_file(settings.egress_allowlist_file)
        launcher = OpenCodeLauncher(settings, sandbox.cli, egress)
        coding = build_selector(settings, egress, [ProviderOption(launcher.name, launcher, CODING_CAPABILITIES, profile=opencode_profile(settings))])
    # log_config=None keeps the redacting JSON handler installed by create_app.
    # access_log=False: uvicorn's access line includes the query string; the app's own
    # request log records the path only.
    uvicorn.run(
        # GitHub, Google Drive and n8n, each only if its own setting enables it (app/integrations.py).
        create_app(settings, sandbox=sandbox, coding=coding, **integrations_for(settings, sandbox)),
        host=settings.devpilot_host,
        port=settings.devpilot_port,
        log_config=None,
        access_log=False,
    )


if __name__ == "__main__":
    main()
