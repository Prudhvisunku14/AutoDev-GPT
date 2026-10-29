# DevPilot AI: Progress

**Current phase:** Phase 1 complete (2026-10-29). Phase 2 has **not started** and is waiting for explicit approval.

## Phase tracker

| # | Phase | Status | Prerequisites |
|---|---|---|---|
| 0 | Architecture and initialization | ✅ Done (2026-10-29, revised the same day) | none |
| 1 | Backend foundation | ✅ Done (2026-10-29) | Python 3.12.9 at `D:\python.exe` |
| 2 | GPT orchestrator | ⏳ Not started | OpenAI API key (user) |
| 3 | Docker sandbox | ⏳ Not started | Docker Desktop + WSL2 (data on D:) |
| 4 | OpenCode integration | ⏳ Not started | OpenCode container image, Git |
| 5 | Verification-guided development | ⏳ Not started | — |
| 6 | Failure classification and recovery | ⏳ Not started | — |
| 7 | Adaptive tool selection | ⏳ Not started | — |
| 8 | GitHub, Google Drive and n8n integrations, with document retrieval | ⏳ Not started | Google OAuth client, GitHub PAT, n8n container (user) |
| 9 | React frontend | ⏳ Not started | Node.js (installed) |
| 10 | Evaluation and competition demonstration | ⏳ Not started | all of the above |

## Acceptance criteria by phase

Each criterion must be **verified by running tests**, not claimed.

| # | Acceptance criteria |
|---|---|
| 0 | All docs exist and cover the architecture. `.env.example` has placeholders only. No dependencies are installed and no code is written. |
| 1 | `pytest` passes. Path traversal, absolute-path and symlink escapes are rejected. Secrets are redacted in logs. Unauthenticated requests get 401. The egress allowlist rejects unlisted hosts and private addresses. Core coverage ≥ 80%. |
| 2 | With a scripted FakeLLM, a run reaches `DEFINING_CRITERIA` with the analysis, plan and criteria persisted. Invalid arguments return a structured error. Approval pauses the run and it resumes. Turn and budget caps stop the run. The key never appears in the DB or logs. An opt-in live smoke test exists. |
| 3 | Docker-marked tests: a sample pytest suite runs in a container and is parsed. An outbound connection fails. A timeout kills the container. The memory limit is enforced. Unit tests never touch the host. The document-parser container has no network, and oversize or macro-enabled files are rejected. The install proxy allows only listed registries. |
| 4 | respx contract tests cover every OpenCode endpoint used. A live test generates a small FastAPI app and returns its diff. bash and external-directory edits are denied. Abort on timeout works. |
| 5 | Score math is unit-tested. Editing a frozen test triggers revert + POLICY_VIOLATION. `finish` is refused while any MUST fails. Test validation flags vacuous, flaky, untagged and unmapped tests, and blocks freezing them. A MUST test that passes before implementation is flagged. An end-to-end run with fakes goes from requirement to V. |
| 6 | Classifier accuracy ≥ 90% on a corpus of 40+ labeled logs. Every `decide()` branch is tested. An injected syntax error is repaired in ≤ 2 iterations (with fakes). |
| 7 | Score math is deterministic. In simulation the selector picks the p = 0.9 tool in ≥ 90% of selections after 50 trials. The breaker opens and closes. The breakdown is persisted. |
| 8 | Mocked API tests pass. HMAC rejects bad and stale signatures. Google OAuth uses PKCE and `state`, stores only encrypted refresh tokens, and never leaks tokens to logs or the DB. GitHub import rejects symlinks, submodules and oversize repos. PDF, DOCX and text documents are retrieved. Opt-in live n8n, Google and GitHub tests pass. A push or Drive import happens only after approval. Retrieval recall@5 ≥ 0.8. |
| 9 | vitest component tests pass. A Playwright e2e test covers create → watch run → approve → view report. |
| 10 | The stack starts with one command. The benchmark of about 10 requirements and 3 ablations produces a metrics table. A report is produced per project. The quickstart is verified from a clean checkout. |

## Environment prerequisites (checked 2026-10-29, personal laptop)

| Item | Status | Needed from |
|---|---|---|
| Operating system | Windows 11 Home, build 26100, 64-bit | — |
| Hardware | Intel Core i5-1155G7 (4 cores, 8 threads), 15.6 GB RAM | — |
| Disk | ❌ C: has about 0.32 GB free (critical, still unresolved). D: has about 149.8 GB free. Project data, the venv, temp files and caches are on D:. | Phase 3 |
| Project root | `D:\DOWNLOAND_NEW_D\AI\AI` (git repository on `main`, no commits yet) | — |
| Python 3.12 | ✅ 3.12.9 at `D:\python.exe` (root of D:). Venv at `backend\.venv`. The PATH `python` is still the Store's 3.11.9 and is not used. | Phase 1 |
| Dev caches | ✅ `D:\DevPilotCache\temp` and `D:\DevPilotCache\pip`, set per session by `scripts\dev-env.ps1` | Phase 1 |
| Git | ✅ 2.49.0 at `D:\Git\cmd\git.exe`. Global `user.name` is set. | Phase 1 |
| Node.js and npm | ✅ Node v24.14.1, npm 11.11.0, at `D:\DOWNLOAND_NEW_D\` | Phase 9 |
| Docker Desktop + WSL2 | ❌ neither installed. Installing them needs the user's administrator approval. | Phase 3 |
| OpenCode | ❌ not on the host. It runs in a container. | Phase 4 |
| Inkscape | ❌ not installed | — |
| Google Cloud OAuth client | ❌ not created (user action) | Phase 8 |
| OpenAI API key | not configured (user action; not touched) | Phase 2 |

---

## Phase log

### Phase 0: Architecture and plan (2026-10-29)

**Files created**
- `CLAUDE.md`: working rules for Claude Code in this repo.
- `README.md`: project overview and status.
- `docs/ARCHITECTURE.md`: full system design.
- `docs/SECURITY.md`: threat model and controls.
- `docs/PROGRESS.md`: this file.
- `.env.example`: configuration placeholders.
- `.gitignore`

**Tests run:** none. Phase 0 contains no code.

**Verification performed**
- Checked that all the files above exist.
- Scanned `.env.example` and the docs for strings that look like secrets (`sk-…`, `ghp_…`, `github_pat_…`, assigned non-empty key values) to confirm that only placeholders are present.

**Decisions**
- GPT, through the OpenAI Responses API with function calling, is the only runtime orchestrator. Claude is a development assistant only.
- "GPT decides, code governs": selection, recovery and verification are enforced by the ToolExecutor.
- OpenCode runs containerized with edit-only permissions (bash denied). All execution goes through the Docker sandbox.
- SQLite with SQLAlchemy 2.0, an in-process async run worker running one run at a time, and the Docker CLI driven by subprocess.
- `submit_analysis` was added as an explicit gate tool, giving a canonical requirement list (R-xxx) for criteria coverage. This is a refinement over the original plan.

**Remaining limitations**
- Nothing is runnable yet.
- Git, Docker, WSL2, Node and OpenCode are not installed. `git init` is deferred until Git is available.

### Phase 0 revision: environment and documentation corrections (2026-10-29)

**Files modified:** `CLAUDE.md`, `README.md`, `docs/PROGRESS.md`, `docs/ARCHITECTURE.md`, `docs/SECURITY.md`, `.env.example`.

**Changes**
- Recorded the personal laptop environment: Windows 11 Home, the project root `D:\DOWNLOAND_NEW_D\AI\AI`, and the low free space on C:.
- Corrected Python: Python 3.12 is required and is not yet installed. The old `Python312` path was not found, and the PATH `python` is Python 3.11.9.
- Corrected tool status: Git 2.49.0 and Node.js v24.14.1 are installed. Docker Desktop, WSL2 and OpenCode are not installed.
- Added to the design (not implemented): Google Drive OAuth 2.0; GitHub import and clone; PDF, DOCX and text ingestion in a no-network container; an extensible StackCatalog and explicit adapter registration; a default-deny network egress policy; and validation of generated tests and acceptance criteria.
- Aligned phase names with the approved roadmap. Phase 8 now includes Google Drive.

**Verification:** documentation-only change. No code written, no dependencies installed, no Docker or WSL2 commands run, no tests run.

**Next phase:** Phase 1, the backend foundation. It starts only on explicit request. Its prerequisite is Python 3.12, installed and recorded here.

### Phase 1: Backend foundation (2026-10-29)

**Environment:** Python 3.12.9 (`D:\python.exe`), venv `backend\.venv`, TEMP and pip cache in `D:\DevPilotCache`. Dependencies pinned in `backend/requirements.lock`: FastAPI 0.162.0, Starlette 1.10.0, Pydantic 2.14.1, pydantic-settings 2.16.2, SQLAlchemy 2.0.55, uvicorn 0.54.0, PyYAML 6.0.3, pytest 9.2.0, pytest-cov 7.1.0, httpx 0.28.1. Nothing installed globally.

**Components**
- **Configuration** (`app/config.py`): pydantic-settings from env or the repo-root `.env`. Validates a required API token of at least 32 characters, a loopback-only host, SQLite-only `DATABASE_URL`, the port range and the log level. Relative paths resolve against the repo root. Empty secrets mean "unset". Later-phase secrets are declared only so the redactor knows them.
- **Database** (`app/db/`): SQLAlchemy 2.0 typed models `projects`, `runs` (all §4.1 states) and `events`. SQLite in WAL mode with foreign keys on, `BEGIN IMMEDIATE` transactions, and UTC-aware timestamps (naive datetimes are refused). Tables are created with `create_all`.
- **Authentication** (`app/core/auth.py`): bearer token on every route except `/health`, constant-time comparison, always 401 with `WWW-Authenticate: Bearer`. Interactive docs and `openapi.json` are disabled.
- **Redaction** (`app/core/redaction.py`): masks configured secret values, common token formats (OpenAI, GitHub, Google OAuth, Bearer and Basic) and sensitive `key=value` pairs, recursively through dicts and lists. Applied to logs, event payloads, and project names and requirement text before storage.
- **Structured logging** (`app/core/logging.py`): one JSON line per record, redacted, including tracebacks. uvicorn logs go through it, and uvicorn's access log is off, so query strings are never logged.
- **Event storage** (`app/core/events.py`): append-only `events` with `seq` monotonic per project, assigned inside the INSERT. Each event is also logged.
- **Workspace jail** (`app/tools/adapters/workspace_fs.py`): `workspaces/<project_id>/` only. It rejects `..`, absolute, drive, drive-relative and UNC paths, alternate data streams, forbidden characters, reserved device names, trailing dots and spaces, and any symlink or junction component. Applies size and file-count limits. Listing never follows links.
- **Egress policy** (`app/core/egress.py`, `config/egress_allowlist.yaml`): default deny per component. HTTPS on port 443 unless a port is listed. Private, link-local, metadata and unlisted loopback addresses are refused, as are credentials in URLs and non-HTTP schemes. The allowlist is validated at startup.
- **API** (`app/api/`, prefix `/api/v1`): `GET /health`, `GET /status`, `POST/GET /projects`, `GET /projects/{id}`, `GET /projects/{id}/files`, `GET /projects/{id}/files/content`, `POST/GET /projects/{id}/runs` (one active run per project), `GET /runs/{id}`, `POST /runs/{id}/cancel`, `GET /runs/{id}/events`. Each request is logged with an `X-Request-ID`.
- **Entry point:** `python -m app` (`app/__main__.py`).
- **Dev tooling:** `scripts/dev-env.ps1` (session-only environment on D:).

**Files created:** `backend/pyproject.toml`, `backend/requirements.lock`; `backend/app/` `__init__.py`, `__main__.py`, `py.typed`, `config.py`, `main.py`, `api/{__init__,deps,health,projects,runs}.py`, `core/{__init__,auth,egress,events,logging,redaction}.py`, `db/{__init__,models,repositories,session}.py`, `schemas/{__init__,api}.py`, `tools/__init__.py`, `tools/adapters/{__init__,workspace_fs}.py`; `backend/tests/` `__init__.py`, `conftest.py`, `helpers.py`, `unit/{__init__,test_config,test_redaction,test_logging,test_egress,test_workspace_fs,test_db_and_events}.py`, `integration/{__init__,test_api_auth,test_api_projects_runs,test_secrets_and_startup}.py`; `config/egress_allowlist.yaml`; `scripts/dev-env.ps1`.

**Files modified:** `.gitignore` (key and credential files, logs, SQLite variants, coverage shards, build output), `CLAUDE.md`, `README.md` (status and quickstart), `docs/ARCHITECTURE.md` (API table, events columns, SQLite transactions), `docs/PROGRESS.md`.

**Tests run** (`cd backend; & $env:DEVPILOT_PYTHON -m pytest --cov=app`, 2026-10-29):
- **254 passed, 0 failed, 0 skipped**, 1 warning, in 8.4 s. The symlink tests ran on this machine.
- Coverage of `app`: **98%** (846 statements, branch coverage on). The only uncovered module is `app/__main__.py`, which the server smoke test below exercised.
- Warning: `StarletteDeprecationWarning: Using httpx with starlette.testclient is deprecated; install httpx2 instead`. It comes from the test client only, not the app.
- Bugs found and fixed while testing:
  - An egress entry without a port matched every port; it now means HTTPS on port 443 only.
  - Read-then-write SQLite transactions could fail with "database is locked" under concurrency. Fixed with `BEGIN IMMEDIATE`; a test runs 8 concurrent writers × 5 events.

**Acceptance criteria (verified by tests)**
- `pytest` passes ✅
- Path traversal, absolute-path, symlink and junction escapes are rejected ✅
- Secrets are redacted in logs; a test also checks the database files ✅
- Unauthenticated requests get 401, checked on 11 routes × 7 bad headers ✅
- The egress allowlist rejects unlisted hosts and private addresses ✅
- Core coverage ≥ 80% (actual 98%) ✅

**Server smoke test:** `python -m app` was started on `127.0.0.1:8765` with a throwaway token and data in `D:\DevPilotCache\temp\smoke`. Health returned 200, status without a token returned 401, project and run creation worked, `/status` reported `ok`, and the token did not appear in the server log.

**Remaining limitations**
- Runs are only recorded (state `CREATED` or `CANCELLED`). Nothing executes them until the Phase 2 orchestrator exists.
- `GET /runs/{id}/events` returns JSON; SSE streaming and the in-memory event bus come in Phase 2.
- The egress check is application-level only and does not resolve DNS. Container-level enforcement is Phase 3.
- WorkspaceFS checks links before each access but cannot rule out a race in which a link is created between the check and the write (TOCTOU). Only DevPilot itself writes to workspaces in Phase 1.
- If the database commit fails after a workspace directory was created, an empty directory is left behind.
- No schema migrations yet (`create_all`); Alembic comes once the schema stabilizes.
- C: still has only about 0.32 GB free. It does not block Phase 1 or 2, but it must be resolved before Docker in Phase 3.
- The git repository has no commits yet.

**Next phase:** Phase 2, the GPT orchestrator. It starts only on explicit approval and needs an OpenAI API key configured locally in `.env`.
