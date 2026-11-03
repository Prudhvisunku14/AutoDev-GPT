"""The `devpilot` command line: setup, doctor and build, over the same engine the dashboard and API use.

Nothing here generates code itself. `devpilot build` creates the same app the backend serves (GPT orchestrator, OpenCode behind
its filter and model gateway, the Docker sandbox, the spending limits, the verification engine) and drives it in-process.
"""
