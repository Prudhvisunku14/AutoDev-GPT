# DevPilot AI: Progress

**Current state (2026-11-01):** the offline MVP is finished and verified. A requirement goes through the real API, orchestrator and audit, real OpenCode writes the files on real containers behind the real filter and gateway code, Docker runs the tests, and the tested files are delivered. GPT's replies and the model are scripted. **No real model has been called through this path, and no paid call is authorized.** A live run needs its own approval. Its proposal, with a $1.50 limit, is under "First real demonstration" at the end of this file.

**Stage 2 (2026-11-02):** adaptive tool selection is built in Python and tested offline. See "Stage 2: adaptive tool selection" near the end of this file.

**Stage 3 (2026-11-02):** intelligent failure recovery is implemented in Python and tested offline, but not validated on real failures. See "Stage 3: intelligent failure recovery" near the end of this file.

**Stage 4 (2026-11-02):** an offline competition demonstration of the whole workflow, independent acceptance verification, a three-configuration evaluation and a dashboard. Not validated with a real model or on real Docker in this stage. See "Stage 4" near the end of this file.

**Real-World Milestone 1 (2026-11-02):** GitHub-assisted development: public repository discovery, evaluation and safe import, offline-tested with fixtures. No live GitHub request, real model or Docker run; the weather application was not generated. See [MILESTONE_1_GITHUB.md](MILESTONE_1_GITHUB.md).

**Milestone 6 (2026-11-03, committed as `829b9da`):** Google Drive and a RAG chatbot builder, implemented and offline-tested with a simulated Drive, simulated consent and a test-double embedder. No Google server, real OAuth, paid call or Docker run; no chatbot was generated, and there is no working live Google Drive RAG chatbot. See [MILESTONE_2_GOOGLE_DRIVE_RAG.md](MILESTONE_2_GOOGLE_DRIVE_RAG.md).

**Milestone 6 hardening (2026-11-03, committed as `af5c576`):** OAuth security contracts, a fail-closed parser-isolation interface and detailed Drive approvals, offline only. Encryption is pending (`cryptography` is not installed), and the parser container is not built.

**First real build, run once and FAILED (2026-11-03), diagnosed and fixed offline, not re-run:** real GPT and OpenCode ran; task 1 completed, task 2 failed. All 18 refusals were DevPilot's own gateway, whose token budget (80,000 an attempt, chosen too tight against its byte-based reservations) ran out after 3 to 6 requests. OpenAI answered all 16 forwarded requests with 200. The gateway reported it as a 429, which the engine retried and recovery retried again; $0.27 of the $0.30 spent was rolled back. Counted cost $0.3798, nothing delivered, acceptance `NOT_RUN`. Fixes:
- **Gateway refusals:** a 402 with the reason, which the pinned engine does not retry. Its retry rule was read from the binary, ported and tested, and the port reproduces the run's timing.
- **Recovery:** a refused or rate-limited attempt is not automatically started again.
- **Start-up floor:** an attempt with room for fewer than 4 requests is not started.
- **Token limits:** no tighter than the dollars; modelled, the dollars bind first.
- **OpenAI's own 429s:** recorded; passed on for the engine's own bounded backoff, or ended when they are hard quotas or long pauses.
- **Fewer requests:** a one-task request for the experiment, and a brief line that asks for fewer steps. Details and what is still unknown: [FIRST_REAL_BUILD.md](FIRST_REAL_BUILD.md) §10. **No second paid run has been made or authorized.**

**Second real build, prepared and committed; the paid run not yet authorized (2026-11-03):** at most $0.50 more on top of the ledger's $0.379808, a typed `second run` confirmation, no paid retry by itself. See [FIRST_REAL_BUILD.md](FIRST_REAL_BUILD.md) §11.

**First real build, prepared (2026-11-03):** `backend/scripts/live_build.py` runs one paid build of a small Python attendance tracker (real GPT, real OpenCode, real Docker, the evaluator's frozen acceptance suite) with a strict $0.50 cap (a total across runs, counted from the start in a ledger) and a typed confirmation. **Prepared and offline-tested only: Docker was not started and no build request was made.** Docker's RAM target fails at the last reading (3.70 GB available, against 4 GB). Readiness, blockers and the ordered procedures: [FIRST_REAL_BUILD.md](FIRST_REAL_BUILD.md).

**Manual GPT planning CLI (2026-11-03, committed as `6297927`):** `python -m app.plan_cli`, run from `backend`. You type a prompt; it goes through the existing app, database, orchestrator, plan gate and approval queue, and the run stops once the plan is accepted.
- **What GPT can do:** it is offered only the analysis and planning tools.
- **Limits, all enforced in code before each request:**
  - 3 requests per run;
  - $0.25 per run against the worst case;
  - 90,000 tokens per run, which holds even if the prices are wrong;
  - $1.50 across runs, in a ledger;
  - no retries.
- **What it reports:** which coding tool build mode would choose for each task, and why, without starting anything.
- **Tested offline:** FakeLLM and a stand-in client, 21 tests.
- **First real run (user-triggered, 2026-11-03):** `gpt-6.1-sol` analysed "a college attendance management portal" into 32 requirements and planned 9 tasks on `python-fastapi`. Both passed the gates. 2 requests, 3,465 in / 3,196 out tokens, about $0.039 at the configured prices. Planning only: nothing was generated, and build mode is blocked (no FastAPI verification image, OpenCode not eligible for that stack).

Phase 4 (OpenCode) in progress. The foundation, the contract checks, the verified binary and the precedence experiment are committed (2026-10-30).

Phase 4D Round 1 (2026-10-31):
- The production container definition, the install guard and the A1/A2 lifecycle are tested and committed.
- **The guarded baseline against the real server passes: 22 PASS, 0 FAIL, 2 NOT VERIFIED (C13, unknown paths, which depend on the API filter, and C18, the ripgrep download path, which depends on a runtime image).**

Phase 4D Round 2 (2026-10-31): the adapter and the tool allowlist are built and tested, and the adapter's HTTP client reads the real server, read-only. They are committed as `53070e8`. Nothing calls OpenCode. Phase 3 is verified in real containers.

Phase 4D Round 3 (2026-10-31): the API filter and independent container termination are built and tested, and one isolated run through the filter against the real server passes (42 PASS, 0 FAIL). **C13 is verified at the filter, and the work is committed. Production network isolation is not proven.**

## Phase tracker

| # | Phase | Status | Prerequisites |
|---|---|---|---|
| 0 | Architecture and initialization | ✅ Done (2026-10-29, revised the same day) | none |
| 1 | Backend foundation | ✅ Done (2026-10-29) | Python 3.12.9 at `D:\python.exe` |
| 2 | GPT orchestrator | ✅ Done (2026-10-29), verified with FakeLLM; first live GPT call verified (analysis accepted, 1 call, 919 tokens) | OpenAI API key (user) for live use |
| 3 | Docker sandbox | 🟡 Isolation controls verified in real containers (2026-10-30); install proxy and orphan-sweep test on a real container still open | Docker Desktop running, data on D:, `python:3.12-alpine` present |
| 4 | OpenCode integration | 🟡 Foundation committed; binary verified; precedence verified; Phase 4D Stages 1–4 committed as `f4aa786` (2026-10-31). Stages 5 and 6 committed as `53070e8`: adapter, tool allowlist, read-only real-server check (28 PASS, 0 FAIL). Round 3 committed as `ab3f447`: API filter and independent termination, isolated filter run 42 PASS, 0 FAIL. MVP slice committed as `b7a0010`: gateway, launcher, disposable copy, `generate_code`, verification, recovery, delivery, report. Verified on real containers with a scripted model, and closed out on 2026-11-01. A real model, the published port and the route out have not run | Paid-call approval |
| 5 | Verification-guided development | 🟡 Tests in Docker judged against MUST criteria; Stage 4 adds a frozen, hidden evaluator suite with per-criterion evidence (offline-tested, replayed). No score | Docker for the acceptance run |
| 6 | Failure classification and recovery | 🟡 Stage 3 implemented and tested offline (2026-11-02), not validated on real failures. Simulated recovery 59.8% against 66.3% for the bounded baseline (55.7% as deployed), at lower cost. Escalation off, repeat limit 2. No real-failure dataset | Docker for the deferred tests, then real failure data |
| 7 | Adaptive tool selection | 🟡 Python-only Stage 2 built (2026-11-02): eligibility filter, utility ranking, verified history, fallback. One real provider, so no real comparison. Target not met in simulation, and the breaker is not built | Docker for the deferred tests |
| 8 | GitHub, Google Drive and n8n integrations, with document retrieval | 🟡 Milestone 1 (2026-11-02): public repository discovery, evaluation and safe import, multi-stack plans and framework recipes, offline-tested with fixtures; live GitHub not reached and recipes never executed. Google Drive offline (Milestone 6). n8n adapter built and offline-tested (2026-11-04): signed triggers, signed callback, health check; never run against a real n8n | `codeload.github.com` in the egress allowlist, approvals, a read-only token or a lower search limit; Google OAuth client; n8n container |
| 9 | React frontend | 🟡 Stage 4: a read-only dashboard in plain HTML and JavaScript at `/ui`, no packages. No React app | — |
| 10 | Evaluation and competition demonstration | 🟡 Stage 4: offline demonstration (six scenarios) and a pre-registered simulated evaluation. No real model, no Docker in this stage | Docker and model approval for a real demonstration |

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
| Project root | `D:\DOWNLOAND_NEW_D\AI\AI` (git repository on `main`; Phase 1 baseline committed as `63f1c89`; Phase 2 not committed yet; nothing pushed) | — |
| Python 3.12 | ✅ 3.12.9 at `D:\python.exe` (root of D:). Venv at `backend\.venv`. The PATH `python` is still the Store's 3.11.9 and is not used. | Phase 1 |
| Dev caches | ✅ `D:\DevPilotCache\temp` and `D:\DevPilotCache\pip`, set per session by `scripts\dev-env.ps1` | Phase 1 |
| Git | ✅ 2.49.0 at `D:\Git\cmd\git.exe`. Global `user.name` is set. | Phase 1 |
| Node.js and npm | ✅ Node v24.14.1, npm 11.11.0, at `D:\DOWNLOAND_NEW_D\` | Phase 9 |
| Docker Desktop + WSL2 | ❌ neither installed. Installing them needs the user's administrator approval. | Phase 3 |
| OpenCode | ❌ not on the host. It runs in a container. | Phase 4 |
| Inkscape | ❌ not installed | — |
| Google Cloud OAuth client | ❌ not created (user action) | Phase 8 |
| OpenAI API key | ❌ not configured: `.env` does not exist yet. The user enters `OPENAI_API_KEY` and `OPENAI_MODEL` personally; Claude Code never reads or prints the value. | Phase 2 live use |
| OpenAI SDK | ✅ `openai` 2.54.0 in the venv | Phase 2 |

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

### Phase 2: GPT orchestrator (2026-10-29)

**Baseline:** the Phase 1 suite was re-run before any change (254 passed). Phase 1 was then committed locally as `63f1c89` (50 files, after a secret scan and an ignore check). Nothing was pushed.

**New dependency:** `openai` 2.54.0 (official SDK), installed in the venv with caches on D:. `backend/requirements.lock` was refreshed.

**Components**
- **LLM client** (`orchestrator/llm_client.py`):
  - `LLMClient` protocol, and `OpenAIResponsesClient` on the official SDK's Responses API, with strict function tools, `parallel_tool_calls=false`, `tool_choice="required"` and `previous_response_id` chaining.
  - Model, base URL, timeout, retries and the output cap come only from settings. The base URL must pass the egress allowlist.
  - The SDK retries connection errors, 408, 409, 429 and 5xx with backoff. Remaining failures map to structured `LLMError` kinds: AUTH, QUOTA, RATE_LIMIT, MODEL_NOT_FOUND, TIMEOUT, TRANSIENT, BAD_REQUEST, CONFIG and UNEXPECTED.
  - The client is built lazily, so a missing key never blocks startup.
- **FakeLLM** (`orchestrator/fake_llm.py`): scripted function calls, text replies and errors. It records every request.
- **Tool registry** (`tools/registry.py`):
  - Converts Pydantic models to OpenAI strict schemas: references inlined, `additionalProperties: false` and all fields required at every level, `X | None` for optional values, and only portable keywords.
  - Registration refuses defaults, models that allow extra fields, bad names and duplicates.
  - Exposes tools per run state.
- **Policy engine** (`tools/policy.py`): READ, WORKSPACE_WRITE and SANDBOX_EXEC are allowed. EXTERNAL_NETWORK, GIT_PUSH and DESTRUCTIVE need approval. A tool outside its states, or any tool while the run is CREATED, waiting or terminal, is denied. `request_human_input` asks a person.
- **Tool executor** (`tools/executor.py`):
  - Validates the name, state, JSON and Pydantic arguments; validation errors never echo input values.
  - Runs the handler in a savepoint, so a failed or crashing tool leaves no partial rows.
  - Redacts results and truncates them to `TOOL_OUTPUT_MAX_CHARS`, always producing valid JSON.
- **Gate tools** (`tools/gates.py`):
  - `submit_analysis`: requires R-001… in order, with unique requirements.
  - `submit_plan`: requires a stack from `stacks/catalog.yaml`, known requirement references, every requirement covered, a language matching the stack, unique titles and at least one code_generation task.
  - `submit_criteria`: requires AC-001… in order, known references, every requirement referenced, no MUST criterion with the manual method, methods supported by the stack, and at least one MUST criterion.
  - `request_human_input`.
  - Every problem is reported at once.
- **State machine** (`orchestrator/state_machine.py`):
  - All 13 states, with forward edges only. AWAITING_APPROVAL resumes only the state it came from.
  - Phase 2 drives ANALYZING, PLANNING and DEFINING_CRITERIA. Later states pause with `phase_unavailable`.
- **Agent loop** (`orchestrator/loop.py`):
  - Exactly-once execution: a persistent call queue, a claim step, and INTERRUPTED recovery.
  - Outputs are delivered only after GPT accepts them, and no transaction is held during a GPT call.
  - Reminders for text-only replies; pauses on retryable LLM errors and on backend shutdown; a cancelled run discards the in-flight response.
  - Requirements are passed as escaped data blocks.
- **Limits** (`orchestrator/budget.py`):
  - MAX_TURNS, MAX_TOOL_CALLS, RUN_MAX_TOKENS, RUN_BUDGET_USD (when prices are known), active wall-clock time, MAX_REPEATED_TOOL_CALLS and MAX_NUDGES.
  - Unknown prices give `cost_known=false`, and the token cap applies instead.
- **Approvals** (`orchestrator/approvals.py`, `api/approvals.py`):
  - Atomic, decide-once decisions. The worker executes approved calls.
  - Denial returns APPROVAL_DENIED. Human answers are redacted. Cancel closes pending approvals.
- **Run worker** (`orchestrator/run_manager.py`): one run at a time, started by the app lifespan. Runs start only via `POST /runs/{id}/start` or an approval decision.
- **API:**
  - `POST /runs/{id}/start` (202, or 503 with the reason when OpenAI is not configured)
  - `GET /runs/{id}/analysis`, `/plan`, `/criteria`, `/invocations`
  - `GET /approvals`, `POST /approvals/{id}/decision`
  - `GET /runs/{id}/events/stream`: SSE with header-only auth, `Last-Event-ID` resume, heartbeats, `stream.end` and a time limit. The React client plan is in ARCHITECTURE §11.3.
  - `RunOut` gained `tool_calls`, `cost_known`, `active_seconds`, `pause_reason` and `status_detail`.
- **Data model:**
  - New tables: `requirements`, `tasks`, `acceptance_criteria`, `tool_invocations` and `approvals`.
  - New `runs` columns.
  - `init_db` now adds missing columns to an existing database when they are nullable or have a server default, and refuses otherwise.
- **Demo and tooling:**
  - `app/demo/calculator.py` and `backend/scripts/demo_calculator.py`: the offline demo.
  - `backend/scripts/check_openai_access.py`: a model-access check that generates no tokens.
  - `tests/live/test_openai_live.py`: the opt-in live smoke test.

**Files created**
- `backend/app/orchestrator/` `{__init__, llm_client, fake_llm, loop, state_machine, budget, prompts, run_control, approvals, run_manager}.py`
- `backend/app/tools/` `{base, registry, policy, executor, gates}.py`
- `backend/app/stacks/` `{__init__, catalog}.py`, `catalog.yaml`
- `backend/app/api/approvals.py`
- `backend/app/demo/` `{__init__, calculator}.py`
- `backend/scripts/` `{demo_calculator, check_openai_access}.py`
- `backend/tests/harness.py`
- `backend/tests/unit/` `{test_tool_registry, test_state_policy_budget, test_llm_client, test_gate_tools, test_orchestrator, test_phase2_infra}.py`
- `backend/tests/integration/test_api_orchestration.py`
- `backend/tests/live/` `{__init__, test_openai_live}.py`

**Files modified**
- `backend/app/config.py` (OpenAI, limits and SSE settings)
- `backend/app/db/models.py`, `backend/app/db/session.py` (additive migration)
- `backend/app/api/runs.py`, `backend/app/api/health.py` (phase string)
- `backend/app/main.py` (wiring and lifespan worker)
- `backend/app/schemas/api.py`
- `backend/app/core/redaction.py`: fixed the over-redaction of prose such as "basic arithmetic", which the demo exposed
- `backend/pyproject.toml`, `backend/requirements.lock`
- `backend/tests/conftest.py` (opt-in skip hook, more isolated env vars)
- `backend/tests/unit/test_redaction.py` (prose cases), `backend/tests/integration/test_api_projects_runs.py` (phase string)
- `.env.example`, `CLAUDE.md`, `README.md`, `docs/ARCHITECTURE.md`, `docs/SECURITY.md`, `docs/PROGRESS.md`

**Tests run** (`cd backend; & $env:DEVPILOT_PYTHON -m pytest --cov=app`, 2026-10-29):
- **432 passed, 0 failed, 1 skipped**, 1 warning, about 24 s with coverage. The suite was repeated 3 more times with identical results (no flakiness seen).
- The skipped test is `tests/live/test_openai_live.py`. It is opt-in, needs `RUN_LIVE_OPENAI_TESTS=true` and the user's approval, and has **not been run**.
- Coverage of `app`: **97%** (2,420 statements, branch coverage on).
- New Phase 2 tests: 177. That is 432 + 1 skipped - 256 Phase 1 tests (254 before, plus 2 new redaction cases).
  - `test_state_policy_budget` 47, `test_orchestrator` 28, `test_gate_tools` 24, `test_llm_client` 23, `test_api_orchestration` 21, `test_tool_registry` 18, `test_phase2_infra` 15, live 1.
- The warning is still Starlette's `httpx` → `httpx2` notice in the test client.
- **Bugs found and fixed while testing:**
  - `init_db` deadlocked: the inspector's connection held the `BEGIN IMMEDIATE` lock while a second connection tried to alter the table. Found by the Phase 1 tests.
  - `RunManager.join()` could return before a just-submitted run was queued, which also made a worker test pass without testing anything. Fixed, and the test now counts calls.
  - The redaction pattern masked "basic arithmetic". Found by the demo.
  - Tool-result truncation could cut through JSON. It now falls back to a minimal valid result.

**Acceptance criteria (verified by tests)**
- With a scripted FakeLLM, a run passes DEFINING_CRITERIA with the analysis, plan and criteria persisted, then pauses at IMPLEMENTING ✅
- Invalid arguments return a structured error (INVALID_ARGS, VALIDATION_FAILED, UNKNOWN_TOOL, POLICY_REFUSED) and the loop continues ✅
- Approval pauses the run, and it resumes after approve or deny. The approved action runs exactly once, and a double decision gets 409 ✅
- Turn, tool-call, token, budget, wall-clock, repeated-call and no-progress limits stop the run ✅
- The key never appears in GPT requests, the DB files or the logs ✅
- An opt-in live smoke test exists ✅ (first real run 2026-10-29, see the post-test review below)

**Demonstration (offline, FakeLLM): "Create a Python calculator application."** (`backend/scripts/demo_calculator.py`)
1. GPT analyzed the request into R-001 to R-005.
2. GPT chose a stack. A plan with stack `django` was **rejected by Python** ("unknown stack 'django'; choose one of …"); the corrected plan chose `python-cli` with 3 tasks.
3. GPT defined AC-001 to AC-005 (4 MUST unit criteria and 1 SHOULD static criterion), which Python accepted.
4. 31 events and 4 tool invocations were recorded.
5. The run stopped at IMPLEMENTING with `pause_reason=phase_unavailable`.

The calculator was **not** generated, run or tested.

**Live API:** not run. `.env` does not exist, so `OPENAI_API_KEY` and `OPENAI_MODEL` are not configured, and no paid call was made.

**Remaining limitations**
- `OPENAI_STORE_RESPONSES=false` (transcript replay) is not implemented; Phase 2 requires `true`. Responses are stored by OpenAI for chaining.
- Runs pause at IMPLEMENTING until Phases 3 and 4 exist.
- If GPT returns a function call that duplicates an earlier `call_id`, it is ignored and gets no output. That cannot happen with the real API, but if it did, the next request would be rejected.
- Paused runs resume only on request. There is no auto-resume and no scheduled retry for transient LLM errors; Phase 6 adds recovery policy.
- The SSE stream polls the events table every 0.5 s. There is no in-memory push, so latency is up to `SSE_POLL_SECONDS`.
- Cost is computed from configured prices only. With prices unset, spending is unknown and only the token cap applies.
- One process, one run at a time (by design).
- The unused `D:\DOWNLOAND_NEW_D\AI\.devpilot-cache` folder from the first session is still there, waiting for the user's decision.
- C: still has only about 0.4 GB free. This blocks Docker in Phase 3.

**Next phase:** Phase 3, the Docker sandbox. It starts only on explicit approval, and needs Docker Desktop with WSL2 installed by the user, with its data on D:, which requires freeing space on C: first.

### Phase 2 finalization (2026-10-29)

**Commits (local only, nothing pushed):**
- `042e6b9`: Phase 2, GPT orchestrator. 52 files. Before committing, the full suite passed (432 passed, 1 skipped), the staged content was scanned for secrets, and every ignore rule was checked by exit code.
- A follow-up commit holds the items below. Its ID is reported in the session summary.

**Security fix: settings errors echoed values.**
- A probe with fake values showed that a too-short `DEVPILOT_API_TOKEN` was printed verbatim in the validation error, so a typo in `.env` would have put the token in the startup traceback.
- Fixed with `hide_input_in_errors=True` in `Settings`, plus a regression test with 3 cases.
- `check_openai_access.py` no longer needs `DEVPILOT_API_TOKEN`. It prints "FIELD: problem" lines without values, plus a hint for each error kind.

**Live smoke test prepared, not run** (`app/demo/live_smoke.py`, `backend/scripts/live_smoke_test.py`, `tests/live/test_openai_live.py`):
- Requirement: "Create a simple Python calculator application."
- Limits enforced in code by `SmokeGuardClient`:
  - at most 2 OpenAI calls, with SDK retries off
  - ANALYZING requests only (it stops once the analysis is accepted)
  - a second call only if the first used ≤ 2,500 tokens
  - `max_output_tokens` 2,000 per call and `RUN_MAX_TOKENS` 8,000
  - a request is refused before sending if any configured secret appears in it
- The estimated worst-case total is about 7,500 tokens.
- The dry run (the default) prints call 1 and sends nothing: about 700 estimated input tokens, the tools `request_human_input` and `submit_analysis`, and only the public requirement sentence.
- 7 offline tests in `tests/unit/test_live_smoke.py` cover the guard: one call on success, one retry on rejection, never a third call, no retry after an expensive first call, a secret refused before sending, and the dry run.

**Tests:** 442 passed, 1 skipped (the live test, not run), 0 failed.

**OpenAI configuration:**
- `.env` exists, is ignored by git and is not tracked.
- `OPENAI_API_KEY`, `OPENAI_MODEL` and `DEVPILOT_API_TOKEN` are still empty, so the model-access check reports "OPENAI_API_KEY is not set". No network call was made.

**Phase 3 readiness:** see [SETUP_WINDOWS.md](SETUP_WINDOWS.md).
- WSL is not installed, and neither is Docker.
- Virtualization is active (a hypervisor is present).
- C: has 0.43 GB free; about 15 GB are needed. D: has 149.7 GB free.
- About 7 GB of regenerable caches on C: are listed there for the user to review.

### Phase 2 post-test review: first live GPT call (2026-10-29)

**Live result (one paid call, reviewed from the run's stored events):**
- Model: `gpt-6.1-sol`, as set in `.env` (the key is not recorded anywhere).
- API calls: 1. Tokens: 543 input + 376 output = 919, against the 8,000 cap.
- Tool call: `submit_analysis`, accepted by validation on the first attempt. No retry, no second call.
- Requirements: R-001 to R-008.
- Run: 1 turn, 1 tool call, state PLANNING. The guard stopped it after the analysis, as designed.
- Cost: not computed. `OPENAI_PRICE_*` is not set in `.env`, so `cost_known` is false and only the token cap applied.

**What `llm_error:config` meant.** The configuration was fine. The guard refused the planning request on purpose by raising `LLMError(CONFIG)`. The orchestrator treats every `LLMError` as a resumable error, so it recorded `llm.error` (kind CONFIG) and paused with `llm_error:config`. The smoke summary then hid that event, because the message contained "smoke test", so the printout listed no OpenAI error and only said `pause: llm_error:config`. That was a bug in the harness: a deliberate stop looked like a broken configuration.

**Fix:**
- New error kind `LLMErrorKind.STOPPED`: the caller refused to send the request. It is not a provider or configuration fault.
- The orchestrator records `llm.stopped` and pauses the run with `llm_stopped`. It records no `llm.error` for a deliberate stop.
- The smoke guard and the dry run raise `STOPPED`.
- The smoke summary no longer filters events by message text. `llm_errors` holds only real errors; `llm_stops` holds deliberate stops.
- The CLI prints "Stopped on purpose by the smoke guard (not an error)".

**Tests** (FakeLLM only, no network, no cost):
- 3 new tests. A deliberate stop pauses as `llm_stopped` with no error event. A real CONFIG error still pauses as `llm_error:config` with an error event. A genuine config error inside the smoke harness is reported as an error.
- The smoke unit tests now assert `llm_stopped`, an empty `llm_errors`, and the `STOPPED` kind on a refused request.
- The opt-in live test asserts that the run ends at `llm_stopped`.
- Full suite: 445 passed, 1 skipped (the live test, not run), 0 failed. Coverage 97%.

**Remaining Phase 2 issues:**
1. Cost is unknown until `OPENAI_PRICE_INPUT_PER_MTOK` and `OPENAI_PRICE_OUTPUT_PER_MTOK` are set. Until then `RUN_BUDGET_USD` is not enforced; the token cap still applies.
2. Only the analysis step has run against the live model. Planning (`submit_plan`) and criteria (`submit_criteria`) are tested only with FakeLLM. Checking them live needs a new paid approval.
3. A third-party deprecation warning (Starlette's test client asks for `httpx2`) appears in test runs. It is not from our code, and nothing was changed for it.
4. Earlier smoke databases remain under `D:\DevPilotCache\temp` (`devpilot-smoke-*` and `smoke\`). They are temp files and can be removed only with your approval.

### Phase 3: Docker sandbox (2026-10-29): built offline, real-container checks pending

**Docker verification: FAILED.** I made no container, no image pull and no Docker change. The checks:

| Check | Result |
|---|---|
| Docker CLI | Version 29.9.2 at `C:\Users\prudh\AppData\Local\Programs\DockerDesktop\resources\bin\docker.exe`. **Not on PATH**, so the backend needs `DOCKER_CLI` set to that path. |
| Docker Desktop program | Version 4.97.0, installed to **C:** (`C:\Users\prudh\AppData\Local\Programs\DockerDesktop`). The program needs about 3.3 GB on C:. `D:\DockerDesktop` exists but is empty. |
| Engine | **Not running**: no Docker process, no `docker_engine` pipe, and `docker version` cannot reach the server. |
| First start | Never done. Docker has no settings file, so no data location is set and none is confirmed on D:. |
| Data location | `D:\DockerData` is empty. Without the setting, Docker's VM disk would go under `%LOCALAPPDATA%\Docker` on C:. |
| Installer log | Two attempts failed for lack of C: space (needed 3.2 GB). The third ran `install -package … -vm-package …` with no `--installation-dir` or `--wsl-default-data-root`, so the program went to C:. The D: temp redirect was not used either. |
| docker-users group | Not present. The elevated installer normally creates it. |
| WSL | Version 3.0.3.0, default version 2, no distributions yet (expected before Docker's first start). |
| Disk | C: 1.42 GB free of 183 GB. D: 148.8 GB free of 293 GB. |

**Steps only you can do** (I have not done any of them):
1. Decide where Docker's program lives. Option A: uninstall Docker Desktop and reinstall it to D: as in `docs/SETUP_WINDOWS.md` step 5, in an administrator PowerShell. Option B: keep the program on C: (about 3.3 GB) and move only the data to D: after the first start. Option B needs about 5 GB free on C: first. Tell me which you choose; I will not reinstall without your permission.
2. Start Docker Desktop once from `C:\Users\prudh\AppData\Local\Programs\DockerDesktop\Docker Desktop.exe`. It may ask for administrator approval to install its service. Approve it yourself. Sign-in is optional.
3. In Settings → Resources → Advanced, set **Disk image location** to `D:\DockerData` (Option B; under Option A this is already the install setting). Then run `docker info` and check `Docker Root Dir`.
4. Add the CLI path to `.env` (not a secret): `DOCKER_CLI=C:\Users\prudh\AppData\Local\Programs\DockerDesktop\resources\bin\docker.exe`. Or put Docker on PATH yourself.
5. Only after step 3 is confirmed, run `docker pull python:3.12-alpine`. It is a small official image, and the download goes to the disk location set in step 3.
6. Then tell me. I will run `docker version`, the real-container tests and `demo_sandbox.py --run`.

**What was built (offline):**
- `app/sandbox/`: `policy.py` (approval table and request checks), `spec.py` (job and result types), `docker_cli.py` (the only code that knows docker's syntax, including the hardened `create` arguments), `process.py` (subprocess with timeout, cancel and output cap), `runner.py` (create, start, inspect, remove, with status mapping and orphan sweep), `fake.py` (test double).
- `app/tools/adapters/sandbox.py`: the GPT tool `run_sandbox_command`, registered only when a sandbox is supplied.
- `ToolContext` gained the sandbox, the workspace and a cancel signal. The orchestrator passes them on, and backend shutdown cancels running containers.
- `create_app` takes an optional `sandbox`. `python -m app` supplies `DockerSandbox`; tests do not, so tests never touch Docker.
- Settings: `DOCKER_CLI`, `SANDBOX_MEMORY`, `SANDBOX_CPUS`, `SANDBOX_PIDS_LIMIT`, `SANDBOX_TIMEOUT_SECONDS` (already in `.env.example`) and `SANDBOX_MAX_OUTPUT_BYTES`.
- `backend/scripts/demo_sandbox.py`: the default is a dry run, which prints the exact `docker create` arguments and starts nothing. `--run` goes through the real tool and prints the outcome and the recorded `sandbox.*` events. Only the dry run has been executed.
- Tests: `test_sandbox_policy.py`, `test_sandbox_process.py` (real child processes), `test_sandbox_docker_args.py`, `test_sandbox_runner.py` (fake CLI), `test_sandbox_tool.py`, and `tests/integration/test_sandbox_docker.py` (9 real-container tests, skipped unless `RUN_DOCKER_TESTS=true`; **not run**).

**Tests (offline):** 528 passed, 10 skipped (9 real-container tests and the live OpenAI test), 0 failed. Coverage 97%. ruff and mypy are not installed in the venv, so they were not run.

**Remaining Phase 3 issues:**
1. Real-container checks are not run (see the steps above). Until they pass, the isolation controls are unverified (see SECURITY.md §11).
2. The install step with a package-registry proxy is not built. Only `--network none` exists.
3. Images are pinned by tag (`python:3.12-alpine`), not digest. Record the digest after the first pull.
4. The tool is not reachable from GPT yet, because the orchestrator pauses at IMPLEMENTING until Phase 4.
5. A per-run cancel from the API is not wired. Only backend shutdown cancels containers.
6. An empty folder `D:\DOWNLOAD_NEW_D` was created by a mistyped path during this work. It holds nothing, and the tools refused to delete it. Delete it by hand.

### Phase 3 finalization: real Docker verification (2026-10-30)

**Docker environment (checked by the engine and disk files, not by Docker Root Dir alone):**

| Item | Result |
|---|---|
| Docker Desktop program | **D:**: `D:\DockerDesktop\Docker Desktop.exe`. Nothing remains under `C:\Users\prudh\AppData\Local\Programs\DockerDesktop`. |
| Docker CLI | `D:\DockerDesktop\resources\bin\docker.exe`, on PATH, version 29.9.2. `.env` now has `DOCKER_CLI=D:\DockerDesktop\resources\bin\docker.exe` (a path, not a secret). |
| Engine | Running. `docker version`: server 29.9.2, OS linux, amd64. Pipe `\\.\pipe\docker_engine` present. Driver overlayfs. |
| Linux containers through WSL2 | Yes. WSL distro `docker-desktop` (VERSION 2) has `BasePath = \\?\D:\DockerData\main`. |
| Disk image location | **On D:.** `D:\DockerData\disk\docker_data.vhdx` (about 1.48 GB) and `D:\DockerData\main\ext4.vhdx` (about 104 MB). No Docker VHDX was found under C:. |
| Docker's own Root Dir | `/var/lib/docker`, inside the VM. It does not show the host location, so the VHDX files and the WSL base path were used as proof. |
| WSL | 3.0.3.0. Default version 2. No `.wslconfig`, so WSL uses its defaults. |
| Free space after setup | C: 6.70 GB, D: 144.02 GB. |

**Image:** `python:3.12-alpine` pulled once (authorized). Digest `sha256:e6dae589c1c05976188fd3bd019afd091445fc533ed2ea29001beddc7368a9c8`. No other image was downloaded.

**Real container tests (`RUN_DOCKER_TESTS=true`): 9 passed, 0 failed, 0 skipped.**
- Python version runs and the container is removed.
- The workspace script runs and prints `14`.
- No network: the probe prints `blocked`. A control with `--network bridge` prints `connected`, so the block is real.
- Read-only workspace: the write fails. The file is not created.
- Non-root: uid 65534, gid 65534. `CapEff` is all zeros.
- Memory: a 512 MB allocation under a 128 MB limit is OOM-killed (`oom_killed`).
- Timeout: a 60-second sleep is killed at 3 seconds. No container is left.
- Output limit: 8000 characters are truncated to 1024 and marked.
- Cancel: a running container is killed after 2 seconds. No container is left.

**Hardened container audit (`docker inspect` on a freshly created container):** NetworkMode `none`, Privileged `false`, ReadonlyRootfs `true`, CapDrop `[ALL]`, SecurityOpt `[no-new-privileges]`, Memory and swap both 128 MB, PidsLimit 64. The audit container was removed.

**Demonstration (`demo_sandbox.py --run`), exit 0:**

| Command | Status | Exit | Duration | Output | Cleanup |
|---|---|---|---|---|---|
| python-version | SUCCEEDED | 0 | 1093 ms | `Python 3.12.16` | removed |
| python-script calc.py | SUCCEEDED | 0 | 1000 ms | `14` then `sandbox arithmetic ok` | removed |

Recorded events: `sandbox.started` and `sandbox.finished` for each command, with the container name, image, status, exit code, duration, and cleanup. No host path is recorded.

**Full suite (with `RUN_DOCKER_TESTS=true`):** 537 passed, 1 skipped (the live OpenAI test, which stays off), 0 failed. Coverage 97%. Without the Docker flag: 528 passed, 10 skipped.

**Open items:**
1. The full run took about 7.5 minutes, while the suite without the Docker tests took about 40 seconds and the nine Docker tests alone took about 24 seconds. The cause is not yet known. It is not a failure, but it should be checked before Phase 4 adds more container tests.
2. The startup sweep of leftover containers is tested only against the fake CLI. It has not been run on a real leftover container.
3. The install step with a registry-only proxy (T13) is not built. The install step cannot run until then.
4. ruff and mypy are still not installed in the venv.
5. `tests/docker_fake.py` was reported as changed on disk. Git shows no difference from the committed version, so no change is pending.

**Readiness:** Phase 3 is verified on real containers for every isolation control in the table in SECURITY.md §11, except the orphan sweep and the install proxy. Phase 4 has not started and waits for your approval.

### Phase 4 foundation checkpoint (2026-10-30)

Phase 4 is **not complete**. This checkpoint commits the parts that are built and tested. Nothing calls OpenCode yet: there is no `generate_code` tool, no container and no model route.

**OpenCode research (from the published opencode-ai 1.19.5 SDK and docs, 2026-10-30):**
- The latest release is 1.19.5.
- The public docs and the SDK disagree on the permission reply endpoint, so the adapter reads the server's own `/doc` at run time.
  - The SDK has `POST /permission/{id}/reply` with `{reply}`.
  - The docs have `POST /session/{id}/permissions/{id}` with `{response}`.
- A project `opencode.json` or `.opencode/` folder overrides global configuration and can add plugins or MCP servers, which run code. Only managed configuration (`/etc/opencode/`) cannot be overridden.
- The API includes command-running endpoints (`/session/{id}/shell`, `/session/{id}/command`) and a per-request `directory` parameter.

**Committed and tested:**

| Component | File | Tests |
|---|---|---|
| OpenCode policy: managed config, effective-config check (last-match-wins wildcards, agent overrides, list and object rule forms), protected paths | `app/coding/opencode_policy.py` | `test_opencode_policy.py` |
| Task brief from the saved plan, with untrusted text fenced | `app/coding/brief.py` | `test_coding_brief.py` |
| Workspace snapshot, change audit (protected paths, links, size, executables, secrets, file count, invalid names), checkpoint and exact restore | `app/coding/audit.py` | `test_coding_audit.py` (real files, symlinks and junctions) |
| Provider contract types | `app/coding/types.py` | used by the above |
| Slow tools run with no database transaction open (`ExternalStep`), per-run cancel watcher, recovery hook, late results stored as `DISCARDED` | `tools/base.py`, `tools/executor.py`, `tools/registry.py`, `orchestrator/loop.py`, `db/models.py` | `test_external_tools.py` |
| `run_sandbox_command` moved to the transaction-free path | `tools/adapters/sandbox.py` | `test_external_tools.py`, existing sandbox tests |
| Settings for OpenCode and checkpoints (OpenCode is off by default) | `config.py`, `.env.example` | existing config tests |

**Bugs found and fixed:**
1. **SQLite write lock held during tools (Phase 3).** Every tool handler ran inside the orchestrator's write transaction, so a sandbox run over 30 s would have made API writes, such as a cancel, fail with "database is locked". A test shows that a handler tool still holds the lock and that an external tool does not.
2. **A cancelled run kept its tool running.** A per-run watcher now stops slow tools when the run is cancelled. A test cancels a run through the real API while a tool runs.
3. **Late results.** A result that finished after its run ended was recorded as an ordinary result. It is now `DISCARDED`, never sent to GPT, and no GPT call follows.
4. **A GPT call after shutdown.** A backend shutdown during a slow tool was followed by one more GPT request (a paid call with the real client). The loop now checks the stop flag after the tool queue.
5. **Test isolation.** The new `OPENCODE_*` settings were not cleared from the developer's environment in tests.

**Not committed (kept in the working tree):**
- `app/tools/adapters/opencode.py`, the OpenCode HTTP adapter: **0% tested**. Its HTTP-mock tests were stopped by a safety filter during generation and were not retried.
- The egress allowlist line `127.0.0.1:4096` for the backend. It only serves that adapter.

**Tests:** 637 passed, 10 skipped (the 9 Docker tests and the live OpenAI test, all opt-in), 0 failed. Coverage: 84% including the untested adapter, 96% without it. Run time varied between 43 s and 115 s for the same suite on this laptop.

**Remaining Phase 4 work:** see [ARCHITECTURE_OPTIMIZATION.md](ARCHITECTURE_OPTIMIZATION.md) for the P0 items.
1. Agree how to test the adapter (P0 1).
2. Confirm `/doc` on a real server (P0 2).
3. Build the model gateway (P0 3).
4. Build the per-project OpenCode container launcher (P0 4).
5. Add `call_id` to the tool context (P0 5).
6. Build the `generate_code` and `submit_implementation` tools, the `coding_tasks` table and the IMPLEMENTING prompt, then wire them into `create_app`.
7. Write the offline demo, then do a live test after approval.

### Phase 4B: OpenCode contract and security review (2026-10-30)

This stage was read-only investigation and preparation. No OpenCode server was started, nothing was downloaded, no image was pulled and no paid call was made. Full findings are in [ARCHITECTURE_OPTIMIZATION.md](ARCHITECTURE_OPTIMIZATION.md), section "Phase 4B".

**Findings:**
- **Versions.** 1.19.5 is still the stable release (npm `latest`, GitHub Latest, GHCR `latest`). A 2.x line exists on GitHub and GHCR with changed permission checks and a new configuration layer. The adapter keeps refusing 2.x.
- **`/doc` format is unknown.** The docs call it both an OpenAPI 3.1 spec and an HTML page. The adapter accepts JSON only, so it fails closed.
- **Contracts are inferred, not verified.** Every operation the adapter needs appears in the published 1.19.5 SDK types. The permission-reply endpoint differs between the docs and the SDK. Nothing has been confirmed on a server.
- **Static review of the untested adapter.** Two P0 defects:
  - A1: a prompt whose send fails ambiguously is not aborted.
  - A2: an abort is not confirmed before the workspace is restored.

  There are also several P1 items. The adapter file was not changed.
- **Managed config protects only the keys it sets.** OpenCode merges configuration key by key, so a project could add MCP servers, agents or patterns. The workspace pre-check, the effective-config check and the audit cover this. The build agent's rules are now pinned in managed config.

**Committed and tested in this stage:**
- `app/coding/opencode_contract.py` with 65 tests. It contains the adapter's pure checks:
  - health and version range
  - `/doc` classification (HTML is refused, never scraped)
  - OpenAPI operation checks, with the permission-reply choice and an inventory of sensitive operations
  - a size-capped SSE parser
  - workspace path containment
  - a tool allowlist
  - the configuration check

  The adapter does not use this module yet.
- `managed_config` pins the build agent's permission rules (2 new tests).
- `ToolContext.call_id`: tools get the invocation's call id, the same key `recover` receives (test added).

**Still not committed:** `app/tools/adapters/opencode.py` (untested) and the egress line `127.0.0.1:4096`. The line is safe, but it is premature until the launcher and gateway design is fixed.

**Next, needs your approval:** the real-server contract check in ARCHITECTURE_OPTIMIZATION.md. It downloads the official 1.19.5 musl binary (about 0.3 GB on D:), runs it with no network, no credentials and no prompts inside the existing `python:3.12-alpine` image, and then cleans up.

### Phase 4C: official OpenCode binary verified; real-server check prepared (2026-10-30)

**Not done in this stage:**
- No OpenCode server was started and no check container was run.
- No image was built or pulled, and no paid call was made.
- The adapter is still **untested** (0%) and unchanged. Full details are in [ARCHITECTURE_OPTIMIZATION.md](ARCHITECTURE_OPTIMIZATION.md), section "Phase 4C".

**Done:**
- **Download** (authorized): `opencode-linux-x64-musl@1.19.5` from registry.npmjs.org.
  - The 79,167,835-byte tarball matches the published SHA-512 integrity, the value pinned in 4B, and the SHA-1 shasum.
  - Both archive entries passed the safety checks before unpacking.
  - Binary: `D:\DevPilotCache\opencode\1.19.5\extracted\package\bin\opencode`, 221,590,952 bytes, SHA-256 `528058cf…acc8`. Manifest beside it. 0.30 GB on D:. It was never executed.
- **Finding:** the binary needs `libstdc++.so.6` (from its ELF headers), and `python:3.12-alpine` (Alpine 3.24.3) has neither `libstdc++` nor `libgcc`. I checked this by reading a `docker save` export on the host, with no container. A Dockerfile for a check image (the pinned base plus those two packages) is written, **not built**.
- **Prepared and dry-run, not executed:**
  - `backend/scripts/opencode_contract_check.py` (the host runner)
  - `backend/scripts/opencode_check/probe.py` (the in-container probe: standard library only, allowlisted requests, no prompts)
  - the check container: no network, no port, read-only root, no capabilities, user 65534, limits, the binary mounted read-only, a fresh workspace, and a fresh password passed by name
  - the inert precedence test
- **New finding, tested offline:** a project `opencode.json` that names keys first fixes the merged key order. Its later wildcard rules (for example `b*sh: allow`) then beat managed denials under last-match-wins. DevPilot's effective-config check catches this. Run 2 measures what the real server does.

**Files:**

| File | Status |
|---|---|
| `app/coding/opencode_artifact.py`: registry metadata, SHA-512 and archive checks, ELF reader, manifest | New, 42 tests |
| `app/coding/opencode_check.py`: check container arguments, `docker inspect` isolation check, image provenance, report analysis (C0–C14), precedence analysis (P1–P5) | New, tested in `test_opencode_check.py` |
| `app/coding/opencode_policy.py`: `ruleset_problems` for one reported rule set | Changed, 1 test |
| `scripts/fetch_opencode.py` | New; ran once (the download) |
| `scripts/opencode_contract_check.py`, `scripts/opencode_check/probe.py`, `scripts/opencode_check/image/Dockerfile` | New; dry run only. The probe's allowlist and helpers are unit-tested without a server. |

**Tests:** 799 passed, 10 skipped (the 9 Docker tests and the live OpenAI test, all opt-in), 0 failed. Coverage is 86% including the untested adapter (0%, 497 statements) and 96% without it.

**Prepared plans** (ARCHITECTURE_OPTIMIZATION.md):
- the A1 and A2 fixes, with 10 regression tests that use a `SessionControl` fake (no HTTP mocking)
- the API filter and model gateway: interfaces and a per-project topology
- the final network architecture: the `127.0.0.1:4096` line stays uncommitted until the filter publishes there

**Not committed:** everything in this stage, as well as the adapter and the egress line from earlier.

**Next, each step needs your approval:**
1. Build the check image (apk downloads about 1.5 MB).
2. Run the baseline check.
3. After a passing baseline, run the precedence check.

### Phase 4C: check image built, baseline run executed (2026-10-30)

**Approved and done:** the image build, one baseline run and the offline tests.

**Not done:** the precedence test, `--allow-tmp-exec`, prompts, OpenAI and the gateway. Details are in [ARCHITECTURE_OPTIMIZATION.md](ARCHITECTURE_OPTIMIZATION.md), section "Phase 4C baseline run".

**Image.** `devpilot/opencode-check:1.19.5`:
- ID `sha256:c380fdb5d0cb5cd9a59322029b665bd9c484d7bf861e0d9842893ecde0470bd6`
- the local pinned base plus one layer, user 65534
- `libgcc 15.2.0-r5` and `libstdc++ 15.2.0-r5` only, installed with `--no-scripts` and signatures checked
- about 4.9 MB of new data on D:

**Baseline.** 7.7 s, cleaned up. **Not passed:** of 21 checks, 18 PASS, 1 FAIL and 2 NOT VERIFIED. That run's first report said 17 PASS, a counting mistake corrected from the saved summary.

**Confirmed on the real server:**
- the isolation, through `docker inspect`
- version 1.19.5
- basic auth required everywhere tested, including `/event` and `POST /session`
- `/doc` is **JSON** (OpenAPI 3.1.0, 162 paths)
- session create (rules echoed exactly), get, status (idle means absent), messages, abort (200 `true`), and unknown id (404)
- `server.connected` as the first SSE event
- the build agent's merged rules pass
- 59 sensitive operations inventoried, none called
- both permission-reply forms present
- `messageID` accepted
- the workspace untouched

**C8 FAIL.** `GET /config` re-orders permission keys, so DevPilot's last-match-wins check cannot use it. The minimum fix is to judge rule order from `GET /agent`, which passed (C9).

**NOT VERIFIED:**
- **C12:** the unreviewed tools `apply_patch`, `invalid` and `question`. GPT models edit through `apply_patch`, which the adapter does not allow yet.
- **C13:** unknown paths serve the web UI, so the API filter must refuse them.

**Incident.** The first attempt completed and cleaned up, but the runner crashed while printing, before saving. The runner was fixed to save first, and the identical run was repeated.

**Tests:** 803 passed, 10 skipped, 0 failed. Coverage is 96% without the adapter and 86% with it (the adapter is 0%).

**Commit:** none. The baseline did not fully pass. All Phase 4C work is preserved in the working tree, as are the adapter and the egress line.

**Still open:** A1 and A2 (plan ready, not applied). The adapter is untested.

### Phase 4C fix round: effective-policy check corrected, baseline passes (2026-10-30)

**Approved and done:** the C8 fix, the tool review, the reporting fixes, one baseline re-run, the offline tests and this commit.

**Not done:** the precedence test, prompts, OpenAI, the gateway and Phase 4D. Details are in [ARCHITECTURE_OPTIMIZATION.md](ARCHITECTURE_OPTIMIZATION.md), section "Phase 4C fix round".

**What changed:**
- **C8 fix.** Allow and deny decisions are judged only from the ordered rules in `GET /agent`. `GET /config` is checked where order does not matter (settings, declared rules as key/value sets, disabled agents). Without `/agent` the check fails closed. Unreviewed permission names, wildcard permission names that allow, and external-folder allows other than OpenCode's own tool-output folder all fail.
- **Tools** (read from the 1.19.5 binary). `edit`, `write` and `apply_patch` ask the `edit` permission.
  - `apply_patch` names only each change's source path, so a move's destination escapes the edit rules. DevPilot's audit of every changed path catches it (new test).
  - Minimum allowlist: read, glob, grep, edit, write, apply_patch, todowrite, invalid.
- **C12 is now verified (read-only).** Every listed tool is reviewed, and the enforced rules allow or deny and hide it as reviewed.
- **C13 stays NOT VERIFIED.** Unknown paths get 200 with the web interface; refusing them is the API filter's job, and the filter is not built.
- **Reporting.** Evidence is saved before printing, in UTF-8, atomically. The console replaces characters it cannot encode, and analysis errors are saved with a bounded reason. Tested against a strict cp1252 console.

**Baseline re-run.** Same image (ID matched; not rebuilt), same binary and same hardening. **20 PASS, 0 FAIL, 1 NOT VERIFIED (C13).** Changes from the previous run: C8 FAIL → PASS and C12 NOT VERIFIED → PASS. 7.1 s, cleaned up. Evidence is in `D:\DevPilotCache\opencode-check\reports\20261030T074203Z-*`.

**Tests:** 828 passed, 10 skipped, 0 failed, in 59.6 s. Coverage is 97% without the adapter and 86% with it (the adapter is 0%, 497 statements).

**Committed:** Phase 4C's tested modules, scripts, tests and docs. **Not committed:** `tools/adapters/opencode.py` (untested) and the `127.0.0.1:4096` egress line (premature).

**Still open:**
- A1 and A2 (plan ready, not applied)
- the adapter's own tests, and wiring it to the new checker, `/agent` and the allowlist
- the API filter and the model gateway
- the precedence run (needs approval)

### Phase 4C configuration-precedence experiment (2026-10-30)

**Approved and done:** the two precedence runs. **Not done:** prompts, OpenAI, Phase 4D and any commit. Details are in [ARCHITECTURE_OPTIMIZATION.md](ARCHITECTURE_OPTIMIZATION.md), section "Phase 4C configuration-precedence experiment".

**Dependency installation.**
- The binary shows that OpenCode writes a `.gitignore` into, and starts an in-process npm install in, every writable config folder (no subprocess, scripts disabled). That includes a workspace `.opencode/`.
- The fixture was not run as it stood.
- Both runs used a guard: `.opencode/.gitignore` in the fixture, the workspace `.opencode/` read-only, and a read-only global config folder.
- No installation happened in either run (new check C16), and the workspace was unchanged.

**Run A, project config enabled.**
- The project file loaded, and **bash became allowed and offered to the model**. The project file fixed the key order, so its later `b*sh: allow` beats the managed `bash: deny`, which managed config still won as a key.
- The `.opencode/` agent file added an agent that may run bash and edit protected files.
- Protected-file edits, web access, external folders and subagents stayed denied.
- **DevPilot rejected the configuration** (C8, C9 and C12, with 7 findings).

**Run B, `OPENCODE_DISABLE_PROJECT_CONFIG=1`.** No workspace file was read. The result equals the baseline (20 PASS, 0 FAIL, 1 NOT VERIFIED), and DevPilot accepted it.

**Isolation and cleanup.** `docker inspect` confirmed the hardening in both runs (now 6 mounts, 5 of them read-only). Containers and run folders were removed, and the evidence is redacted.

**Code changed, uncommitted, check tooling only:**
- the install guard and check C16, in `opencode_check.py`, the runner and the probe
- the comparison report
- tests

No change was made to the adapter, the policy module or the contract module.

**Tests:** 834 passed, 10 skipped, 0 failed. Coverage is 96% without the adapter and 86% with it.

**Phase 4D must:**
- enforce `OPENCODE_DISABLE_PROJECT_CONFIG=1`, not rely on detection
- use the install guard
- force `agent: "build"` in the API filter
- keep the pre-check, effective-policy check and audit

**How it is known.** Findings 1–5 were observed on the server. The install mechanism was read from the binary, and its prevention was observed (C16). Session-level rule precedence is **not verified**. The conclusions table in ARCHITECTURE_OPTIMIZATION.md tags each item.

**Still open:**
- the adapter is untested
- A1 and A2 are unresolved
- C13 (unknown paths) is not verified, and waits for the API filter

**Commit:** this round's tooling, tests and docs were committed after approval. The adapter and the egress line are not included.

### Phase 4D Round 1: configuration hardening and critical recovery fixes (2026-10-31)

**Scope: Stages 1–4 only.** Committed after approval, without the adapter, its test file and the `127.0.0.1:4096` egress line. No prompt, model call, paid API, image build, download or network change was made. The API filter, model gateway and launcher are not built, and `generate_code` is not registered.

**Stage 1, production container** (`coding/opencode_container.py`).
- One definition with no switch to weaken it. `production_environment()` takes no argument and refuses to return without `OPENCODE_DISABLE_PROJECT_CONFIG=1`.
- `ProductionContainer` has no field for variables, mounts, ports or networks.
- The precedence experiment stays in `opencode_check.py`, which no application module imports. A test enforces that.
- OpenCode sees a private copy of the project. The real workspaces, the repository, the home folder and the checkpoints are refused as mount sources.
- `deployment_problems` confirms from `docker inspect` that the definition was applied, with an exact allowlist of environment names.

**Stage 2, installation paths** (`INSTALL_PATHS`). Read from the pinned binary as bytes, never by running it.
- The guard is now part of every baseline. C16 also fails on a logged attempt or a `~/.opencode` folder, and says it is an observation, not proof.
- New: C17 reads each model's SDK package from `GET /provider`, and `config_problems` refuses any `npm` override and `skills.urls`.
- **New finding: ripgrep.** With no `rg` on PATH, OpenCode downloads it from github.com on the first use of the grep or glob tool, both of which DevPilot allows. In the check image only the missing network stops that, so the attempt is not prevented. C18 stays NOT VERIFIED and the production builder refuses any image without `rg`. No runtime image is approved yet, so production cannot start. This is deliberate.

**Stages 3 and 4, A1 and A2** (`coding/session_control.py`, `coding/attempt_store.py`, table `coding_attempts`).
- Lifecycle: `PREPARED → SUBMITTED → RUNNING or AMBIGUOUS → ABORT_REQUESTED → QUIESCENCE_CHECK → SAFE_TO_RESTORE or QUARANTINED`.
- A prompt is sent at most once. Only a 204 counts as accepted.
- **Decision: a restore always needs confirmed container termination.** Session and workspace are watched and recorded, but observation alone never permits it. A test shows a delayed prompt fooling the watch.
- Logical cancellation, confirmed termination and verified restoration are separate recorded facts.
- Each phase is saved before its step. `recover` finishes any unresolved phase after a crash. A quarantined attempt has no way out and holds its project.
- The adapter now uses this lifecycle (uncommitted, A1 and A2 only). It still refuses every task until Stage 5, because it does not read `GET /agent` yet.

**Tests of the commit alone, run on an export of the staged files:** 1051 passed, 10 skipped, 0 failed, 97% coverage. With the held-back adapter and its 19 wiring tests: 1070 passed, 93% coverage, the adapter at 59%. A mutation check broke the safety logic in 31 ways in a throwaway copy, and the tests caught all 31. A simulation of 1500 interleavings found no restore with a writer active, given a truthful stop.

**Guarded baseline** (one run, approved): 22 PASS, 0 FAIL, 2 NOT VERIFIED of 24, in 11.8 s. Not verified: C13 (waits for the API filter) and C18 (no `rg` in the check image). `docker inspect` confirmed the hardening, and the container and run folder were removed. The image is unchanged. The probe gained one read-only `GET /provider` and a check for `rg`. Evidence: `D:\DevPilotCache\opencode-check\reports\20261030T201709Z-baseline*`.

**Static checks:** 0 secret-pattern hits, no network or process imports in the new modules, no `anthropic` import. `ruff` and `mypy` are not installed, so they were not run.

**Open:**
- Stage 10 must wire `recover` into backend startup. Nothing calls it yet.
- The guarantee rests on `terminate()` telling the truth: a stop that says "gone" while a quiet writer survives cannot be detected. Stage 9 must confirm with an independent `docker inspect`.
- The adapter's HTTP layer is untested. Abort-to-idle time on a busy session and session-level rule precedence are unmeasured.
- No code clears a quarantine yet. It needs a person.

### Phase 4D Round 2: adapter, tool allowlist and read-only server check (2026-10-31)

**Round 1 is committed as `f4aa786`,** without the adapter, its test file and the egress line. On an export of the staged files: 1051 passed, 10 skipped, 0 failed.

Committed after approval, without the `127.0.0.1:4096` egress line. No prompt, model call, paid API, image build, download or network change was made. The API filter, gateway, launcher and `generate_code` are not built.

**Stage 5, adapter** (`tools/adapters/opencode.py`).
- New `OpenCodeApi` Protocol: the decisions are tested with an in-memory fake, and `OpenCodeClient` is the thin HTTP side.
- Before a session exists: the API filter answers, then health and version, `/doc`, `GET /config` with `GET /agent`, and the tool inventory.
- Every answer is checked: status 200, `application/json`, body shape. HTML with a 200 is refused. Sessions must hold DevPilot's folder and exact rule list. Abort and rejections must answer the JSON value `true`.
- Completion follows the server's own loop rule, by `parentID` (`session_control.turn_finished`), never by counting messages. Messages from before the attempt are ignored, for completion and for tokens.
- Duplicated contract code is gone. The client's message ID, and the A1/A2 lifecycle, are kept.

**Security finding: the prompt's `tools` field replaces the session's rules.** On 1.19.5 `SessionPrompt.prompt` turns `tools` into a rule list and stores it over the session's whole list. The adapter's "further layer" would have silently removed the protected-path rules. It is gone, sessions are checked against the exact list, and the API filter must refuse `tools`.

**Other fixes the reading found:**
- A late finish could have returned COMPLETED from a stopped attempt. It now returns CANCELLED.
- The adapter treated the workspace root as outside the project, which would have stopped a legitimate `glob`.
- `permission.v2.asked` and `question.v2.asked` were not fully handled.

**Stage 6, allowlist** (`coding/opencode_tools.py`).
- `read, glob, grep, edit, write, apply_patch, todowrite`, plus `invalid`, which executes nothing. The denylist is gone.
- The server's list must equal the pinned 14 tools of 1.19.5. Any other release is unreviewed.
- `apply_patch` paths are read from the patch itself, and move destinations are judged, because OpenCode asks `edit` only for the source.
- Any permission request or question stops the task. Terminal and MCP events stop it. An event that cannot be read stops it.
- A `pending` part under a forbidden name is not a violation: that is a model asking, and OpenCode repairs it to `invalid`.

**Read-only real-server check** (one run, approved): 28 PASS, 0 FAIL. Same isolation as the baseline, confirmed by `docker inspect`. 20 GET requests reached the server through a relay inside the container, because it has no port. 7 others were refused on the host, and the relay refused 4 more on its own. No session exists afterwards, the workspace is unchanged, and the server log shows no session activity. This is read-only compatibility, not coding integration. Evidence: `D:\DevPilotCache\opencode-check\reports\20261031T055130Z-readonly-http.json`.

**Server finding:** 1.19.5 sends `null` for optional agent fields its own schema types as boolean, number or string. Nothing DevPilot reads depends on them.

**Tests:** 1593 passed, 10 skipped, 0 failed. Coverage is 96%, or 97% without the adapter. The adapter is at 84% offline, and 90% with the real read-only run. A mutation check broke the new logic in 52 ways in a throwaway copy. The tests caught 51; the survivor exposed a gap, now tested, and caught. Lines still unexercised are the client's writing calls, which a read-only check cannot reach.

**Open:** see "Phase 4D Round 2" in ARCHITECTURE_OPTIMIZATION.md.

### Phase 4D Round 3: API filter and container termination (2026-10-31)

**Stages 5 and 6 are committed as `53070e8`,** 19 files, without the egress line. On an export of the staged files: 1593 passed, 10 skipped, 0 failed.

Round 3 is committed after review, without the egress line. No prompt, model call, paid API, image build, download or network change was made. The gateway, launcher and `generate_code` are not built.

**Termination** (`sandbox/docker_cli.py`, `coding/container_stop.py`).
- `docker rm -f` exits 0 for a container that does not exist, so its exit code proves nothing.
- `inspect_container` returns RUNNING, STOPPED, NOT_FOUND or UNKNOWN_ERROR, and never raises. Only Docker's exact line for the exact reference is NOT_FOUND. A daemon failure, timeout, permission error, unreadable answer or different identity is UNKNOWN_ERROR.
- `ContainerTerminator` identifies the container by its full ID and confirms only from a separate read after asking for the stop. An unconfirmed stop quarantines, and the workspace is not restored.
- `coding_attempts` gains `container_id` and `stop_evidence` (redacted). The original failure is kept.
- The one caller of the old `inspect` was the sandbox's final state. It now calls `final_state`, with the same behaviour. `remove` keeps its meaning.

**API filter** (`backend/edge/`). A pure policy and a small byte mover, standard library only. The checks run in the order framing, authentication, route, body. It allows 15 operations, one of them its own health check, and 13 of the 188 in the server's spec. Nothing is forwarded as received. The per-run token is compared in constant time, and the OpenCode password stays in the filter. ARCHITECTURE_OPTIMIZATION.md has the endpoints, schemas and protections.

**Adapter.** `OpenCodeClient` takes the filter's token. A 403 now means the filter refused, a protocol failure. `from_settings` is gone: it had no callers, and static credentials contradict a per-run token.

**Bugs found on the way:**
- Tests caught `answer_problem` letting `200 application/json` through on `prompt_async`, which should only ever answer 204.
- The event limiter counted the tail twice across chunks.
- An unread request body could destroy the error reply with a TCP reset, so refused requests now drain first.

**Isolated filter run** (one run, approved): 42 PASS, 0 FAIL, 18.6 s. Same guarded container as the baseline, with no network, no ports and no credentials. **C13 is verified at the filter:** 8 unknown paths got 403, not the web page.
- **All 175 other operations in the server's spec were refused,** along with 9 unknown methods, 19 encoded or normalized paths, 20 unapproved directories, 18 prompts, 7 sessions, 2 permission grants, 7 failed logins and 13 ambiguous requests.
- A witness in front of OpenCode counted 10 requests before and after those 280 refusals, so none reached it. It saw 15 requests in all, each an allowed operation, and no prompt.
- One inert session was created through the filter, and an idle abort answered `true`. This is the first real writing traffic, and it is disclosed here because it goes beyond reading.
- The filter's token got 401 directly on OpenCode. No credential is in the audit, witness log or report.
- On one real container, `inspect_container` and `ContainerTerminator` gave the right answer for running, a wrong identity, a confirmed stop and a missing container.
- Afterwards: 0 containers, images and networks unchanged, no run folders.

Evidence: `D:\DevPilotCache\opencode-check\reports\20261031T064332Z-filter-check.json`.

**Not proven:** production network isolation. The filter and OpenCode shared one container and one user. That proof belongs to the launcher stage.

**Tests:** 2245 passed, 10 skipped (opt-in Docker and live OpenAI tests), 0 failed. Coverage is 96%, with or without the adapter. `filter_policy.py` and `container_stop.py` are at 100%. The adapter is at 88% offline, and 92% with the real filter run. `filter_server.py` is at 76%: its listener, `HttpUpstream` and `main` ran on the real path, but that run measured only the adapter. A mutation check broke the new logic in 59 ways in a throwaway copy. The tests caught 57. One survivor exposed a real gap, stop evidence not proven redacted, now tested and caught. The other was an ineffective break, rewritten and caught. `ruff` and `mypy` are not installed in this virtual environment, so neither ran.

**Open:** see "Phase 4D Round 3" in ARCHITECTURE_OPTIMIZATION.md, which also holds the review of what remains and the recommended Round 4 scope.

### MVP slice: end-to-end workflow, offline (2026-10-31)

Priorities changed: finish a working workflow instead of more Phase 4 rounds. Round 3 is committed as `ab3f447`, and this slice as `b7a0010`.

**Built:**
- **Model gateway** (`edge/gateway_policy.py`, `gateway_server.py`). A per-run token, the approved model only, function tools only, and budgets for requests, tokens and dollars, enforced before forwarding. The upstream is fixed in code. Offline tests use an in-memory fake. **No real API call was made.**
- **Launcher** (`coding/launcher.py`). Four networks, three hardened containers, secrets by name, confirmed stop. Tested against a fake Docker only. It refuses to start until each approval below exists.
- **Disposable workspace** (`coding/candidate.py`). The real project is written once, by `finish`, with exactly the files that were tested.
- **Tools** (`tools/adapters/coding.py`): `generate_code`, `submit_implementation`, `finish`. GPT stays the only orchestrator.
- **Minimal engines:** `verification/`, `recovery/`, `selection/`. Phases 5 to 7 had not been started, so these are small working cores, not those phases.
- **Simulated provider** (`coding/simulated.py`) and the demonstration (`python -m app.demo.mvp`).

**What the pinned binary showed (read as bytes, never run):**
- The `openai` provider calls `sdk.responses(...)`, which is `POST {baseURL}/responses`. WebSockets sit behind an experimental flag that the environment allowlist refuses.
- `gpt-6.1-sol` is in the bundled catalog, at $2 and $10 per million tokens.
- `rg` is looked for on `PATH` first. Without it, OpenCode downloads ripgrep 15.1.0 from GitHub.
- The native `fff` library is looked up on local disk only. It has no download path.

**Verified on real Docker, no model:** neither image on disk holds `rg`. User 65534 can create, change, rename and delete on a D: bind mount, and modification times match on the host, which closes that open item from Round 3.

**Offline demonstration, on real Docker:** 6 of 6 scenarios ended as expected, and no container was left. Evidence: `D:\DevPilotCache\mvp-demo\mvp-demo.json` and `mvp-demo.md`. A pass reached the real workspace only through `finish`. In both failed scenarios the real workspace was unchanged, by hash.

**Tests:** 2560 passed, 17 skipped (opt-in Docker and live OpenAI tests), 0 failed. With Docker enabled, those 16 tests pass too. Coverage is 95%, and 96% without the three modules that hold the real network clients, which offline tests cannot reach. A mutation check broke the new logic in 91 ways in a throwaway copy. 90 are caught, after 7 survivors exposed test gaps that are now closed. The last one changes nothing, because `AttemptGuard.restore` refuses as well. `ruff` and `mypy` are not installed in this virtual environment, so neither ran.

**Limits:**
- The launcher, the gateway's TLS path, the four networks and repeated `--network` flags have never run for real. Network isolation stays unproven.
- The tests in a run come from the same engine as the code, so a pass shows they agree, not that either is right.
- Selection, recovery and verification are the small cores described above.

### Real-container verification, no model (2026-10-31, final pass 2026-11-01)

The launcher, filter, gateway and OpenCode 1.19.5 ran on real containers, on two internal networks, with a scripted model and no key. No paid call was made, and nothing reached OpenAI.

**Results.** The first nine ran on the final code, each with a hard deadline of 240 s. The last two were not repeated, for the reason given.

| Scenario | Result | What it shows |
|---|---|---|
| `boundaries` | 77 PASS, 0 FAIL | The container matches its definition, every unapproved operation is refused and audited, overload is refused with nothing forwarded, the host cannot reach OpenCode, and writes stay in the disposable copy |
| `gateway` | 20 PASS, 0 FAIL | Tokens, model, request, token and dollar limits, timeouts, no secret in any log |
| `stop` | 10 PASS, 0 FAIL | A timeout: confirmed stop, then a verified restore |
| `cancel` | 8 PASS, 0 FAIL | A person's cancel takes the same path |
| `quarantine` | 9 PASS, 0 FAIL | An unconfirmed stop: the writer was really running, nothing was restored, the copy was kept |
| `recovery` | 9 PASS, 0 FAIL | A real writer left running by an abandoned session: the next start stops it, confirmed from Docker, and undoes its work |
| `full` | 8 PASS, 0 FAIL | A whole run through real OpenCode and real Docker tests, GPT and the model scripted |
| `production_gateway` | 9 PASS, 0 FAIL | The real `gateway_server.py`, placeholder key, no route: it starts, refuses bad tokens, trusts 118 certificate authorities, fails closed and charges in full |
| `rehearsal` | 10 PASS, 0 FAIL | The paid script's own `dry_launch` and `run_demo` under its exact limits, scripted |
| `engine` | 40 PASS, 0 FAIL (2026-10-31) | 44 scripted answers accepted, real tools ran, `apply_patch` wrote files. Not repeated: later changes added a ledger field and a forwarded `service_tier` the scripted upstream ignores, and a refusal path it does not reach |
| `interrupt` | 33 PASS, 0 FAIL (2026-10-31) | Ctrl-C under PowerShell, a kill, a deadline, a wedged process, and a stop that cannot be confirmed. Not repeated: the guard's stopping path has not changed since, and each case leaves a folder holding a token |

**What the first runs got wrong:**
- **The 47-minute run was the laptop asleep.** Windows Modern Standby lasted 45 min 45 s of it, and only about 2 minutes was work. The check now asks Windows to stay awake and treats a clock gap as an invalid run.
- **The stalled run was a limit in the check itself:** 20 requests against a script needing 44. OpenCode retries a 429 five times, over about 65 s, longer than the check waited. These retries are read from the binary: at most 5 per prompt, on any 429 or 5xx.
- **Ctrl-C skipped cleanup because PowerShell terminates its child part-way through a `finally`.** Reproduced with the same signal under two hosts: Python alone finished cleanup, and under PowerShell it was killed in it. No handler inside the process can fix that, so a separate guard does the job.

**Real bugs found and fixed:**
- A request in flight when the gateway died left no ledger line, so its cost could go unseen. It is now written before it leaves.
- `auto` follows the project's setting, which may be the dearer Fast tier. Every call is now pinned to `default`.
- A request over 272,000 input tokens costs $4 and $15, so a reservation at $2 and $10 would be too small. Such a request is now refused, on both sides.
- A busy refusal left no audit line and could be lost to a connection reset. A connection with no thread available closed silently. Both now answer 503 and are recorded.
- A weak check let "no tool attempted" pass as a refusal, and three others could not fail. All are fixed.

**The 503s.** The three first seen in a sequential run were not reproduced, in 600 paced and 300 concurrent requests, and their cause is unknown. A slow audit log is not supported: the slowest write measured was 2.1 ms. What is proven is the part that matters: overload fails closed. With every slot deliberately taken, a valid `POST /session` got 503, and OpenCode's own log shows no session was created, while the same request afterwards did appear.

**Not proven:**
- The published loopback port, `devpilot-ingress` and `devpilot-egress` have never run, because each needs a network with a route out. The dry launch checks them before the first paid call.
- The gateway's TLS path has never reached OpenAI. Its trust store and its failure path are checked, not a real handshake.
- Real model behaviour and model-driven cancellation: every stop so far ended a scripted stall.
- A real process death followed by recovery, as one test. `interrupt` kills a real process and shows the guard stops everything. `recovery` runs the startup path against a real writer, but its crash is a session abandoned inside one process.
- The tests in a run come from the same engine as the code, so a pass shows they agree, not that either is right.

### Offline close-out (2026-11-01)

The last step before any live demonstration. **No paid call was made, nothing contacted OpenAI, and the real key was not loaded. No paid call is authorized.**

**The real key stays out of offline work.** Some test runs on 2026-10-31 loaded `.env` into memory, though they printed and sent nothing. That is closed:
- `tests/conftest.py` installs an audit hook that refuses any open of the real `.env` before it happens, so a test that tries fails. Only `RUN_LIVE_OPENAI_TESTS=true`, which needs approval, lifts it. Offline tests use fake credentials from `tests/helpers.py`.
- `backend/scripts/opencode_launcher_check.py` passes `_env_file=None`, and refuses to run holding a provider key.
- These scripts do load `.env`, so the key enters their process: `live_demo.py` (its preflight too), `check_openai_access.py`, `live_smoke_test.py` and `demo_sandbox.py`. None of them ran in this round, and none belongs in the offline workflow.

**How this round was checked.** The runs sat inside a fence, a `sitecustomize` loaded into each Python process through `PYTHONPATH`, kept outside the repository. It refused an open of the real `.env` and any public network contact before they happened, and logged each attempt. A control proved each refusal on a decoy. It refused nothing in any run. One guard test's child sets its own `PYTHONPATH`, so it sits outside the fence. The second witness covers it, and everything else that is not Python: NTFS last-access time on `.env`. A control proved a read updates it on this volume, and it did not change. `.env` was not hashed or opened to compare it: only its size and times were read.

**Final results:**

| Check | Result |
|---|---|
| Offline suite | 2683 passed, 17 skipped (16 opt-in Docker tests and 1 live OpenAI test), 0 failed |
| Opt-in Docker tests | 16 passed, 0 failed |
| Real-container scenarios | Nine scenarios on the final code: 160 PASS, 0 FAIL, in 431 s, on battery. No clock gap |
| Containers and networks | None left at the last read, 19:21Z, after all of this round's Docker use: 0 containers, only Docker's own networks. Docker Desktop's engine then idled, and failed to restart when another project's Compose call woke it, so no later read exists. With the WSL VM down, nothing can be running |
| `ruff`, `mypy` | Not run: neither is installed in this virtual environment |

**Mutation check.** A campaign of 153 deliberate breaks crashed at the 59th, on a hanging mutant, on 2026-10-31. Of the 59 it reached, all are now detected, after three test gaps and the hang were fixed. The other 94 ran once on 2026-11-01, on a throwaway copy holding no `.env`, with no network and no Docker, a limit of 180 s per run and 50 minutes overall, each run in its own Windows job so nothing could outlive it.

| Mutants | Caught | Survived | Timed out | Not executed |
|---|---|---|---|---|
| The 59 reached on 2026-10-31, after their fixes | 59 | 0 | 0 | 0 |
| The 94 run on 2026-11-01 | 92 | 2 | 0 | 0 |

Both survivors were investigated, in one targeted check. Neither is a defect:
- `guard: a failed start is ignored` was a test gap. The next check still refused the run, about 30 seconds later, and the test accepted either refusal. The test now names the reason, and catches this.
- `tool: recovery trusts the crash` is left as it is. The check in `recover_attempts` is a second layer: `AttemptGuard.restore` refuses as well. Removing that inner check alone is caught, and with both removed `test_a_crashed_attempt_that_cannot_be_stopped_stays_quarantined` fails, so the tests do see a wrongful restore.

So 152 of the 153 are detected, and the last is redundant by design. The 94 ran with no timeout, no clock gap and nothing left behind; the 59 were not run again. These breaks were chosen by hand for the safety logic, so this shows the tests notice them, not that every possible break would be noticed.

**What is real and what is simulated in the offline runs:**

| Part | Status |
|---|---|
| API, orchestrator loop, budgets, tools, audit, delivery, report | Real code |
| Docker, the OpenCode 1.19.5 server and its tools, the API filter, the gateway's code, the internal networks, the disposable copy, stops, restores and quarantine | Real |
| Generated tests running in the Docker sandbox | Real |
| GPT's replies | **Simulated:** scripted by `FakeLLM` |
| The model behind OpenCode | **Simulated:** a scripted upstream inside the real `Gateway`. No key exists in these runs |
| The gateway's connection to OpenAI | **Never run.** `production_gateway` runs the real server with a placeholder key and no route, and it fails closed |
| The route from the host to the filter | **Simulated:** `docker exec` stands in for the published port, which has never run |
| The crash in `recovery` | **Simulated:** the session is abandoned, in one process. The writer, the stop, the restore and the startup path are real |

**Retained evidence.** Nothing was deleted. `D:\DevPilotCache\launcher-check` holds the 13 folders from the first pass, unchanged by fingerprint, and one more, `20261031T191216Z`, from this round. Two attempts stay QUARANTINED on purpose, one from each pass, in `20261031T171452Z\quarantine` and in the new folder: each stop was recorded as unconfirmed, with a Docker CLI broken by the check. Both containers have since been confirmed gone by a separate read, and their workspaces and evidence are intact. They stay until their cleanup is authorized. Six attempt folders from the first pass hold gateway tokens: test-only, not OpenAI keys, each valid only for a gateway that no longer exists. None is in a report, log or the repository. The cleanup is proposed separately in `D:\DevPilotCache\cleanup-recommendation-2026-11-01.md`, and has not been done.

### Stage 2: adaptive tool selection (2026-11-02)

Python only: no Docker, no model, no key, and the real `.env` was not read. The design and full results are in [ARCHITECTURE_OPTIMIZATION.md](ARCHITECTURE_OPTIMIZATION.md), "Stage 2".

**Built:** `selection/adaptive.py` (eligibility, then `U = 0.30·C + 0.25·R + 0.15·Q + 0.10·L + 0.10·K + 0.10·M`), `selection/history.py` and the `tool_outcomes` table (learning from verified outcomes only, with real and simulated evidence apart), `selection/catalog.py` (what really exists), `selection/model.py`, and `demo/selection_benchmark.py`. `SELECTION_POLICY=fixed` keeps the old preference order.

**Results, all simulated, on hypothetical tools:**

| Each scenario counted equally | Fixed | Adaptive |
|---|---|---|
| Correct selection | 15.4% | 84.7% |
| Simulated success within policy | 33.5% | 64.6% |
| Policy violations | 4800 | 0 |
| Switched tool after a failure | 0 of 598 | 488 of 488 |

These figures describe a scenario mix in which the preferred tool is usually wrong or forbidden, and predict nothing about real use. **Fixed preference wins the control scenario** (100.0% against 14.5% correct), because the adaptive policy has no exploration and never tries a better untested tool. **The Phase 7 target is not met:** 85.0% against 90%.

**Tests:** The full offline suite passed on this code: 2,827 passed, 17 skipped and 0 failed, in 102 s, peak 217 MB, one process. The 17 skipped are the opt-in tests: 16 need Docker and 1 calls OpenAI. The real `.env` was never opened, and nothing reached Docker or an external host. 39 deliberate breaks of the selection logic were tried on throwaway copies with no `.env`, and the final tests catch all 39. The first 20, on eligibility and learning, were all caught. The next 19, on tie-breaking, the budget arithmetic, what reaches the selector and how authorization is built, found 5 that no test caught: floating-point ties, remaining tokens reaching the selector, verified history reaching the selector, a verdict staying inside its own run, and only a failure marking a tool as failed. The code was correct in all five. The tests were the gap, and they were added or fixed. `ruff` and `mypy` are not installed.

**Deferred, because they need Docker:** the 16 opt-in Docker tests, and a real-container pass of `generate_code` under the adaptive policy.

**Known limitations, recorded and not addressed in this stage:**
- Only one real coding provider exists, so no real comparison has happened. The adaptive policy's only real effect today is refusing attempts the budget or memory cannot support.
- There is no exploration policy, so a better untested tool can be missed for good. On a paid path exploring costs money, so it needs its own decision.
- There is no circuit breaker.
- Quality is declared, not measured.
- The simulated benchmark is biased toward adaptive selection: in 11 of its 13 scenarios the fixed preference is wrong or forbidden.
- The simulated 90% target was missed, at 85%.

### Stage 3: intelligent failure recovery (2026-11-02)

**Implemented and tested offline, not validated on real failures.** Python only: no Docker, no model, no key, and the real `.env` was not read. Design and results: [ARCHITECTURE_OPTIMIZATION.md](ARCHITECTURE_OPTIMIZATION.md), "Stage 3".

**Built:** `recovery/diagnosis.py` (failure categories, evidence, confidence, whether recovery is safe, the actions that could apply, a fingerprint), `recovery/policy.py` (a deterministic choice of RETRY, REPAIR, SWITCH_TOOL, RECONFIGURE or STOP), two columns on `tool_outcomes`, `runs.recovery_json`, and `demo/recovery_benchmark.py`. Security failures and ambiguous states fail closed. A1 and A2 are unchanged.

**Decided (2026-11-02):** `RECOVERY_REPEAT_LIMIT` stays at 2. Timeout escalation is off: `RECOVERY_MAX_ATTEMPT_TIMEOUT_SECONDS` is unset, which means `OPENCODE_TIMEOUT_SECONDS`. Both `RECOVERY_POLICY=bounded` and `intelligent` are kept, the latter for controlled evaluation.

**Simulated result:** the intelligent policy recovers **59.8% against 66.3%** for the bounded baseline, at $0.155 against $0.225 a failure, with 0 unsafe decisions against 1393. As deployed, with escalation off, it recovers 55.7%. A repeat limit of 3 gave 79.6%, which is recorded as an experimental finding only. The scenarios were written for the benchmark, and no real-failure dataset exists.

**Tests:** Whole offline suite: 3,206 passed, 17 skipped (16 opt-in Docker, 1 live), 0 failed, in 83 s, in one process with a 226 MB peak, and the fence refused nothing. 376 are Stage 3's own (220 classification, 90 decisions, 47 through the real flow, 19 benchmark), plus 3 on the configuration. A targeted check broke 25 safety rules one at a time in a throwaway copy, and the tests caught all 25. The three fixes made after review each have tests that fail without them.

**Deferred, because they need Docker:** the 16 opt-in Docker tests under the intelligent policy, a real-container retry, and a real unconfirmed stop.

### Stage 4: final integration and competition demonstration (2026-11-02)

**A working offline demonstration, not validated with a real model or on real Docker in this stage.** No Docker, no model, no key, and the real `.env` was not read. Design and results: [ARCHITECTURE_OPTIMIZATION.md](ARCHITECTURE_OPTIMIZATION.md), "Stage 4". How to run it: [COMPETITION.md](COMPETITION.md). Deployment: [DEPLOYMENT.md](DEPLOYMENT.md).

**Built:** independent acceptance (`verification/acceptance.py`: a frozen evaluator suite the provider never sees), `ReplaySandbox` (recorded runs of exact files, no process), the competition demonstration (`demo/competition.py`, six scenarios, structured reports), the three-configuration evaluation (`demo/evaluation.py`), and the dashboard (`app/ui`, served at `/ui`).

**Integration audit:** no defect found in the existing path. The missing parts were added.

**Evaluation, held-out seeds (simulated):** verified completion 45.0% (A: fixed + bounded), 75.0% (B: adaptive + bounded), 72.0% (C: adaptive + intelligent); violations 42, 0, 0; incorrect recovery decisions 34.7%, 31.9%, 32.9%. Adaptive selection is where the gain is; the deployed C completes slightly less than B, mostly on transient failures. Stage 3's benchmark stands: bounded 66.3% against intelligent 55.7%.

**Tests:** Whole offline suite: 3,288 passed, 17 skipped (16 opt-in Docker, 1 live), 0 failed, in 106 s, in one process with a 232 MB peak. The fence refused nothing, and no `.env` was opened outside pytest's temp folders. 82 tests are new in Stage 4: acceptance, the replay sandbox and its recordings, acceptance through the orchestrator, the API, the demonstration, the evaluation and the dashboard. The demonstration ran 6 of 6 scenarios as expected in 6 s, and the evaluation made 600 backend runs (300 per split) with nothing refused.

**Final review (2026-11-02):** three reporting defects were fixed. The Markdown report called replayed results "Tests in the sandbox", and left out failed acceptance runs and the evidence behind each recovery decision. The dashboard did not highlight the selected run, swallowed load errors, and did not show a decision's evidence. `--serve` held its link in a buffer when its output was piped. Same-process acceptance is documented as **not an adversarial security boundary**. The fixes were re-checked with the targeted Stage 4 tests (135 passed) and the demonstration (6 of 6), and the dashboard was checked visually in the installed Chrome, headless, on loopback. The whole-suite figure above (3,288 passed) was measured before these fixes.

**Status by kind** (details in [COMPETITION.md](COMPETITION.md)):
- Implemented and offline-tested: requirement intake, GPT's tool loop and gates (with FakeLLM), adaptive selection, recovery, independent acceptance, safe workspace management, reports, the dashboard, the demonstration and the evaluation.
- Scripted or replayed: GPT's replies, every coding provider, test execution, the additional providers and the evaluation's tools.
- Verified on real Docker before Stages 2 to 4 (historical): sandbox isolation (2026-10-30), the MVP on Docker tests (2026-10-31), OpenCode on real containers with a scripted model (2026-10-31 to 2026-11-01).
- Awaiting Docker verification of the Stage 2 to 4 code: the 16 opt-in Docker tests, the acceptance suite in the real sandbox, recovery on real containers.
- Real AI not yet tested beyond one live GPT call (2026-10-29): a full real-GPT run, model-written code, the classifier on real failures.
- Not built: cloud deployment and a remote worker ([DEPLOYMENT.md](DEPLOYMENT.md)), and the integrations below.

### Real-World Milestone 1: GitHub-assisted development (2026-11-02)

**Offline-tested with fixtures; live GitHub, real OpenCode generation and real Docker execution pending.** Details, blockers and the permissions a first real run needs: [MILESTONE_1_GITHUB.md](MILESTONE_1_GITHUB.md).

**Built:** `app/repos/` (repository model, licence policy, evaluation and the reuse-or-build decision, `GitHubSource` and fixtures, the safe importer) and two approval-gated GPT tools, `search_repositories` and `import_repository`, in PLANNING. Delivery refuses reused code without its licence and `THIRD_PARTY_NOTICES.md`. The weather request's specification and frozen acceptance suite are in `app/demo/weather.py`.

**Found offline, first pass:** the full weather request was refused at planning, because a plan named one stack and React and FastAPI are two; a back-end-only scope stopped at `generate_code`, because `python-fastapi` had no approved test command.

**Readiness round (same day):** multi-stack plans (components with their own stacks and folders, tasks per component, dependencies across components, every requirement covered, one-stack plans unchanged); verification recipes for `python-fastapi` (pytest) and `react-vite` (Vitest) with fixed commands, checked folders, runtimes, dependency checks and parsers, **defined but never executed** because their runtime images are not built; a precise blocked status (`STOPPED_BY_POLICY`, `verification_unavailable`, each component and its reason) instead of writing code that cannot be verified; per-component repository search and import; and the GitHub client wired into `python -m app` behind `REPOSITORY_SOURCE=github`, off by default. A defect found by the rehearsal is fixed: criteria were checked against the main stack's test methods only. `python -m app.demo.fullstack` rehearses the full-stack request offline: 13 of 13 checks pass, and it ends blocked, with nothing generated.

**Tests:** 97 new across the milestone; whole offline suite 3,385 passed, 17 skipped, 0 failed; the fence refused nothing. Live GitHub, real code generation, real Docker execution and the framework recipes remain unverified.

### Milestone 6: Google Drive and the RAG chatbot builder (2026-11-03)

**Implemented and offline-tested; committed as `829b9da`.** The security hardening after it (OAuth contracts, parser isolation, approval details) is committed as `af5c576`. It is described in §14 of the milestone document. With the hardening, the whole offline suite gives 3,585 passed, 17 skipped and 0 failed. Details, security findings and what real use needs: [MILESTONE_2_GOOGLE_DRIVE_RAG.md](MILESTONE_2_GOOGLE_DRIVE_RAG.md).

**Built:**
- `app/drive/`: a read-only Drive v3 client behind the `google_drive` egress entry, grants owned by one project, DevPilot's own folder restriction, a consent URL with PKCE, and a simulated Drive for tests.
- `app/rag/`:
  - extraction for TXT, Google Docs and DOCX, with PDF behind an interface, because no parser is installed;
  - chunks that keep their source;
  - a hashing test-double embedder;
  - a scoped in-memory index;
  - retrieval and citation checks;
  - provider selection through the adaptive selector;
  - the RAG application blueprint.
- Seven tools, three of them approval-gated, behind `DRIVE_SOURCE` (off by default). The plan gate checks a RAG plan against its blueprint.
- `python -m app.demo.rag`, which passes 24 of 24 checks and ends blocked, with nothing generated.

**Simulated:** Google Drive, consent, embeddings and GPT. **Pending:**
- real OAuth (callback, token exchange, encrypted refresh tokens, revocation at Google);
- parsing in the no-network parser container (SECURITY.md §8);
- a PDF parser;
- a real embedding provider;
- real code generation;
- Docker verification.

### Manual GPT planning from the terminal (2026-11-03)

**Purpose:** evidence that the real GPT API can take a person's own prompt and produce a valid plan through DevPilot's own orchestration, before more features are added. **Built, offline-tested, and run once live by the user:** the real model's analysis and plan for a college attendance portal passed the gates (see the entry at the top of this file).

**Entry point:** `app/plan_cli.py`, run from `backend` as `python -m app.plan_cli`. Without `--live` it is a dry run: it shows the first request exactly as it would be sent, and its worst-case cost, from a throwaway database. Nothing is sent. With `--live`, it asks for the prompt and a typed "yes".

**What it reuses:**
- `create_app`, with the configured database and nothing wired but the GPT client;
- the orchestrator, the analysis and plan gates, the approval queue (a GPT question is answered in the terminal, and Enter stops the run) and `RunGuard`;
- the adaptive selector and the verification-readiness check, asked what build mode would decide for each task. The OpenCode launcher is given a Docker CLI that cannot start a process.

No new model gateway was written.

**Limits:**
- **Fixed in code, not `.env`:**
  - at most 3 requests per run, counted before sending, with OPENAI_MAX_RETRIES forced to 0;
  - MAX_TURNS 3;
  - $0.25 per run, through `RunGuard`'s worst case before every request;
  - 90,000 tokens per run, worst case before every request, which holds even if the prices are wrong;
  - 6,000 output tokens per request.
- **Across runs:** $1.50, in `data/planning/ledger.jsonl`. A run counts at its cap until it is settled, and an unreadable ledger refuses.
- **Required before sending:** prices must be configured, and requests must go to OpenAI's own endpoint.
- **Tools:** only `submit_analysis`, `submit_plan` and `request_human_input` may be offered. The run is refused at `submit_criteria`, then closed, so it cannot be resumed outside the limits.

**Output:**
- **On screen:**
  1. the requirement;
  2. GPT's responses, with each gate verdict;
  3. the analysis and plan;
  4. the tasks;
  5. tool eligibility;
  6. blockers;
  7. tokens and estimated cost;
  8. the verdict.
- **Report file:** `data/planning/<run_id>.json`, which also keeps GPT's raw tool arguments.

**Also changed:** `create_app` takes a `gpt_label`, so a real client behind a guard is not reported as FakeLLM. `.env.example`'s Google redirect URI now uses the path the OAuth code accepts.

**Tests:** `test_plan_cli.py`, 21, offline (FakeLLM and a stand-in for the OpenAI client). They cover:
- the prompt reaching the orchestrator, and the stop at plan acceptance;
- the gate still rejecting a bad plan;
- the request, token, run-cap and experiment-cap limits;
- a GPT question waiting for the person, and Ctrl+C;
- confirmation and preflight refusals;
- the dry run leaving the database alone;
- live mode having no path to a scripted model;
- no key in the output or report.

Related suites (orchestrator, coding, repository, Drive, API report, live smoke): 200 passed together.

**Billing uncertainty:** costs are computed from the configured OPENAI_PRICE_* values. If those are lower than the model's real prices, the real cost is higher by the same ratio. OpenAI's usage page is the authority.

### Product backlog (requested 2026-11-02, not built)

**A. GitHub-assisted development** (discovery, evaluation and import built offline in Milestone 1; adapting and testing reused code waits on the blockers there)
- Search public repositories for projects relevant to a requirement.
- Evaluate their relevance and their licences, and propose only projects whose licence fits the intended use.
- Clone approved projects, after the user approves each one, into isolated workspaces under the same jail and disposable-copy rules as generated code.
- Adapt and test the existing code in the sandbox, with the same verification and acceptance suite as generated code.
- Needs: GitHub access through the approval queue (SECURITY.md §10), egress allowlist entries, and Docker for the tests.

**Full-stack coding interfaces (2026-11-02, after commit `6ead6b3`, not committed):** component tasks confined to their folders, per-component recipes and evidence, and a project that passes only when every component and every MUST criterion pass. Tested with scripted outputs and a simulated runtime approval; the real FastAPI and React runtimes, Docker verification, live GitHub and real code generation are pending. Details: [MILESTONE_1_GITHUB.md](MILESTONE_1_GITHUB.md).

**B. Google Drive-to-RAG development** (built offline in Milestone 6, see above; real OAuth, the parser container, real embeddings and generation pending)
- User-authorised OAuth access (SECURITY.md §7).
- Narrowly scoped permissions: only the folders or documents the user picks.
- Ingestion of PDF, DOCX and Google Docs, parsed in the no-network document-parser container (SECURITY.md §8).
- Embeddings and a vector index.
- A chatbot whose answers cite their sources.
- Document-access and privacy protections: no document content in logs or prompts beyond what the user selected, encrypted refresh tokens, and deletion on request.
- Needs: a Google OAuth client, an embedding model and approval for its cost, and the parser container.

**C. Live App Preview** (on the roadmap; not started): show a generated application running, from a verified build only.

### First real demonstration (proposed, not authorized)

**Nothing below has run, and no paid call is authorized.** It needs its own explicit approval. `backend/scripts/live_demo.py` holds these limits as constants, and `.env` supplies only the key. Without `--approve-spend 1.50` it runs a preflight that contacts nothing external, but that preflight does load the key, so it needs that approval too.

**Request:** "Create a Python calculator application supporting addition, subtraction, multiplication and division, with unit tests."

| Item | Value |
|---|---|
| Models | GPT `gpt-6.1-sol`, OpenCode `openai/gpt-6.1-sol` through the gateway |
| Prices | $2 input, $10 output per million, Standard tier up to 272,000 input tokens. Read from developers.openai.com/api/docs/pricing on 2026-10-31. Cached input is $0.10, and is charged as $2 here |
| **Proposed total spend** | **`RUN_BUDGET_USD=1.50`, strict, across GPT and OpenCode together.** There is no extra turn past it: a call that could pass the limit is not sent |
| Per attempt | `GATEWAY_MAX_USD_PER_TASK=0.75` or what the run has left, `GATEWAY_MAX_REQUESTS_PER_TASK=15`, `OPENCODE_MAX_TOKENS_PER_TASK=100000`, `GATEWAY_MAX_OUTPUT_TOKENS=8000` |
| GPT | `MAX_TURNS=14`, `MAX_TOOL_CALLS=14`, `OPENAI_MAX_OUTPUT_TOKENS=4000`, `OPENAI_MAX_RETRIES=0`, `RUN_MAX_TOKENS=300000` |
| Bounds | `MAX_REPAIR_ATTEMPTS=1`, `OPENCODE_MAX_ATTEMPTS_PER_TASK=2` |
| Time | `OPENCODE_TIMEOUT_SECONDS=300` per attempt, `RUN_WALL_CLOCK_SECONDS=1200`, and the guard's own deadline 300 s past that |
| OpenCode tools | For this model it offers `apply_patch`, `read`, `glob`, `grep` and `todowrite`. Not `write` or `edit` |

**Measured sizes:** an OpenCode request is 15 to 17 KB, so its reservation starts at $0.114. A GPT request is 3 to 5 KB, reserving about $0.07. The offline calculator path takes 7 GPT turns. OpenCode makes no title call: 22 prompts cost 44 requests, one per tool call plus one to finish.

**The 15-request limit.** It stays at 15. It is not the limit that binds: the 100,000-token limit reserves each request's bytes as tokens, so an attempt gets about 6 to 12 requests (modelled with the real `Budget`; the growth per turn is assumed). A calculator task should need 3 to 8. Refusal is free, and the attempt then fails.

**Networks, created for each attempt and removed afterwards:**

| Network | Route out | Attached | Purpose |
|---|---|---|---|
| `devpilot-ingress` | Yes (a published port needs it) | filter | `127.0.0.1:4096` to the filter, loopback only |
| `devpilot-core` | No, `--internal` | filter, OpenCode | The filter reaches OpenCode |
| `devpilot-model` | No, `--internal` | OpenCode, gateway | OpenCode reaches the gateway |
| `devpilot-egress` | Yes | gateway only | The gateway reaches `api.openai.com:443` |

A bridge network cannot limit destinations. The gateway's single destination is fixed in its code, with verified TLS, and it is the only holder of the key. The filter sits on a network with a route out, and has no code that uses it.

**Held back, uncommitted:** the line `127.0.0.1:4096` under `backend:` in `config/egress_allowlist.yaml`. The preflight fails without it.

**Runtime image:** `devpilot/opencode-runtime:1.19.5`, `sha256:a7753fe443915eed28c577c1d1b3f0e27ab1c5bd16b7debc7de9579d2aaefe99`, in `APPROVED_RUNTIME_IMAGES`. It is `devpilot/opencode-check:1.19.5` plus one verified file, built with no network:

```powershell
$d = "D:\DevPilotCache\opencode\ripgrep-15.1.0"; New-Item -ItemType Directory -Force $d
curl.exe -L --fail -o "$d\rg.tar.gz" https://github.com/BurntSushi/ripgrep/releases/download/15.1.0/ripgrep-15.1.0-x86_64-unknown-linux-musl.tar.gz
(Get-FileHash "$d\rg.tar.gz" -Algorithm SHA256).Hash   # must be 1C9297BE4A084EEA7ECAEDF93EB03D058D6FAAE29BBC57ECDAF5063921491599
tar -xzf "$d\rg.tar.gz" -C $d --strip-components=1 ripgrep-15.1.0-x86_64-unknown-linux-musl/rg
Set-Content "$d\Dockerfile" "FROM devpilot/opencode-check:1.19.5`nCOPY rg /usr/local/bin/rg" -NoNewline
docker build --network none --pull=false -t devpilot/opencode-runtime:1.19.5 $d
```

The source is GitHub's release of BurntSushi/ripgrep, the version OpenCode 1.19.5 itself pins. The archive is 2,263,077 bytes. `rg` on `PATH` stops OpenCode's download.

**Order of a paid run:** preflight, keep-awake and guard, the free model-access check (`GET /v1/models/...`), a dry launch of the real containers, then the run. A run that pauses is cancelled, so nothing goes on spending unattended. Every stop takes the path verified above. Afterwards, compare the gateway's ledger with OpenAI's usage page.

**Failure and cleanup:** the same paths the offline run exercised. A failed attempt stops the container, confirms it from an independent read, and restores the disposable copy. Over budget, the gateway answers 429 and the run stops. If a stop cannot be confirmed, the run is quarantined and everything stays for review. The real project is written only by `finish`, after a pass.

**Account access.** A real call to `gpt-6.1-sol` succeeded on 2026-10-29. It was not re-checked here, because that means sending the key to OpenAI. The script does it first, at no charge. It proves the key sees the model, not that quota remains.

### Dashboard integration: coding engine, console, mobile, end-to-end (2026-11-04)

**Not committed.** No paid call, no live build, and the real `.env` was never opened (audit hooks in the tests and in the end-to-end backend refuse it).

**Why the dashboard said "No coding engine is configured (OPENCODE_ENABLED)":** `python -m app` connects OpenCode only when `OPENCODE_ENABLED=true`, and `.env.example` ships `false`. Turning it on is not enough: the launcher also requires `CODING_LIVE_APPROVED`, the approved runtime image, the verified binary's manifest, `OPENCODE_MODEL=openai/<model>`, prices, `OPENAI_MAX_OUTPUT_TOKENS` and `OPENAI_MAX_RETRIES=0`, and real builds have only ever run behind the terminal scripts' preflight and guard.
- `coding/launcher.py`: the launcher's checks are now `config_problems()`, used by `unavailable_reason()` and by `/capabilities`, which names every missing setting even when the engine is not connected (names only, never values).
- `scripts/live_server.py` (new): serves the dashboard with `live_build.py`'s proven configuration ($0.50 a run), the same preflight, model-access check, dry launch, guard and keep-awake, `REQUIRE_SPEND_APPROVAL`, a session total (`--approve-session`, at most $5.00, `SessionBudget` in `orchestrator/start_policy.py`) and a session length (`--hours`). Not run: it loads the key.
- Gemini is not supported: the OpenCode policy bundles only `@ai-sdk/openai`, the gateway forwards only to `api.openai.com`, and GPT is the only orchestrator.

**Console warnings:** `MaxListenersExceededWarning` and `ObjectMultiplex` are not from the app. The production bundle and the source contain no event emitter, and headless Edge with no extensions logged neither across 91 messages. They match MetaMask's injected `obj-multiplex` stream. App-owned listeners were audited: `useMediaQuery` passed a new subscribe function on every render, so React re-subscribed each time; it is now stable. Plan and analysis are fetched only after the run records them, so a working run no longer logs a 404 on every poll.

**Mobile:** the build-approval dialog had no height limit and could clip its checkbox and buttons. It now has a scrolling body and a fixed footer holding the checkbox and both buttons, stacks them on phones, takes focus and closes on Escape. `/runs` scrolled sideways below 1280 px because an absolutely positioned `sr-only` header escaped the table's unpositioned scroll container. Popovers span the screen below the header on phones. Long setting names wrap.

**History download fix:** `verification/artifact.py` no longer fails on a partial report (`report.md` falls back to a note; `report.json` is complete).

**Tests:**
- Backend, new: `test_start_policy.py` (9), `test_live_server.py` (11), `test_api_history.py` (12, including that reading history writes nothing and never opens a database). Full suite: 3801 passed, 17 skipped (opt-in Docker and paid tests), 0 failed.
- Frontend: typecheck and ESLint clean; 95 Vitest tests passed; production build passed.
- End to end, an isolated real backend (fake token, no key, own database, sandbox sweep disabled so the user's running backend is untouched) behind a second Vite server: 28 of 28 HTTP checks through the proxy, and 58 of 58 checks in headless Edge at 320, 375, 400, 768 and 1280 px, including a real download of the verified attendance archive through the UI.

**Still blocked:** code generation from the dashboard (needs `live_server.py`, Docker, the key and the user's approval to spend), FastAPI and React (verification images defined, not built or approved), Gemini (not in the architecture).

### Dashboard simplified; the approve button explained (2026-11-04)

**Not committed.** No paid call, no live build, `.env` never opened.

**Why "Approve and start build" stayed disabled after ticking the box:** it was not a UI fault. The button requires the box **and** `assessReadiness`, built from `/capabilities`, `/budget` and `/status`. `python -m app` has no coding engine, so the backend said no and the button agreed. The reason was buried at the bottom of a long dialog, and the readiness check was written out twice (Composer, RunDetail). Nothing was bypassed: the button is still disabled on such a backend, the backend refuses a start independently, and both are tested.

**Changed (frontend only; no backend script removed):**
- `lib/readiness.ts` (new): the one readiness check, returning a short line (`Ready to build`, `Setup required — Coding engine is not configured`, …) and the full diagnostics kept for an *Advanced details* fold (`components/BuildStatus.tsx`).
- `Composer.tsx` rewritten: no technology-stack section, no stack explanations, no environment names, no warning panels, no attachment button. Python command-line (`DEFAULT_STACK`) is the default. The dialog shows the application, the maximum spending, the approval box, Cancel, Approve and one concise error. A request for React, FastAPI, a website or a mobile app is declined with one sentence (`unsupportedRequest`). The box is always usable; start needs readiness. Double-click guarded; busy state on the button.
- `RunDetail.tsx` start panel uses the same check. `Templates.tsx`: FastAPI/React templates replaced by Python command-line ones, including a weather forecast.

**Backend change:** `verification/artifact.py` now offers a download in two tiers. (1) With an evaluator's independent acceptance suite: it must have passed. (2) For a freeform request, which has no suite: the project's own tests must have passed in the sandbox with at least one test run, and the archive, manifest and UI say "Verified by the generated tests only". Unchanged refusals: a failed acceptance suite (own passing tests do not outvote it), any simulated component, a run that did not complete, no delivered files. `/artifact` reports `verification`. This is a decision the user may reverse: delete the `elif _own_tests_passed` branch to require independent acceptance again.

**Real pipeline, what is and is not ready (checked read-only):** the verified OpenCode 1.19.5 binary, its manifest and the ripgrep 15.1.0 binary (hash checked against its manifest) are on D:. `scripts/live_server.py` supplies every setting `python -m app` lacks, tested: with its constants and a key, `config_problems` is empty once live coding is approved, and the only problem without the approval is `CODING_LIVE_APPROVED`. **Docker Desktop was not running**, so the runtime image could not be checked or built; D: has 128 GB and C: 12.5 GB free (live_build needs 10).

**Tests:** frontend 107 Vitest tests, typecheck, ESLint and build clean. Backend: `test_verified_artifact.py` +3, `test_live_server.py` +1; full suite 3805 passed, 17 skipped (opt-in Docker and paid tests), 0 failed. End to end in headless Edge, over real HTTP, against (a) the real backend with no coding engine: 16 of 16, including that ticking the box leaves start disabled with one concise reason and nothing is created; (b) an offline real backend with **simulated** GPT (FakeLLM), coding (SimulatedLauncher) and tests (ReplaySandbox): 18 of 18, from typing the request to COMPLETED, one build only on a double click, the spending approval recorded, and the download correctly refused because the run was simulated. A real verified build has not been run.

### The `devpilot` command line and the Windows installer (2026-11-04)

**Not committed, not pushed.** No OpenAI request and no paid call; the repository's `.env` was never opened (the CLI cannot read it: it builds `Settings(_env_file=None)`).

**Goal:** `git clone …; cd AutoDev-GPT; .\setup.cmd; .\devpilot.cmd build` for a new user, over the existing engine, with nothing machine-specific.

**New:**
- `setup.cmd`, `setup.ps1`, `devpilot.cmd` (repo root). `setup.ps1` finds Python 3.12, checks Git and Docker, creates `.venv`, installs `backend\requirements.lock` and `-e backend`, then runs `devpilot setup`. It never touches the key.
- `backend/app/cli/` (`devpilot` entry point in `pyproject.toml`): `main` (build, doctor, setup, forget-key), `paths` (`%LOCALAPPDATA%\DevPilot` or `DEVPILOT_HOME`), `userconfig` (model, prices, cap; no secrets), `credentials` (key via `OPENAI_API_KEY`, else **Windows DPAPI**-encrypted file), `runtime` (pinned ripgrep, OpenCode via the unchanged `scripts/fetch_opencode.py`, image build, audit, approval record), `image_audit`, `doctor`, `engine`, `build`, `setup`, `Dockerfile.runtime`.
- `docs/INSTALL_WINDOWS.md`, `examples/prompts.md`; README quick start; `.gitignore` additions.

**Reuse, not a second implementation:** `devpilot build` calls `create_app` with the real OpenCode launcher, Docker sandbox, GPT client, `SessionBudget`, the guard process and keep-awake, and drives it through the same projects/runs/start/approvals API as the dashboard. The start carries `approved_max_usd` equal to the enforced `run_budget_usd` (`REQUIRE_SPEND_APPROVAL` on). Packaging is `verification/artifact.py`, the code behind the dashboard's download, with one optional addition (`extra=`, for `DEVPILOT-HOW-TO-RUN.txt`).

**The image-ID mismatch, root cause and fix:** see `docs/RUNTIME_IMAGE_VALIDATION.md`. The ID is a digest of an OCI index that embeds BuildKit's provenance record, so identical content rebuilt gets a new ID; and a locally built image's layers differ per machine. A fixed ID cannot be right elsewhere. `setup` now builds under a new tag, audits the exported image's *contents* offline and records that exact ID as approved for the installation (`opencode_container.approve_locally`, used by `launcher.config_problems` and `production_problems`). The built-in constant is unchanged; rebuilt or replaced images lose approval until audited again. The earlier "approve `c1e2f26e…`?" request is therefore moot; the old scripts still compare against the constant.

**Spending:** typed `approve <cap>` (or `--approve-spend` with the exact figure) is the only way a build starts; default cap $0.50, at most $2.00; the engine enforces it before every request; per-attempt gateway cap `min(0.30, 0.6 × cap)`; the limits are those of the verified builds. A decline, a wrong figure, or a failed check starts nothing and creates no engine.

**Tests (offline):** `test_cli_foundation` 8, `test_cli_image_audit` 24, `test_cli_runtime` 19, `test_cli_doctor` 10, `test_cli_build` 33, `test_cli_main_and_install` 20 (including a PowerShell parse of `setup.ps1` and `git check-ignore` checks that secrets and outputs are ignored and the installer is not). Full suite: 3922 passed, 17 skipped (opt-in Docker and paid tests), 0 failed.

**Real clean-checkout run:** a copy of exactly what git would hold (tracked plus untracked-not-ignored: 384 files, no `.env`, `.venv`, `node_modules` or data) with a throwaway `DEVPILOT_HOME` and a **fake** key in the environment. `.\setup.cmd --key-from-env --model gpt-6.1-sol --input-price 2 --output-price 10 --yes` installed the pinned dependencies, downloaded and hash-verified ripgrep (GitHub) and OpenCode (npm), built the image, audited it (user 65534:65534, six layers matching their hashes, last layer only `rg` with the pinned SHA-256, `libstdc++` and `libgcc_s` present) and passed `devpilot doctor` **17 of 17**. A second `setup` took 13.6 s and skipped everything. `devpilot --help`, `doctor`, a declined approval, a mismatched `--approve-spend`, an unsupported request and an over-limit cap all behaved and spent nothing. The fake key was in no file; the real `%LOCALAPPDATA%\DevPilot` was never created; the existing `devpilot/opencode-runtime:1.19.5` and `devpilot/opencode-check:1.19.5` images were unchanged.

**Not verified:** a real build through `devpilot build`: it needs your key and your approval to spend. The test-start of the real containers (the "dry launch") was not run, because it needs the live-coding approval flag. FastAPI, React and full-stack remain unsupported, declined at the prompt. Not tried: a machine with Docker's classic image store, a non-Windows OS, or a Docker Desktop that cannot reach the Alpine mirrors.

### n8n integration: signed triggers, signed callbacks, health check (2026-11-04)

**Offline only.** No n8n instance was started or contacted, no paid call was made, and the real `.env` was never opened. Until now, n8n existed only as a planned selector profile.

**New:**
- `app/n8n/`:
  - `workflows.py` loads the registry in `config/n8n_workflows.yaml`. Each entry is a key, a one-segment webhook path, a description and the input names the workflow takes. A malformed file stops the app.
  - `signing.py` implements HMAC-SHA256 over `timestamp.body`, a five-minute window on either side, a constant-time compare, and a replay guard that fails closed when full.
  - `client.py` holds the `N8nClient` Protocol and `HttpN8nClient`. Every request goes through the egress allowlist's `n8n` entry. Redirects are never followed, and answers are size-limited. The public API is read-only: workflows (paged) and executions.
  - `fixtures.py` provides the simulated n8n.
  - `runtime.py` names configuration problems and runs the health check.
- `trigger_workflow` (EXTERNAL_NETWORK) and `get_workflow_result` (READ) in `app/tools/adapters/n8n.py`:
  - GPT names a registered key and exactly its inputs, and never supplies a URL.
  - The trigger is committed before the webhook is sent, so a callback that arrives at once still matches.
  - Workflow output reaches GPT only as labelled untrusted data.
- `N8N_AUTO_APPROVE` is enforced in code. `ToolSpec.auto_approved` is honoured by the PolicyEngine only for EXTERNAL_NETWORK, and the registry refuses it on any other risk, so GIT_PUSH and DESTRUCTIVE still always wait for a person.
- Routes:
  - `POST /integrations/n8n/callback` (HMAC, not the bearer token). It verifies the signature before parsing anything. It requires a trigger that this project's run made, for a workflow registered to report back, and accepts one callback per trigger. Bodies are at most 64 KB, and stored results at most 8,000 characters.
  - `GET /api/v1/integrations/n8n/status` (bearer token) checks each registered workflow against n8n's own list: present, active, recent executions.
  - Both routes answer 404 while `N8N_ENABLED=false`, the default.
- Templates and guide: `n8n/workflows/devpilot-echo.json` (round trip) and `devpilot-notify.json`, with setup steps in `n8n/README.md`.
- Settings: `N8N_ENABLED`, `N8N_WORKFLOWS_FILE`, `N8N_CALLBACK_URL`, `N8N_TIMEOUT_SECONDS`, `N8N_MAX_RESPONSE_BYTES`.

**Tests:** `test_n8n.py` has 52 tests, covering the registry, signing, the client over `httpx.MockTransport`, egress refusals, the runtime, the policy exception, the tools through the real orchestrator and approval queue, and the callback route. Two of them deserve a note:
- **Round trip:** GPT is scripted. It triggers `echo`, a simulated n8n verifies the signature and posts a signed callback to the real FastAPI route on the same database, and GPT's next call reads `success` and the echoed text.
- **Templates in Node.js:** the templates' JavaScript runs in Node.js. It accepts DevPilot's signature, including non-ASCII and quoted text, and refuses forged and stale ones. Its reply passes DevPilot's verification.

**Also in this commit:** the uncommitted static-website pipeline from earlier on 2026-11-04. Four existing tests still asserted that websites were declined, which that change deliberately lifts. They were updated to the new contract:
- OpenCode declares `static-html` at weight 0.6.
- A self-tested verifier lets a static site be accepted, while React stays declined.
- Submitting a site with no website backend still fails closed.
- The CLI help names the new limits.

The claim in `verification/recipes.py` that the Docker website pipeline passed was not re-run in this session.

**Not done:** a real n8n instance, so the templates have never been imported. Forwarding `approval.requested`, `run.completed` and `run.failed` to the notify workflow. The `opencode_via_n8n` coding provider, which stays PLANNED.

### Integration and bug audit: fixes (2026-11-04)

**Offline only.** No paid call, no live service, and the real `.env` was never opened. Each finding below was checked against the source before it was changed. The audit was run against `main` before the n8n and website commits were merged.

**Fixed:**
- **A01 (P0), CLI packaging:** `devpilot build`'s ZIP skipped the check for source files dropped by a safeguard, which the dashboard's download made.
  - Both now call one function, `verification/artifact.decide`.
  - New tests cover a credential-shaped literal in `.py`, `.js`, `.json`, `.html` and `.txt`: the CLI refuses with the same reason as the HTTP route.
  - One older CLI test had expected a ZIP in that case, and was corrected.
- **A02 (P0), unbounded GPT spend:** with prices set and no `OPENAI_MAX_OUTPUT_TOKENS`, each call reserved 0 output tokens and sent no cap. SDK retries could also resend a billed call that the budget then counted once.
  - `llm_client.spend_bound_problem` refuses a start, and `OpenAIResponsesClient` refuses to be built, without a finite output cap or with `OPENAI_MAX_RETRIES` above 0. Every call now carries the cap.
  - OpenAI's unprocessed 429s are still waited out by `rate_wait`.
  - `.env.example` now ships `OPENAI_MAX_OUTPUT_TOKENS=4000` and `OPENAI_MAX_RETRIES=0`.
  - **Your own `.env` may need both settings** before `python -m app` can start runs.
- **A03 (P1), start without approval:** with `REQUIRE_SPEND_APPROVAL=false`, a run with no stack started a paid run with no approval.
  - Now the first start of any run whose GPT is not scripted (FakeLLM) needs an approval of the enforced limit.
  - Configuration problems (unknown prices, GPT not configured) are reported before the approval request.
  - `plan_cli` and `scripts/live_demo.py`, which confirm spending in the terminal, now pass that approval to the API.
- **A04 and A05 (P1), dashboard stack choice:** the composer always used `python-cli`, and a regex declined website and React prompts whatever the backend supported.
  - `lib/prompt.decideStack` maps the request to a stack: explicit command-line wording wins, otherwise React, API, then website wording.
  - That stack is built only if `/capabilities` says it is verifiable. Otherwise the request is declined with the backend's reason, never turned into a Python program.
  - It is decided again on fresh capabilities at approval, and the dialog shows "Builds as".
- **A06 (P1), entry-point wiring:** the CLI engine and `scripts/live_server.py` built the app without the GitHub, Drive and n8n integrations.
  - `app/integrations.integrations_for` is now used by all three entry points, each integration still enabled only by its own setting.
  - A test checks that every production `create_app(...)` call goes through it.
- **A07 (P1), n8n auto-approve:** `N8N_AUTO_APPROVE` covered every workflow.
  - Each registered workflow must now declare its `effect` (`read_only`, `notify`, `write` or `destructive`), and a missing one stops the app.
  - The exemption is a predicate on the parsed arguments, consulted by the PolicyEngine only after the arguments are valid, and it fails closed. Only `read_only` and `notify` are exempt.
  - Tests show that `write` and `destructive` workflows wait for approval and send nothing before it.
- **A10 (P2), cache across tokens:** a changed or cleared token now empties the query cache before the page renders with the new token.
- **A11 (P2), file browser selection:** the selection belongs to one project and one existing file.
- **A13 (P2), CLI request classifier:**
  - Explicit command-line wording now wins over frameworks or platforms the request only mentions ("a CLI that compares Django and Flask releases").
  - A `--stack` the person chose is not overridden by an incidental word.
  - Mobile apps and Django websites are still declined.
- **A14 (P2), home page copy:** it no longer promises "deploy".
- **A15 (P2), CI:** `.github/workflows/ci.yml` runs the backend pytest on Windows and the frontend typecheck, lint, Vitest and build. It uses no secrets, and the opt-in Docker and paid tests skip themselves. It has not run yet: it first runs when pushed.

**Not fixed: design work, each needing its own decision:**
- **A08:** a delayed n8n callback is stored but does not resume a run. Resuming on a callback would spend money without a person, so it needs a wait state and a rule for when a callback may continue a run.
- **A09, partly done:** the dashboard now remembers the token on this computer by default (localStorage; untick it for this tab only), strips quotes copied from `.env`, forgets a refused token everywhere, and its error names the `.env` to use. Server-side sessions, so that no token is pasted at all, are still open.
- **A12:** pagination of projects and runs.
- **A16:** a durable queue that resumes runs after a restart.
- **The audit's G01–G14 gaps:** GitHub writes and PRs, Drive file links and live OAuth, real RAG, the FastAPI and React runtimes, and the others.


### General-purpose platform, Phase 1: the tool catalog (2026-11-05)

Branch `feature/general-purpose-platform`, from `main` at `e120238`. Baseline before any change: backend 4160 passed, 20 skipped (Docker and paid tests, opt-in); frontend typecheck and lint clean, 123 Vitest tests passed.

**Built:**
- **`config/tool_catalog.yaml`**: reviewed facts about every development tool DevPilot really has (GPT, OpenCode, the Docker sandbox, the website verifier, n8n, GitHub, Google Drive). Each has its tasks, advantages, limitations, requirements and evidence, with a level (`documentation` < `offline_test` < `real_container` < `live`), an outcome, a date and the document that records it. `opencode_via_n8n` is listed as planned, not built.
- **`app/toolcatalog/`**: loads and validates the file (a malformed one stops the app), and computes readiness from the running backend only, contacting nothing. A tool backed by a fake reports `simulated`, never `ready`. It also compares tools, recommends a tool per task, and reads a free-text question.
- **`GET /api/v1/tools`** and **`POST /api/v1/tools/compare`** (names, tasks or a question). Answered from the catalog: no model call, no spend.
- **`compare_tools`**: a strict, read-only GPT function in every working state, so the orchestrator chooses tools from the same facts. The planning CLI allows it.
- **Dashboard: a Tools page** (sidebar). Tool cards with readiness here and strongest evidence, and an "Ask" box that answers "Compare n8n and Cloudflare".

**Truthfulness rules, enforced by tests:**
- A name with no adapter (Cloudflare, Zapier, Cursor, Vagrant) is reported as unknown, with no capabilities or results made up for it.
- Only GPT has a passed live run. OpenCode's one live build failed, so its strongest *passed* evidence is real containers with a scripted model. n8n, GitHub and Drive have offline evidence only.
- A coverage test wires every integration with its fixture and checks that the registry and the catalog name exactly the same GPT functions.

**Tests:** `tests/unit/test_tool_catalog.py` (25) and `src/pages/Tools.test.tsx` (3). The two tests that pin the analysis-state tool list now include `compare_tools`. Backend after the phase: 4173 passed, 20 skipped, then 146 of 146 in the affected files after the fixes.

**Limitations:** recommendations come from task words in the question plus the catalog's facts; they explain, they do not choose the build. GPT chooses, and code checks. The catalog cannot compare tools DevPilot has no adapter for, by design.

### General-purpose platform, Phase 2: GitHub and Google Drive resources, the GitHub connection, approved writes (2026-11-05)

**Built (offline, against an in-process GitHub: `tests/github_fake.py`):**
- **Discovery** (`app/sources/discover.py`). GitHub repositories and Google Drive files, folders, Docs, Sheets and Slides named in free text. Exact hosts only. Look-alike hosts, plain http, credentials in a link and short IDs are refused with the reason. A link carrying a token is never echoed.
- **Access check** (`app/sources/check.py`, `POST /api/v1/projects/{id}/resources/check`):
  - A repository the person named is read automatically at a pinned commit, with the same reading as `devpilot inspect`: metadata, file list and small manifests, nothing downloaded or run.
  - A 404 without a connection is reported as "may be private, or the name may be wrong", never as "private", and asks for a GitHub connection. With a connection, a 404 is "not visible to your account".
  - A Drive item is readable only under an active grant of this project.
  - The result is recorded (`resources.checked`) and given to GPT in its first message as an untrusted `<resources>` block.
- **The person's GitHub connection** (`app/repos/github_oauth.py`, `app/api/github.py`). GitHub's OAuth web flow, managed by the backend; the dashboard never sees a token.
  - The flow uses a single-use `state` with a 10-minute expiry, PKCE S256, and a browser-binding cookie scoped to the callback.
  - A narrower scope than asked is refused, and the account is confirmed through `/user` before anything is stored.
  - The token is stored encrypted with Fernet (`DEVPILOT_TOKEN_KEY`) under `CREDENTIALS_DIR`, via `app/core/secret_store.py`. Without the key or the client secret, nothing is exchanged.
  - Disconnecting deletes the local token first, then revokes the grant, and reports the two outcomes separately.
- **`cryptography` 49.0.0 is now installed** in the venv and declared in `pyproject.toml`. Fernet, the approved cipher, can therefore be built, and Drive refresh tokens can be stored too.
- **Approved GitHub writes**:
  - `app/repos/github_write.py`, through the Git Data API: a new repository, or a pull request on a **new branch**. Its tree starts from the base tree, so every file it does not name is kept. A taken branch name is refused, and the default branch is never written.
  - **`publish_repository_changes`** (GIT_PUSH: the approval queue holds every call) is offered in DOCUMENTING once a coding engine and a configured GitHub connection exist.
  - It publishes only after a passed verification, and only the tree whose digest equals the verified one.
  - A pull request carries only the run's added and changed files; deletions are listed, never pushed.
  - A new repository gets the whole project, with the imported licence and notices when code was reused.
  - The packaging rules of the ZIP apply.
  - The approval shows the account, the target, the branch and every file.
- **Dashboard:**
  - After a project is created, its resources are checked. A resource that needs permission opens a small **Permission needed** popup, and the run is not started meanwhile.
  - "Connect GitHub" or "Allow Google Drive access" opens the consent page in its own window. The popup polls, re-checks, and the build then continues by itself. "Build without it" is also offered.
  - The Tools page has a GitHub connection card (connect and disconnect).
- **Google:** `POST /api/v1/projects/{id}/resources/google/authorize` creates a pending grant for the Drive items the requirement names, and returns its consent URL. The existing callback activates it.

**Tests:** `tests/unit/test_github_connection.py` (36):
- discovery, and the encrypted store;
- the OAuth flow's refusals (replayed state, wrong browser, expired state, GitHub error, bad code, narrower scope, missing cipher or secret);
- disconnect and revocation;
- the HTTP flow end to end, the public, private and not-visible checks, and where the token is sent (only `api.github.com`);
- writes that keep other files and never move a branch;
- the publish tool through a real run and approval: approved, denied, files changed after verification, no connection, and a pull request with only the run's changes.

Frontend: `PermissionDialog.test.tsx` (2), plus a Composer test showing that nothing starts while permission is pending. Backend: 4221 passed, 20 skipped. Frontend: 129 passed, typecheck and lint clean.

**Not verified live:**
- No real GitHub OAuth App exists yet. The person registers one, with callback `http://127.0.0.1:8000/oauth/github/callback`, and sets `GITHUB_OAUTH_CLIENT_ID`, `GITHUB_OAUTH_CLIENT_SECRET` and `DEVPILOT_TOKEN_KEY`.
- No request reached GitHub.
- Real Google consent still needs the person's Google OAuth client.
- A run paused inside PLANNING by an `import_repository` authorization error is not resumed by the connection. It is resumed from the run page, as before.
