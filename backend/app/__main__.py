"""Start the backend: `backend\\.venv\\Scripts\\python.exe -m app`."""

from __future__ import annotations

import uvicorn

from app.coding.launcher import OpenCodeLauncher
from app.config import get_settings
from app.core.egress import EgressPolicy
from app.main import create_app
from app.sandbox.runner import DockerSandbox
from app.selection.selector import ProviderOption, ProviderSelector


def main() -> None:
    settings = get_settings()
    sandbox = DockerSandbox.from_settings(settings)
    # Off by default. Once enabled, the launcher itself refuses to start until a run is approved.
    coding = None
    if settings.opencode_enabled:
        launcher = OpenCodeLauncher(settings, sandbox.cli, EgressPolicy.from_file(settings.egress_allowlist_file))
        coding = ProviderSelector([ProviderOption(launcher.name, launcher, frozenset({"code_generation", "test_generation", "documentation"}))])
    # log_config=None keeps the redacting JSON handler installed by create_app.
    # access_log=False: uvicorn's access line includes the query string; the app's own
    # request log records the path only.
    uvicorn.run(
        create_app(settings, sandbox=sandbox, coding=coding),
        host=settings.devpilot_host,
        port=settings.devpilot_port,
        log_config=None,
        access_log=False,
    )


if __name__ == "__main__":
    main()
