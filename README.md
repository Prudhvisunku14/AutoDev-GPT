# DevPilot AI

**A GPT-orchestrated platform for autonomous software development.**

You describe the software you want in plain language. DevPilot AI then:
- plans the work and picks a technology stack
- writes the code with OpenCode
- runs it in an isolated sandbox
- tests it against acceptance criteria derived from your requirement
- diagnoses and repairs failures within strict limits
- delivers the source code, documentation, test results and a final report

> **Status: Phase 1 (backend foundation) complete.** The REST API stores projects and runs, but runs are not executed yet; the GPT orchestrator arrives in Phase 2.
> See [docs/PROGRESS.md](docs/PROGRESS.md) for exactly what has been built and verified.

## How it works

```
Requirement ─► GPT orchestrator (OpenAI Responses API, function calling)
                  │  function calls
                  ▼
             Tool Executor ── validates · enforces policy · selects tools ·
                  │             classifies failures · verifies results
                  ├─► OpenCode (code generation, edit-only)
                  ├─► Docker sandbox (install · run · pytest · Playwright)
                  ├─► Git / GitHub (import · checkpoints · publish with approval)
                  ├─► Google Drive (OAuth, read-only, approval per import)
                  ├─► n8n (notifications · workflow triggers · OpenCode-via-n8n)
                  └─► Documents: PDF, DOCX, text (parsed in a no-network container, indexed in SQLite FTS5)
```

**GPT decides, code governs.** GPT is the central orchestrator: it plans the work and chooses which actions to take. Deterministic backend code validates every action, enforces safety and limits, and computes the platform's three core algorithms.

## Technical innovations

All three are implemented as measurable algorithms, not as prompt instructions.

1. **Adaptive tool selection.** Each candidate tool is scored on task suitability, historical reliability (a Bayesian Beta posterior), latency, cost and an exploration bonus. Every choice is stored with its score breakdown.
2. **Failure classification and recovery.** Failures are classified into a 13-class taxonomy and fingerprinted. A decision policy then chooses whether to retry, repair, switch tools, roll back, escalate or stop. It detects repeated errors and lack of progress.
3. **Verification-guided development.** Requirements become prioritized acceptance criteria, and tests are written first and then frozen. A weighted verification score gates completion. Edits to frozen tests are detected and reverted.

Details are in [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md).

## Technology

| Layer | Technology |
|---|---|
| Orchestration | OpenAI Responses API with function calling |
| Backend | Python 3.12, FastAPI, Pydantic v2, SQLAlchemy 2.0, SQLite |
| Coding engine | OpenCode (`opencode serve` HTTP API), containerized |
| Execution and testing | Docker sandbox, pytest, Playwright |
| Integrations | n8n, GitHub REST API and clone, Google Drive (OAuth 2.0, read-only), SQLite FTS5 retrieval over PDF, DOCX and text |
| Frontend | React + Vite + TypeScript |

## Prerequisites (for later phases)

- Python 3.12
- Docker Desktop with WSL2 (Phase 3 and later)
- Git
- Node.js LTS (Phase 9)
- A Google Cloud OAuth client for Google Drive (Phase 8)
- An OpenAI API key
- Disk: keep project data on D:. C: has limited free space on the current laptop.

Configuration lives in `.env`. Copy [.env.example](.env.example) and fill in your values. **Never commit `.env`.**

## Quickstart (Windows, PowerShell, backend only)

Run from the repository root. The commands keep temp files and caches on D:.

```powershell
# 1. Session environment: TEMP and the pip cache on D:, venv interpreter in $env:DEVPILOT_PYTHON
. .\scripts\dev-env.ps1

# 2. One-time: create the venv and install pinned dependencies
D:\python.exe -m venv backend\.venv
& $env:DEVPILOT_PYTHON -m pip install -r backend\requirements.lock -e "backend[dev]"

# 3. One-time: create .env from the template and set a long random API token
Copy-Item .env.example .env
& $env:DEVPILOT_PYTHON -c "import secrets; print(secrets.token_urlsafe(32))"   # paste into DEVPILOT_API_TOKEN

# 4. Run the tests
cd backend; & $env:DEVPILOT_PYTHON -m pytest --cov=app; cd ..

# 5. Start the API on http://127.0.0.1:8000
cd backend; & $env:DEVPILOT_PYTHON -m app
```

Try it from a second terminal:

```powershell
$h = @{ Authorization = "Bearer <your DEVPILOT_API_TOKEN>" }
Invoke-RestMethod http://127.0.0.1:8000/api/v1/health
Invoke-RestMethod http://127.0.0.1:8000/api/v1/projects -Method Post -Headers $h -ContentType 'application/json' `
  -Body '{"name":"Demo","requirement_text":"A small todo API"}'
```

The SQLite database is created at `data/devpilot.db` and project workspaces under `workspaces/`, both in the repo folder on D: and both gitignored. Interactive API docs (`/docs`) are disabled, because every route except `/health` needs the token.

## Documentation

- [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md): system design, orchestration protocol, algorithms, data model and APIs
- [docs/SECURITY.md](docs/SECURITY.md): threat model, sandboxing and approval policy
- [docs/PROGRESS.md](docs/PROGRESS.md): phase tracker, acceptance criteria and phase log
- [CLAUDE.md](CLAUDE.md): rules for the AI development assistant used to build this project

## Acknowledgements

The design draws ideas from [OpenHands](https://github.com/OpenHands/OpenHands), [OpenCode](https://github.com/anomalyco/opencode), [n8n](https://github.com/n8n-io/n8n) and [n8n-nodes-opencode](https://github.com/ssccio/n8n-nodes-opencode). OpenCode and n8n are integrated through their documented interfaces. No implementation code was copied from these projects.
