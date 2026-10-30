# CLAUDE.md: Working guide for Claude Code in this repository

## What this project is

**DevPilot AI** is a GPT-orchestrated platform for autonomous software development, built for a competition. It takes a natural-language requirement and produces a plan, code (via OpenCode), sandboxed execution, tests, automatic repair, documentation and a final report.

Design: [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md). Security: [docs/SECURITY.md](docs/SECURITY.md). Status: [docs/PROGRESS.md](docs/PROGRESS.md).

## Non-negotiable architecture rules

- **At runtime, GPT (OpenAI Responses API with function calling) is the only orchestrator.**
- Claude Code builds DevPilot. DevPilot must **never** call Claude or the Anthropic API. Do not add `anthropic` (or similar) dependencies to the delivered app.
- **GPT decides, code governs.** The three innovations live in code and are measurable:
  - adaptive tool selection: `backend/app/selection/`
  - failure classification and recovery: `backend/app/recovery/`
  - verification-guided development: `backend/app/verification/`

  They must not be implemented as prompt instructions. Prompts may describe them, but enforcement happens in the ToolExecutor.

## Workflow rules

- Build **one phase at a time**. The current phase is recorded in `docs/PROGRESS.md`. Do not implement future phases unless the user explicitly asks.
- Before coding, inspect the existing files and explain the proposed changes.
- Write automated tests for every important component. Run them and report the **actual** output. Never claim something works unless it was verified.
- At the end of every phase:
  - Update `docs/PROGRESS.md` with the files created, the tests run and their results, the remaining limitations and the next phase.
  - Update `docs/ARCHITECTURE.md` and `README.md` if the design or status changed.
- **Ask for approval before** any of the following:
  - destructive commands
  - installing system software
  - pushes or external deployments
  - anything that uses real credentials
  - live (paid) API tests

## Environment (personal laptop, Windows 11, PowerShell 5.1)

- **Machine:** personal laptop, Windows 11 Home (build 26100, 64-bit), Intel Core i5-1155G7 (4 cores, 8 threads), 15.6 GB RAM.
- **Project root:** `D:\DOWNLOAND_NEW_D\AI\AI`. Keep the project, the venv, `workspaces/`, `data/` and Docker data on D:.
- **Disk warning:** C: had about 0.3 GB free on 2026-10-29. Do not put project data, venvs, caches or Docker/WSL data on C:. Before Phase 3, move the Docker Desktop disk image and the WSL2 distro to D:. Freeing space on C: is the user's task; do not change it without approval.
- **Python 3.12.9 at `D:\python.exe`** (installed in the root of D:; `py -3.12` also finds it).
  - The project venv is `backend\.venv`, created from `D:\python.exe`. Never install packages globally.
  - **Do not use `python` from PATH.** It resolves to the Microsoft Store Python 3.11.9 (`...\WindowsApps\python.exe`), which is not the project interpreter.
- **Session setup (every new terminal):** from the repo root, run `. .\scripts\dev-env.ps1`. It points TEMP, TMP and the pip cache at `D:\DevPilotCache`, refuses pip installs outside a venv, and sets `$env:DEVPILOT_PYTHON` to the venv interpreter. It changes nothing outside the session.
- **Tests:** `cd backend; & $env:DEVPILOT_PYTHON -m pytest` (add `--cov=app` for coverage).
- **Installed as of 2026-10-29:**
  - Git 2.49.0 at `D:\Git\cmd\git.exe`. Global `user.name` and `user.email` are set. The repo is on branch `main`. Commits are local only; nothing is ever pushed without the user's approval. Commit history is recorded in `docs/PROGRESS.md`.
  - Node.js v24.14.1 and npm 11.11.0 at `D:\DOWNLOAND_NEW_D\`.
- **Not installed as of 2026-10-29:** Docker Desktop, WSL2 and OpenCode. Check before relying on any of them:
  - Phase 3 and later need Docker Desktop with WSL2, with its data on D:.
  - Phase 4 needs OpenCode. It runs in a container and is not installed on the host. The official `opencode-linux-x64-musl` 1.19.5 binary is cached (verified, not installed) at `D:\DevPilotCache\opencode\1.19.5`, with `manifest.json`. Never run it on the host. It needs `libstdc++`, which `python:3.12-alpine` lacks.
  - Phase 9 needs Node.js, which is present.
- **Inkscape:** not installed. Do not reference it.
- **PowerShell 5.1:** there is no `&&` or `||`. Chain commands with `;` and check `$?`.
- **System-level installs** (WSL2, Docker Desktop, Python, and anything that needs administrator rights) need the user's approval and are performed by the user, not by Claude Code.

## Security rules (see docs/SECURITY.md)

- Never hardcode credentials:
  - Configuration comes only from env or `.env`, via pydantic-settings.
  - `.env.example` holds placeholders only.
  - `.env` is gitignored.
- Never execute generated code on the host. Execution goes only through `SandboxRunner` (Docker), and unit tests use `FakeSandbox`.
- Every file access to a generated project goes through `WorkspaceFS`, which jails paths inside `workspaces/<project_id>/`.
- Secrets must never reach GPT prompts, tool results, logs, the database or sandbox environment variables.
- Push, GitHub, destructive and credentialed actions must pass through the PolicyEngine approval queue.
- Google Drive and GitHub are reached only through OAuth or PAT credentials and the approval queue. OAuth refresh tokens are encrypted at rest. Access tokens stay in memory. See [docs/SECURITY.md](docs/SECURITY.md) §7 and §10.
- Untrusted documents (PDF, DOCX, text) are parsed only in the no-network `document-parser` container. See SECURITY.md §8.
- Outbound network calls follow the default-deny egress allowlist. See SECURITY.md §9.

## Code conventions

- Python 3.12 with type hints everywhere.
  - Pydantic v2 models for all boundary data: API DTOs, tool arguments and tool results.
  - SQLAlchemy 2.0 typed ORM.
- Put every external service behind a `Protocol` interface with a fake for tests: `LLMClient`, `SandboxRunner`, `CodingProvider`, `ToolAdapter` and others.
- Tool-argument models must produce **OpenAI strict function schemas**:
  - `additionalProperties: false`
  - every property listed in `required`
  - optional values expressed as `X | None`
- Do not hardcode model IDs, prices or URLs. They come from settings.
- Keep modules small and follow the layout in `docs/ARCHITECTURE.md` §11.

## Testing

- Use `pytest`. External boundaries are faked:
  - `FakeLLM` (`app/orchestrator/fake_llm.py`) replays scripted function calls. `tests/harness.py` wires the real orchestrator around it. Never call the real OpenAI API from a unit or integration test.
  - `respx` mocks HTTP.
  - `FakeSandbox` replaces Docker.
- Opt-in markers: `docker`, `live_openai`, `live_opencode`, `live_n8n`, `live_google`, `live_github`. `tests/conftest.py` skips them unless the matching `RUN_LIVE_*` or `RUN_DOCKER_TESTS` flag is exactly `true` in the process environment. They need user approval each time.
- Never read, print or log the value of `OPENAI_API_KEY` or any other secret, including when checking whether it is set.
- Google, GitHub and document services are faked in unit tests (respx and fixtures). No unit test calls them live.
- Test algorithms on their math and with simulations, not only on happy paths.
