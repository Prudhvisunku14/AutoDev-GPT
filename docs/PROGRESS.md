# DevPilot AI: Progress

**Current phase:** Phase 4 (OpenCode) in progress. The foundation, the contract checks, the verified binary and the precedence experiment are committed (2026-10-30).

Phase 4D Round 1 (2026-10-31):
- The production container definition, the install guard and the A1/A2 lifecycle are tested and committed.
- **The guarded baseline against the real server passes: 22 PASS, 0 FAIL, 2 NOT VERIFIED (C13, unknown paths, which depend on the API filter, and C18, the ripgrep download path, which depends on a runtime image).**

The OpenCode adapter itself is not committed, and nothing calls OpenCode yet. Phase 3 is verified in real containers.

## Phase tracker

| # | Phase | Status | Prerequisites |
|---|---|---|---|
| 0 | Architecture and initialization | ✅ Done (2026-10-29, revised the same day) | none |
| 1 | Backend foundation | ✅ Done (2026-10-29) | Python 3.12.9 at `D:\python.exe` |
| 2 | GPT orchestrator | ✅ Done (2026-10-29), verified with FakeLLM; first live GPT call verified (analysis accepted, 1 call, 919 tokens) | OpenAI API key (user) for live use |
| 3 | Docker sandbox | 🟡 Isolation controls verified in real containers (2026-10-30); install proxy and orphan-sweep test on a real container still open | Docker Desktop running, data on D:, `python:3.12-alpine` present |
| 4 | OpenCode integration | 🟡 Foundation committed; binary verified; precedence verified; Phase 4D Stages 1–4 committed (2026-10-31): production container definition, install guard, A1/A2 lifecycle. Guarded baseline 22 PASS, 0 FAIL, 2 NOT VERIFIED. Adapter not committed; not wired into runs | Stage 5 (adapter reads `GET /agent`), API filter, model gateway, launcher, runtime image with ripgrep, approval for live use |
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
