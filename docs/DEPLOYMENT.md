# DevPilot AI: deployment plan

**Status: proposed, not deployed.** No cloud account, integration, credential or manifest has been added. This page says what each way of running DevPilot needs, and what has to change before a public deployment.

## Four ways to run DevPilot

| | Offline scripted demonstration | Real local Docker execution | Real model-powered coding | Public cloud deployment |
|---|---|---|---|---|
| Command | `python -m app.demo.competition` | `python -m app.demo.mvp`, and the opt-in `RUN_DOCKER_TESTS` tests | `python -m app` with OpenCode enabled and approved | Not built |
| Real | REST API, orchestrator, gates, database, adaptive selection, attempt lifecycle, audit, verification, frozen acceptance suite, recovery, delivery, report | All of that, plus real Docker test runs | All of that, plus GPT and OpenCode | |
| Simulated | GPT (FakeLLM), coding providers, test execution (replayed recordings) | GPT and coding providers | Nothing | |
| Needs | The Python venv only. No Docker, no key, no network | Docker Desktop and `python:3.12-alpine`, approved each time | An OpenAI key, approved network, live approval; costs money | See below |
| Last verified | 2026-11-02, offline | 2026-10-31, before Stages 2 to 4 (historical evidence only). The Stage 2 to 4 code has not run on Docker | One live GPT call (2026-10-29); no model-written code | Never: not built |

## Proposed architecture

```
Browser ──> Vercel: the static dashboard (backend/app/ui)
                │  HTTPS, bearer token
                ▼
           Render: the API backend (FastAPI, orchestrator, selection, recovery, verification, reports)
                │  authenticated job queue (not built)
                ▼
           Secured Docker worker (a VM): the sandbox, the OpenCode containers, the gateway holding the model key
                │  egress allowlist: api.openai.com only
                ▼
           Model provider
```

## What runs where

| Feature | Vercel + Render alone | Needs the Docker worker |
|---|---|---|
| Dashboard | Yes: static files | |
| REST API, run history, reports | Yes | |
| Offline scripted demonstration (FakeLLM, simulated providers, replayed tests) | Yes: Python only | |
| GPT orchestration with a real model | Yes, with a key on Render | |
| Real test execution | No | Yes |
| Real coding with OpenCode | No | Yes |
| Independent acceptance on new code | No: only recorded fixtures replay | Yes |
| GitHub-assisted development, Google Drive-to-RAG (backlog, not built) | API side only | Cloning and testing code, and document parsing, would run on the worker |

**Render cannot host the sandbox as designed.** `DockerSandbox` and the OpenCode launcher call the Docker CLI on the backend's own host. Render web services do not give access to a Docker daemon, so Docker-in-Docker is not assumed. Execution has to move to a separate worker.

## What must change before a public deployment

These are not implemented:

1. **A remote sandbox.** `SandboxRunner` and `CodingLauncher` are already protocols. A remote implementation would send an approved `ExecSpec` to the worker over an authenticated queue and return its `ExecResult`. The worker keeps the hardening that exists today: no network for tests, read-only workspace, memory, CPU and PID limits.
2. **A network-facing API.** `DEVPILOT_HOST` accepts loopback addresses only (SECURITY T7: single user). Exposing the API needs TLS at a proxy, real user authentication in place of the single bearer token, rate limits, and a security review.
3. **A configurable API origin for the dashboard.** It calls `/api/v1` on its own origin. Served from Vercel, it needs the API's URL and `DEVPILOT_CORS_ORIGINS` set to the Vercel origin.
4. **Durable storage.** `DATABASE_URL` must be SQLite, and runs advance in one in-process worker. On Render that means one instance with a persistent disk, or a move to PostgreSQL (not built).
5. **Secrets placement.** The model key lives only on the worker's gateway, never on Vercel. Render needs the API token and, for real GPT orchestration, the OpenAI key.

## Requirements

| Part | Requirements |
|---|---|
| Vercel | Static hosting of `backend/app/ui`; the API URL; no secrets |
| Render | Python 3.12, `backend/requirements.lock`, one instance, a persistent disk for SQLite and workspaces, `DEVPILOT_API_TOKEN`, `DEVPILOT_CORS_ORIGINS`; `OPENAI_API_KEY` only for real GPT |
| Docker worker | Docker Engine with WSL2 or Linux, `python:3.12-alpine`, the OpenCode image built from the verified 1.19.5 binary, the egress allowlist enforced at the network level, a job API with its own credential |
