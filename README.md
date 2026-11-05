# DevPilot AI

**A GPT-orchestrated platform for autonomous software development.**

You describe the software you want in plain language. DevPilot AI then:
- plans the work and picks a technology stack
- writes the code with OpenCode
- runs it in an isolated sandbox
- tests it against acceptance criteria derived from your requirement
- diagnoses and repairs failures within strict limits
- delivers the source code, documentation, test results and a final report

> **Status.** The engine is verified offline, and a Python command-line application has been generated, tested in Docker and independently verified end to end by real GPT and OpenCode, from the terminal scripts (the second of two paid runs, $0.51 in all; the first failed and led to fixes). The new `devpilot` command drives that same engine; its flow is offline-tested and its installer was tried from a clean checkout, but **a real paid build through `devpilot build` has not been run yet**. Only **Python command-line applications** are supported: React, websites and full-stack generation are not, until they pass real end-to-end tests.
> See [docs/PROGRESS.md](docs/PROGRESS.md) for exactly what has been built and verified.

## Quick start (Windows)

```powershell
git clone https://github.com/Prudhvisunku14/AutoDev-GPT.git
cd AutoDev-GPT
.\setup.cmd
.\devpilot.cmd doctor
.\devpilot.cmd build
```

### Requirements

| | |
|---|---|
| **Operating system** | Windows 10 or 11 (the installer and the encrypted key store are Windows-only for now) |
| **Python** | **3.12**, exactly: [download](https://www.python.org/downloads/release/python-3129/) (3.13 is not supported) |
| **Docker Desktop** | installed **and running** (WSL2 backend): [download](https://www.docker.com/products/docker-desktop/). Every build runs the code and its tests in containers |
| **Git** | to clone and update: [download](https://git-scm.com/download/win) |
| **OpenAI API key** | **your own**, on an account **with credit**. A build spends your credit; DevPilot has no key and no credit of its own |
| **Connectivity** | internet during `setup.cmd` (PyPI, the npm registry, GitHub, Docker Hub and Alpine mirrors) and during every build (GPT and the model gateway reach `api.openai.com`). The containers that run the generated code and its tests have **no** network |
| **Disk** | roughly 2 GB free |
| **Spending** | every build is capped; the default is **$0.50** per build, GPT and OpenCode together, at most **$2.00** with `--max-usd`. You must type `approve 0.50` before anything is sent. The real cost cannot be known beforehand, only the limit, which is enforced in code before every model request. Your OpenAI invoice is the authority on what was billed |

`setup.cmd` checks all of this and tells you what is missing and how to fix it; it never installs system software for you.

### What is supported, and what is verified

- **Generated and verified: Python command-line applications**, new or starting from your own GitHub repository (public, or private with your token). A request is matched to a stack from its words or, when it names a repository, from that repository's own files; a stack DevPilot cannot yet verify is declined with the exact reason, before any approval or spending, and is never built as something else.
- **Websites (static HTML, React + Vite + TypeScript): inspected and verified, not yet generated.** `devpilot inspect <GitHub URL>` profiles a repository (framework, pages, routes, components, assets). `devpilot verify-site <folder or GitHub URL>` installs its dependencies through a registry-only proxy, type checks, builds, serves and checks it in a real headless browser, in Docker. Generating a website is declined until the build engine can run that verification itself: see [docs/WEBSITES_AND_REPOSITORIES.md](docs/WEBSITES_AND_REPOSITORIES.md) for exactly what works, what was verified and what remains. React + FastAPI, Next.js, Vue, Angular and mobile apps are declined.
- **Verified so far:** the engine and the installer, offline. The installer was run from a clean checkout and `devpilot doctor` passed 17 of 17. Real GPT and OpenCode generated, Docker-tested and independently verified a Python application from the older terminal scripts (the second of two paid runs, $0.51 in all; the first failed and led to fixes).
- **Not yet verified: a real paid build through the new `devpilot build` command.** Its flow is covered by offline tests with GPT scripted, the coding engine simulated and tests replayed, and it has never been run against the real OpenAI API. Treat your first build as that test: start with the default $0.50 limit.
- "Verified" for a free-form request means the generated project's own tests passed in the no-network Docker sandbox. There is no independent acceptance suite for it, and the ZIP's manifest says so.

**`setup.cmd`** (safe to run again) checks those requirements, creates a private Python environment in the folder (`.venv`), installs the pinned dependencies, then asks for your OpenAI key (typed hidden; stored **encrypted for your Windows account**, never in a file in this folder, never printed, sent only to OpenAI), your model name and your own per-token prices (they cap your spending). It then downloads OpenCode and ripgrep (both checked against pinned SHA hashes), builds the Docker runtime image, audits that image's contents offline, and runs the readiness check. It makes no OpenAI request.

**`devpilot build`** asks what to build, shows the spending limit and asks you to type `approve 0.50`. It then plans with GPT, writes the code with OpenCode, tests it in a no-network Docker sandbox, repairs failures within strict limits, and saves a verified ZIP and folder under `.\devpilot-output\`. Nothing is spent before you approve; the limit (default **$0.50** per build, at most $2.00 via `--max-usd`) is enforced in code before every model request.

```powershell
.\devpilot.cmd --help
.\devpilot.cmd doctor                        # checks this computer; says how to fix anything missing; never contacts OpenAI
.\devpilot.cmd build                         # interactive
.\devpilot.cmd build "a python tool that converts csv files to json, with unit tests"
.\devpilot.cmd build "..." --approve-spend 0.50   # approve without typing; the figure must equal the limit exactly
.\devpilot.cmd setup --rebuild               # build and audit the Docker image again
.\devpilot.cmd forget-key                    # delete the stored key

# GitHub repositories and websites (free: no OpenAI request)
.\devpilot.cmd setup --github-token          # for private repositories: a fine-grained token, Contents read-only (typed hidden, stored encrypted)
.\devpilot.cmd inspect https://github.com/<owner>/<repo>            # framework, stack, pages, components, assets
.\devpilot.cmd setup --web                   # once: build the website verifier and prove it on known good and broken sites (~2 GB download)
.\devpilot.cmd verify-site https://github.com/<owner>/<repo>        # install, type check, build, serve and browser-check it in Docker
.\devpilot.cmd verify-site C:\path\to\site   # the same for a folder (never modified: a disposable copy is checked)
.\devpilot.cmd build "Add a --verbose flag to this tool https://github.com/<owner>/<repo>" --branch main
```

More: [docs/INSTALL_WINDOWS.md](docs/INSTALL_WINDOWS.md) (troubleshooting, where files live, how the Docker image is verified) and [examples/prompts.md](examples/prompts.md).

## How it works

```
Requirement ─► GPT orchestrator (OpenAI Responses API, function calling)
                  │  function calls
                  ▼
             Tool Executor ── validates · enforces policy · selects tools ·
                  │             classifies failures · verifies results
                  ├─► OpenCode (code generation, edit-only)
                  ├─► Docker sandbox (install · run · pytest · Playwright)
                  ├─► GitHub (inspect named repos · import · gap analysis · PR or new repo, with approval)
                  ├─► Google Drive (OAuth, read-only, per-project grants)
                  ├─► n8n (signed workflow triggers and callbacks · notifications · run events)
                  └─► Documents: PDF, DOCX, text (parsed in a no-network container, embedded, indexed in SQLite per project)
```

**Prompt only.** Open the one-time sign-in link the launcher prints, type what you want, approve the run's spending limit, and approve anything else in small popups: a GitHub or Google connection the request needs, a pull request, a paid tool. Nobody pastes a token, picks a stack or names a tool. GPT chooses the stack from what this backend can verify, and reads the repositories and documents the request links to. The Tools page compares DevPilot's tools from recorded evidence, at no cost.

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

## Prerequisites for development

The command line needs only what the quick start lists. The rest of this README is for developing DevPilot itself:

- Python 3.12, Git, Docker Desktop with WSL2, an OpenAI API key
- Node.js LTS, for the dashboard in `frontend/`
- A Google Cloud OAuth client for Google Drive (only for that integration)

The command line never reads `.env`: its settings live in `%LOCALAPPDATA%\DevPilot` (or `DEVPILOT_HOME`). The API server (`python -m app`) and the developer scripts read `.env`; copy [.env.example](.env.example) and fill in your values. **Never commit `.env`.**

## Developer quickstart (Windows, PowerShell, backend only)

Run from the repository root. The commands keep temp files and caches on D:.

```powershell
# 1. Session environment: TEMP and the pip cache on D:, venv interpreter in $env:DEVPILOT_PYTHON
. .\scripts\dev-env.ps1

# 2. One-time: create the venv and install pinned dependencies
py -3.12 -m venv backend\.venv
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
- set `OPENAI_PRICE_INPUT_PER_MTOK` and `OPENAI_PRICE_OUTPUT_PER_MTOK`, so the backend can enforce `RUN_BUDGET_USD`

The API refuses to start a run while either price is unknown; a token limit alone is not represented as a dollar spending cap.

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

Manual planning from your own prompt, in the terminal. It uses the real orchestrator and gate checks, and stops once the plan is accepted. Nothing is generated or run. Run it from the `backend` folder:

```powershell
cd backend
& $env:DEVPILOT_PYTHON -m app.plan_cli           # dry run: shows the first request and its worst-case cost; sends nothing
& $env:DEVPILOT_PYTHON -m app.plan_cli --live    # paid: asks for your prompt, then for a typed "yes"
```

Live mode needs `OPENAI_API_KEY`, `OPENAI_MODEL` and both `OPENAI_PRICE_*` values in `.env`. Its limits are fixed in `app/plan_cli.py`:
- per run: at most 3 requests, $0.25 and 90,000 tokens, with no retries;
- across runs: $1.50, tracked in `data/planning/ledger.jsonl`.

Each run's full report is written to `data/planning/<run_id>.json`.

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
- [docs/WEBSITES_AND_REPOSITORIES.md](docs/WEBSITES_AND_REPOSITORIES.md): your own GitHub repositories (public and private), repository inspection, the website verifier and its network controls, and the capability status of website generation
- [docs/MILESTONE_1_GITHUB.md](docs/MILESTONE_1_GITHUB.md): GitHub-assisted development (repository discovery, evaluation and safe import, offline-tested with fixtures), the weather application's specification, and what a first real run needs
- [docs/COMPETITION_SCENARIOS.md](docs/COMPETITION_SCENARIOS.md): the truthful capability matrix. For each competition scenario: what is built, what has run live, and what it still needs
- [docs/ADDING_A_STACK.md](docs/ADDING_A_STACK.md): how a new framework is added (catalog entry, recipe, runtime image, approval) and which stacks are verifiable here
- [n8n/README.md](n8n/README.md): the signed n8n workflows, and running them on a local n8n
- [CLAUDE.md](CLAUDE.md): rules for the AI development assistant used to build this project

## Acknowledgements

The design draws ideas from [OpenHands](https://github.com/OpenHands/OpenHands), [OpenCode](https://github.com/anomalyco/opencode), [n8n](https://github.com/n8n-io/n8n) and [n8n-nodes-opencode](https://github.com/ssccio/n8n-nodes-opencode). OpenCode and n8n are integrated through their documented interfaces. No implementation code was copied from these projects.
