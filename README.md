# DevPilot AI

**A GPT-orchestrated platform for autonomous software development.**

You describe the software you want in plain language. DevPilot AI then:
- plans the work and picks a technology stack
- writes the code with OpenCode
- runs it in an isolated sandbox
- tests it against acceptance criteria derived from your requirement
- diagnoses and repairs failures within strict limits
- delivers the source code, documentation, test results and a final report

> **Status: the offline MVP is verified.** A requirement goes through the GPT function-calling loop, real OpenCode on real containers, tests in Docker and delivery, with GPT's replies and the model scripted. **No real model has been called through this path, and no paid run is authorized.**
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

### GPT orchestrator (Phase 2)

Offline demo. It uses a scripted FakeLLM, with no key and no cost:

```powershell
& $env:DEVPILOT_PYTHON backend\scripts\demo_calculator.py
```

To use real GPT, edit `.env` yourself:
- set `OPENAI_API_KEY` and `OPENAI_MODEL`
- optionally set `OPENAI_PRICE_INPUT_PER_MTOK` and `OPENAI_PRICE_OUTPUT_PER_MTOK`, so spend is tracked against `RUN_BUDGET_USD`

Then check that the key can see the model. This uses no tokens:

```powershell
& $env:DEVPILOT_PYTHON backend\scripts\check_openai_access.py
```

A run calls GPT only when you start it:

```powershell
Invoke-RestMethod http://127.0.0.1:8000/api/v1/runs/<run_id>/start -Method Post -Headers $h   # may cost money
Invoke-RestMethod http://127.0.0.1:8000/api/v1/runs/<run_id> -Headers $h                      # state, pause_reason
Invoke-RestMethod http://127.0.0.1:8000/api/v1/approvals -Headers $h                          # pending questions
```

Live events: `GET /api/v1/runs/<run_id>/events/stream` (server-sent events, same bearer header).

Live smoke test, which asks one question: can GPT submit a structured analysis? The default is a dry run that sends nothing. `--send` makes at most 2 calls and uses at most about 8,000 tokens:

```powershell
& $env:DEVPILOT_PYTHON backend\scripts\live_smoke_test.py           # dry run: shows the exact request
& $env:DEVPILOT_PYTHON backend\scripts\live_smoke_test.py --send    # paid; run only after approval
```

Docker and WSL2 setup for Phase 3: [docs/SETUP_WINDOWS.md](docs/SETUP_WINDOWS.md).

### Competition demonstration (offline, free)

Six scenarios through the real API, selection, verification, independent acceptance and recovery. GPT, the coding providers and test execution are simulated and labelled as such; no model, key, Docker or network is used. Test results in it are replayed recordings of exact files, not Docker runs.

What is offline-tested, what is scripted or replayed, what was verified on real Docker before Stages 2 to 4, what still awaits Docker or a real model, and what is not built: [docs/COMPETITION.md](docs/COMPETITION.md). Planned integrations (GitHub-assisted development, Google Drive-to-RAG): the product backlog in [docs/PROGRESS.md](docs/PROGRESS.md).

```powershell
cd backend
& $env:DEVPILOT_PYTHON -m app.demo.competition            # reports in data\competition-demo
& $env:DEVPILOT_PYTHON -m app.demo.competition --serve    # then a local read-only dashboard at /ui
```

### GitHub-assisted, multi-stack development (offline-tested)

DevPilot can search public repositories, judge their licences and evidence, import an approved one at a pinned commit into a component's folder, and plan projects with several stacks (for example a FastAPI back end and a React front end). Status, plainly:

- **Offline-tested:** repository discovery, evaluation and safe import (fixtures, never GitHub); multi-stack planning; the FastAPI and React verification recipes' contracts and parsers.
- **Not yet verified:** live GitHub access (off by default, `REPOSITORY_SOURCE=github`); the FastAPI and React recipes, whose runtime images are not built, so such projects stop with a precise blocked status; real code generation; real Docker execution of this code.

```powershell
cd backend
& $env:DEVPILOT_PYTHON -m app.demo.fullstack    # the offline React + FastAPI rehearsal; reports in data\fullstack-rehearsal
```

Details: [docs/MILESTONE_1_GITHUB.md](docs/MILESTONE_1_GITHUB.md).

## Documentation

- [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md): system design, orchestration protocol, algorithms, data model and APIs
- [docs/SECURITY.md](docs/SECURITY.md): threat model, sandboxing and approval policy
- [docs/PROGRESS.md](docs/PROGRESS.md): phase tracker, acceptance criteria and phase log
- [docs/COMPETITION.md](docs/COMPETITION.md): the offline demonstration, what is real and what is simulated, and feature status
- [docs/DEPLOYMENT.md](docs/DEPLOYMENT.md): the four ways to run DevPilot and the proposed cloud architecture (not deployed)
- [docs/MILESTONE_1_GITHUB.md](docs/MILESTONE_1_GITHUB.md): GitHub-assisted development (repository discovery, evaluation and safe import, offline-tested with fixtures), the weather application's specification, and what a first real run needs
- [CLAUDE.md](CLAUDE.md): rules for the AI development assistant used to build this project

## Acknowledgements

The design draws ideas from [OpenHands](https://github.com/OpenHands/OpenHands), [OpenCode](https://github.com/anomalyco/opencode), [n8n](https://github.com/n8n-io/n8n) and [n8n-nodes-opencode](https://github.com/ssccio/n8n-nodes-opencode). OpenCode and n8n are integrated through their documented interfaces. No implementation code was copied from these projects.
