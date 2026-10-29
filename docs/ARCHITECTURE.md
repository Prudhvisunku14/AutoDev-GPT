# DevPilot AI: Architecture

> Status: **design approved in Phase 0 (2026-10-29) and revised the same day** to add Google Drive OAuth, repository and document import, extensibility, the network policy and test validation. Nothing described here is implemented yet. See [PROGRESS.md](PROGRESS.md) for the components built so far.

## 1. Purpose and principles

DevPilot AI turns a natural-language software requirement into a delivered project: source code, tests, documentation and a final report. The pipeline runs in this order:

1. Analyze the requirement.
2. Plan the work and choose a stack.
3. Define acceptance criteria.
4. Generate code with OpenCode.
5. Execute the code in a sandbox.
6. Test it, then diagnose and repair failures within limits.
7. Document and deliver.

**Principles**

1. **GPT is the central orchestrator.** The OpenAI Responses API with function calling drives every decision at runtime. No other LLM vendor orchestrates. Claude is used only as a development assistant, to build DevPilot itself.
2. **GPT decides, code governs.** GPT plans and issues function calls. Deterministic backend code validates each call, enforces policy and limits, and runs the three measurable algorithms:
   - tool selection (§9.1)
   - failure recovery (§9.2)
   - verification (§9.3)

   The prompts do not hold the rules. The executor returns its decisions to GPT, and it refuses calls that violate them.
3. **Nothing generated runs on the host.** All execution happens in hardened Docker containers. The coding engine may edit files but never execute commands. Imported repositories and parsed documents are untrusted data; parsing happens in containers (§8.3), and cloning is hardened (§8.2).
4. **Everything is measurable and auditable.** Every tool call, selection score, failure classification, recovery decision and verification result is persisted.
5. **Modules sit behind replaceable interfaces.** Each external system (OpenAI, OpenCode, Docker, GitHub, n8n) is reached through a Python `Protocol` that has a fake implementation for tests.

## 2. Component diagram

```
┌──────────────── React + Vite dashboard ──────────────────────────┐
│ projects · new run · live timeline · approvals · criteria · report│
└──────────────▲──────────────────────────────┬────────────────────┘
               │ REST + SSE  /api/v1           │
┌──────────────┴──────────────────────────────▼────────────────────┐
│ FastAPI backend                                                    │
│  API routers ─► RunManager (in-process async worker, 1 run/time)   │
│                    │                                               │
│          ┌─────────▼──────────┐  Responses API   ┌───────────────┐ │
│          │  GPT Orchestrator  │◄────────────────►│ OpenAI (GPT)  │ │
│          │ state machine +    │  function calls  └───────────────┘ │
│          │ function-call loop │                                    │
│          └─────────┬──────────┘                                    │
│                    │ ToolCall ⇄ ToolResult (typed contract)        │
│          ┌─────────▼──────────────────────────────────────────┐    │
│          │ Tool Executor: validate → policy → SELECT → invoke  │    │
│          │  → guards → CLASSIFY → RECOVER → VERIFY → record    │    │
│          └──┬───────┬────────┬────────┬───────┬───────┬───────┘    │
│        WorkspaceFS OpenCode Sandbox  Git/GitHub  n8n  DocStore      │
└───────────┼───────┼────────┼────────┼───────┼───────┼──────────────┘
            ▼       ▼        ▼        ▼       ▼       ▼
     workspaces/  opencode  docker   git CLI  n8n     SQLite FTS5
     (per project) container runner   GitHub  container
                  (HTTP)    containers REST
     SQLite: projects · runs · tool_invocations · tool_stats · criteria ·
             test results · failures · recoveries · approvals · events
```

## 3. Modules

| Module (`backend/app/…`) | Responsibility |
|---|---|
| `api/` | REST + SSE routers, bearer auth dependency, request/response DTOs |
| `core/` | Settings (pydantic-settings), structured logging with secret redaction, event bus (DB rows + in-memory pub/sub feeding SSE), `egress` (default-deny outbound allowlist, §8.5), `crypto` (encryption of stored OAuth refresh tokens, §8.4) |
| `db/` | SQLAlchemy 2.0 models, session factory (SQLite in WAL mode), repositories |
| `schemas/` | Pydantic DTOs and tool-argument models |
| `orchestrator/` | `LLMClient` (Responses API wrapper), run state machine, function-call loop, phase prompts, budget, turn and wall-clock guards |
| `tools/` | `ToolRegistry` (Pydantic → strict JSON schema, phase-scoped exposure), `ToolExecutor` pipeline, `PolicyEngine` (risk levels and approvals) |
| `tools/adapters/` | `workspace_fs`, `opencode`, `gpt_writer`, `sandbox`, `test_runner`, `git_local`, `github`, `github_import`, `google_drive`, `n8n`, `docs_retrieval`, `document_parser`, `registry` (explicit adapter registration, §7.4) |
| `selection/` | **Innovation 1.** Capability profiles, Beta/EWMA statistics, scorer, UCB exploration, circuit breaker |
| `recovery/` | **Innovation 2.** Failure taxonomy, rule-based classifier, fingerprinting, recovery decision policy, rollback |
| `verification/` | **Innovation 3.** Acceptance-criteria validation, traceability matrix, test freeze, verification score and finish gate |
| `stacks/` | `StackCatalog`, the supported generated-project stacks with runner image and install/run/test commands |
| `reporting/` | Final report (Markdown/HTML) and delivery bundle (zip) |
| `documents/` | Document ingestion: PDF, DOCX and text parsing (run in the `document-parser` container), normalization and chunking for DocStore (§8.3) |

## 4. Run lifecycle

### 4.1 State machine

```
CREATED → ANALYZING → PLANNING → DEFINING_CRITERIA → IMPLEMENTING
                                                         │  run_tests
                                                         ▼
                              ┌──────────── REPAIRING ◄─ VERIFYING (automatic)
                              │   run_tests                │ gate passed
                              └──────────► VERIFYING       ▼
                                                      DOCUMENTING → COMPLETED
Side states: AWAITING_APPROVAL (resumes previous state) · FAILED · CANCELLED · STOPPED_BY_POLICY
```

GPT advances the run by calling **gate tools**. The executor applies a transition only when the gate's validation passes in code.

| State | Tools exposed to GPT | Exit condition (checked in code) |
|---|---|---|
| ANALYZING | `search_docs`, `request_human_input`, `submit_analysis`, `ingest_document`, `import_github_repo`, `import_google_drive_file` | `submit_analysis` is valid. It holds a numbered requirement list (`R-001…`), assumptions and ambiguities. Imports and document ingestion happen only in this state, before requirements are fixed. |
| PLANNING | `search_docs`, `submit_plan` | The plan is valid, the stack exists in `StackCatalog` and every task names a capability. |
| DEFINING_CRITERIA | `submit_criteria` | Every `R-xxx` maps to at least one criterion, and no MUST criterion is `manual` (§9.3). |
| IMPLEMENTING | `list_files`, `read_file`, `generate_code`, `install_dependencies`, `run_application`, `run_tests`, `git_checkpoint`, `search_docs` | `run_tests` moves the run to VERIFYING. |
| VERIFYING | *(none; runs automatically in the executor)* | If the verification gate passes, the run moves to DOCUMENTING. Otherwise it moves to REPAIRING with a directive. |
| REPAIRING | `read_file`, `list_files`, `diagnose_failure`, `generate_code`, `install_dependencies`, `run_tests` | `run_tests` moves the run to VERIFYING. A recovery directive of STOP or ESCALATE ends repair. |
| DOCUMENTING | `list_files`, `read_file`, `generate_code` (docs only), `git_checkpoint`, `github_publish`, `trigger_workflow`, `finish` | `finish` succeeds only if the workspace hash still equals the last green-verified hash, and the report is then built. |

`request_human_input` is available in every non-terminal state.

**Phase 2 build.** The orchestrator drives ANALYZING, PLANNING and DEFINING_CRITERIA. When a run reaches a state whose tools belong to a later phase (IMPLEMENTING and after), it stays in that state with `pause_reason = "phase_unavailable"`. GPT is not called again, and nothing is generated or executed. `POST /runs/{id}/start` resumes it once that phase exists.

> Refinement over the Phase 0 plan: `submit_analysis` was added as an explicit gate. It produces the canonical requirement list that the criteria coverage check (§9.3) depends on.

### 4.2 Execution model

- The `RunManager` runs as an in-process asyncio worker started by the FastAPI lifespan. It executes **one run at a time**. Celery and Redis are not used.
- Runs start only on an explicit request: `POST /runs/{id}/start`, or a decision on one of the run's approvals. Nothing resumes automatically after a restart, so GPT is never called (and money never spent) without a user action.
- A run that cannot progress keeps its state and records why in `pause_reason`. Examples: a later phase is needed, an LLM error that can be retried (auth, quota, rate limit, timeout, server error), or a backend shutdown. Starting it again resumes from the last persisted step. A deliberate stop by the caller (`llm_stopped`, used by the smoke-test guard) is not an error: it records `llm.stopped`, never `llm.error`.
- Blocking work (Docker CLI, git, SQLite) runs through `asyncio.to_thread`.
- Every SQLite transaction starts with `BEGIN IMMEDIATE` (WAL mode, `busy_timeout` 30 s). This serializes writers so that a read-then-write transaction never fails with "database is locked". The cost is negligible for one user.
- Run state and `last_response_id` are persisted after every turn. A run paused for approval therefore survives a backend restart.

## 5. GPT orchestration protocol

### 5.1 Function-call loop

```python
while not run.is_terminal:
    resp = llm.create(
        model=settings.openai_model, instructions=PROMPTS[run.state],
        input=pending_items, tools=registry.schemas_for(run.state),  # strict JSON schemas
        previous_response_id=run.last_response_id, parallel_tool_calls=False)
    budget.charge(resp.usage); run.last_response_id = resp.id      # persisted
    calls = [i for i in resp.output if i.type == "function_call"]
    if not calls:
        pending_items = stall_handler.nudge(run, resp.output_text)  # ≤2 nudges, then STOPPED_BY_POLICY
        continue
    pending_items = []
    for c in calls:
        result = executor.execute(ToolCall(run.id, c.name, c.arguments, c.call_id))
        pending_items.append({"type": "function_call_output",
                              "call_id": c.call_id, "output": result.model_dump_json()})
    guards.check(run)   # MAX_TURNS, RUN_WALL_CLOCK_SECONDS, RUN_BUDGET_USD
```

- `parallel_tool_calls=False` keeps the order of side effects deterministic and easy to audit.
- The requirement text, file contents, documents and logs are passed to GPT as **delimited data blocks**, never as instructions (see SECURITY.md).
- `OPENAI_STORE_RESPONSES=false` turns on a fallback mode. In this mode the loop replays its own persisted transcript instead of using `previous_response_id`. **Not implemented yet:** Phase 2 requires `true`, and starting a run with `false` returns 503 with an explanation.
- `tool_choice="required"` is sent whenever tools are exposed. A reply without a function call (possible with other providers or the FakeLLM) gets a reminder. After `MAX_NUDGES` reminders the run stops with `no_progress`.

**Exactly-once execution and crash recovery (implemented in `orchestrator/loop.py`)**
1. Every function call is stored in `tool_invocations` before anything runs. `(run_id, call_id)` is unique, so a replayed call is ignored.
2. A call is *claimed* (QUEUED → STARTED) in its own transaction. Its handler and result are then committed together, inside a savepoint that rolls back a failed handler.
3. A call still STARTED when `advance` begins was cut off by a crash. It is marked INTERRUPTED and GPT is told so. It is never re-executed automatically.
4. Outputs are marked `delivered` only after GPT accepted the request that carried them. An interrupted or failed LLM call is therefore repeated with identical input and the same `previous_response_id`.
5. No database transaction is open while GPT is being called.

**Loop protection (in code):** `MAX_TURNS`, `MAX_TOOL_CALLS`, `RUN_MAX_TOKENS`, `RUN_BUDGET_USD` (enforced when prices are configured), `RUN_WALL_CLOCK_SECONDS` of *active* time (time spent waiting for approval does not count), `MAX_REPEATED_TOOL_CALLS` identical consecutive calls, and `MAX_NUDGES`. When `OPENAI_PRICE_*` is not set, the run records `cost_known = false`, cost stays unreported rather than guessed, and `RUN_MAX_TOKENS` is the hard cap.

### 5.2 `ToolExecutor.execute` pipeline

1. **Parse and validate** the arguments against the tool's Pydantic model. If validation fails, the executor returns `ok:false` with `error.class = INVALID_ARGS`. The loop keeps running.
2. **Check policy.** The tool's risk level and the run state determine the outcome: allow, deny, or require approval (§5.4).
3. **Select a provider.** Capability tools such as `generate_code` and `run_tests` are resolved to a concrete provider by the selector (§9.1).
4. **Invoke** the adapter with a timeout.
5. **Run the post-guards:**
   - Workspace path jail.
   - Frozen-test hash check (§9.3).
   - Output size limits.
   - Diff sanity limits on file count and size.
6. **Classify and recover** on failure. The classifier builds a `FailureReport`, and `RecoveryPolicy.decide` turns it into a directive (§9.2).
7. **Verify** test results. `Verifier.evaluate` produces V, the traceability matrix and the gate decision (§9.3).
8. **Record** the outcome:
   - a `tool_invocations` row with latency, cost and the selection breakdown
   - an update to `tool_stats`
   - events emitted for SSE and n8n
9. **Sanitize** the result. Secrets are redacted and the payload is truncated to a token budget.

### 5.3 ToolResult contract

```json
{"ok": false, "tool": "generate_code", "provider": "opencode_server",
 "data": {"changed_files": ["app/main.py"], "diff_summary": "..."},
 "error": {"class": "TEST_ASSERTION", "summary": "AC-003 failed: ...", "fingerprint": "9f2c…"},
 "directive": {"action": "REPAIR", "reason": "first occurrence", "attempts_left": 3},
 "verification": {"score": 0.71, "must_failing": ["AC-003"]},
 "meta": {"latency_ms": 41230, "cost_usd": 0.021,
          "selection": {"score": 0.74, "breakdown": {"suit": 0.9, "rel": 0.68, "lat": 0.6, "cost": 0.7}}}}
```

The executor **refuses** calls that contradict an active directive. For example, `generate_code` is refused after a STOP, and so is a repair that exceeds `MAX_REPAIR_ITERATIONS`. The refusal is returned as `error.class = POLICY_REFUSED`.

### 5.4 Approvals (pause and resume)

1. The PolicyEngine marks a call `REQUIRES_APPROVAL`. The executor then:
   - persists the pending call (`call_id`, name, redacted args)
   - creates an `approvals` row
   - emits `approval.requested`
   - sets the run to `AWAITING_APPROVAL`
2. The loop suspends without sending a `function_call_output`.
3. A user decides through `POST /api/v1/approvals/{id}/decision`, from the dashboard or n8n:
   - **Approved:** the tool executes, and its real result becomes the `function_call_output`.
   - **Denied:** the `function_call_output` is `{"ok": false, "error": {"class": "APPROVAL_DENIED"}}`.
4. The loop resumes with the persisted `previous_response_id`.

Implementation notes (Phase 2):
- The decision is a conditional `UPDATE … WHERE status = 'PENDING'`, so an approval is decided exactly once. A second decision returns 409.
- The HTTP request only records the decision. An approved call is executed by the run worker, through the same claim step as every other call, so it can never run twice.
- `request_human_input` uses the same queue. Approving requires an answer, which becomes `{"ok": true, "data": {"answer": …}}`. Denying returns `HUMAN_DECLINED`, and GPT is told to continue with an assumption.
- Cancelling a run cancels its pending approvals and skips its queued calls.

### 5.5 Tool catalog

| Tool | Kind | Risk | Notes |
|---|---|---|---|
| `submit_analysis`, `submit_plan`, `submit_criteria` | gate | READ | Validated with Pydantic and code rules |
| `search_docs` | retrieval | READ | FTS5 BM25 top-k with sources |
| `list_files`, `read_file` | workspace | READ | Jailed WorkspaceFS |
| `generate_code` | capability → selector | WORKSPACE_WRITE | Providers: `opencode_server`, `opencode_via_n8n`, `gpt_writer` |
| `install_dependencies` | sandbox | SANDBOX_EXEC (network on) | The only step with network access |
| `run_application`, `run_tests` | sandbox / capability | SANDBOX_EXEC (network none) | Test providers: `pytest_sandbox`, `playwright_sandbox` |
| `diagnose_failure` | recovery | READ | Returns the classifier report and the policy directive |
| `git_checkpoint` | local git | WORKSPACE_WRITE | Commits a checkpoint, which ROLLBACK uses |
| `github_publish` | GitHub | GIT_PUSH | **Always requires approval** |
| `trigger_workflow` | n8n | EXTERNAL_NETWORK | Allowlisted keys only; approval unless `N8N_AUTO_APPROVE=true` |
| `import_github_repo` | import | EXTERNAL_NETWORK | Clones an allowed GitHub repo into the workspace (§8.2). Approval when a credential is used (private repo). |
| `import_google_drive_file` | import | EXTERNAL_NETWORK | Downloads only the Drive file IDs the user selected (§8.4). Always requires approval. |
| `ingest_document` | retrieval | READ | Parses an uploaded or imported PDF, DOCX or text file in the `document-parser` container and indexes it (§8.3). |
| `request_human_input` | control | READ | Pauses the run like an approval |
| `finish` | gate | READ | Final verification and workspace-hash check |

Model IDs come only from configuration (`OPENAI_MODEL`, `OPENCODE_MODEL`).

## 6. Coding engine: OpenCode

**Deployment**
- `opencode serve --hostname 0.0.0.0 --port 4096` runs inside the `devpilot-opencode` container.
- The image is built from `sandbox/images/opencode/` and pins the `opencode-ai` version.
- The project workspace is bind-mounted at `/workspace`.
- The port is published on `127.0.0.1` only.
- HTTP basic auth is enabled through `OPENCODE_SERVER_PASSWORD`.

**Model**
- OpenCode uses its OpenAI provider (`OPENCODE_MODEL=openai/<model>`), so the entire stack is GPT-based.
- The OpenAI key is injected only into this container.

**Permissions** (`opencode.json`, shipped in the image)

```json
{
  "$schema": "https://opencode.ai/config.json",
  "permission": {
    "edit": "allow",
    "external_directory": "deny",
    "webfetch": "deny",
    "bash": { "*": "deny" }
  }
}
```

OpenCode can edit files inside `/workspace` but cannot run commands. All execution goes through the DevPilot sandbox.

**Adapter flow (`tools/adapters/opencode.py`)**
1. Get or create a session per (project, task) with `POST /session`.
2. Send a structured brief with `POST /session/:id/prompt_async`. The brief contains the task, linked criteria, failing tests and diagnosis (in repair mode) and the list of frozen tests.
3. Consume the `GET /event` SSE stream, filtered by session ID, until the session is idle. Progress events are forwarded to the DevPilot event bus. Any permission request is answered by the PolicyEngine through `POST /session/:id/permissions/:permissionID`.
4. If the call exceeds `OPENCODE_TIMEOUT_SECONDS`, send `POST /session/:id/abort`. The result is classified as TIMEOUT or TOOL_TRANSIENT.
5. Fetch `GET /session/:id/diff`. The executor runs its post-guards on the changes, and tokens and cost are recorded from the message info.

**Contract safety**
- At startup the adapter reads `GET /doc` (OpenAPI 3.1) and checks that every endpoint it uses exists.
- Contract tests mock these endpoints with respx.

**Alternative providers**
- `gpt_writer`: GPT writes files directly through the jailed WorkspaceFS. It serves as a fallback and as a comparison baseline.
- `opencode_via_n8n`: see §8.1.

## 7. Execution sandbox and testing

### 7.1 StackCatalog (`stacks/catalog.yaml`, illustrative)

```yaml
python-fastapi:
  runner_image: devpilot/python-runner:3.12
  install: ["python", "-m", "pip", "install", "--target", "/workspace/.devpilot/deps", "-r", "requirements.txt"]
  test:    ["python", "-m", "pytest", "--junitxml=/workspace/.devpilot/junit.xml"]
  run:     ["python", "-m", "uvicorn", "app.main:app", "--port", "8000"]
  health:  {path: "/health", port: 8000}
react-vite:
  runner_image: devpilot/node-runner:lts-playwright
  install: ["npm", "ci"]
  test:    ["npx", "playwright", "test", "--reporter=junit"]
  run:     ["npm", "run", "preview", "--", "--port", "4173"]
  health:  {path: "/", port: 4173}
```

The initial stacks are `python-fastapi`, `python-cli`, `react-vite` and `static-html`. GPT may choose **only** from this catalog, and `submit_plan` rejects any other stack.

### 7.2 SandboxRunner

- The SandboxRunner drives the Docker CLI through `subprocess` with argument lists. It never uses `shell=True`.
- **Every container** gets these flags:
  - `--network none` (except during install)
  - `--cap-drop ALL` and `--security-opt no-new-privileges`
  - a non-root user and `--read-only` with a tmpfs `/tmp`
  - `--memory`, `--cpus` and `--pids-limit` limits
  - a bind mount of the workspace only
  - no Docker socket and no host environment variables
- A wall-clock timeout kills the container.
- **Dependency install** is the only step with network access. Dependencies are installed into the workspace mount (`.devpilot/deps`, `node_modules`) so that later containers without network can reuse them.
- **App health checks and e2e tests** run inside the same container as the app, against localhost. The network namespace therefore stays `none`.
- **Phase 3 build (2026-10-29), as implemented.** Code: `app/sandbox/` and `app/tools/adapters/sandbox.py`.
  - `policy.py` is the approval table. `APPROVED_IMAGES` holds exact references with a version tag (never `latest`). `APPROVED_COMMANDS` holds fixed argument lists with no shell. GPT names a command and, where the command needs one, a workspace file. Python builds the argument list.
  - `docker_cli.py` builds `docker create` in one place, `build_create_args`. Every container gets `--pull never` (an image is never downloaded implicitly), `--network none`, `--cap-drop ALL`, `--security-opt no-new-privileges`, `--read-only`, a non-executable tmpfs at `/tmp`, user `65534:65534`, memory with no swap, CPU and PID limits, `--init`, and `HOME=/tmp` as the only environment variable. The workspace is the only mount, and it is read-only.
  - `runner.py` runs `create`, `start -a`, `inspect` and `rm -f` in that order, and removes the container in a `finally` block. Statuses are SUCCEEDED, FAILED, TIMEOUT, OUTPUT_LIMIT, MEMORY_LIMIT, CANCELLED, ERROR and UNAVAILABLE. Containers carry the label `devpilot.sandbox=true`, and `sweep_orphans` removes leftovers at startup.
  - `process.py` runs the Docker CLI with `subprocess` and an argument list, never a shell. It enforces the wall-clock limit, a cancel signal and an output cap. Each stream keeps 64 KB by default; output beyond 16 times that kills the process.
  - Cancellation: the orchestrator's stop signal (backend shutdown) is passed to every sandbox run, so shutdown kills running containers. A per-run cancel from the API waits for Phase 4.
  - The tool `run_sandbox_command` is registered only when a sandbox is supplied, and only in state IMPLEMENTING. Sandbox outcomes, including timeouts and exit codes, are returned as data with a status. Only requests that break the rules are tool errors, and those run nothing.
  - The orchestrator still pauses at IMPLEMENTING, so GPT cannot call the tool until Phase 4 changes that state.

### 7.3 Test results

- JUnit XML is parsed into `test_runs` and `test_cases`.
- Criterion tags are extracted from each test:
  - Pytest: `@pytest.mark.criterion("AC-001")`, recorded as a JUnit property.
  - Playwright: the title prefix `[AC-001]`.

### 7.4 Extending languages, frameworks and adapters

**Stacks.** A new language or framework is added by a catalog entry and its supporting files, not by orchestrator code. Each addition needs:
1. A `catalog.yaml` entry with `runner_image`, `install`, `test`, `run` and `health`. Each command is an argument list, never a shell string.
2. A runner image under `sandbox/images/<name>/`, pinned by digest.
3. A `selection/profiles.yaml` entry for each provider that supports the stack.
4. A recovery rules file, `recovery/rules/<language>.yaml`, for failure classification.
5. Fixture tests in `tests/fixtures/stacks/<name>/` that the catalog validator runs.

The catalog loader validates everything at startup. Every image must exist, every command must be an argument list, every test marker must be supported, and every referenced adapter must be registered. An entry that fails validation is never offered to GPT. GPT still chooses only from the catalog.

**Adapters.** `ToolAdapter` and `CodingProvider` implementations are registered explicitly in code, by name, capability and required settings, through `tools/adapters/registry.py`. Nothing is discovered dynamically from `workspaces/`, from generated code or from the network. A new adapter must implement its Protocol, have a fake for tests, and declare its risk level and egress hosts. The registry refuses to start if an adapter's declared hosts are not in the egress allowlist.

## 8. Integrations

### 8.1 n8n

- **Deployment:** the `n8nio/n8n` container at `127.0.0.1:5678`.
- **DevPilot → n8n:**
  - `N8nClient.trigger(key, payload)` posts only to webhook URLs registered in `config/n8n_workflows.yaml`.
  - Each request is signed with an `X-DevPilot-Timestamp` header and an `X-DevPilot-Signature` header. The signature is `HMAC-SHA256(N8N_WEBHOOK_SECRET, timestamp + "." + body)`.
  - The run events `approval.requested`, `run.completed` and `run.failed` are forwarded to the notification workflow.
- **n8n → DevPilot:**
  - n8n HTTP Request nodes call the REST API with a bearer-token credential, for example `POST /projects/{id}/runs` to start a run from a GitHub issue or a form.
  - Asynchronous results arrive at `POST /integrations/n8n/callback`. This endpoint verifies the HMAC signature and rejects timestamps older than 5 minutes.
- **OpenCode via n8n:**
  - The template `n8n/workflows/opencode-codegen.json` combines an AI Agent node with the community node n8n-nodes-opencode (an OpenCode LangChain chat model). The chat model points at the same OpenCode server.
  - The workflow is registered as the provider `opencode_via_n8n`, so the selector measures it against the direct route.
- **Health check:** the n8n public API (`X-N8N-API-KEY`) confirms that the required workflows exist and are active.
- **Templates:** `notify-on-event`, `opencode-codegen` and `github-issue-to-run`.

### 8.2 Git and GitHub

- **`git_local`:** runs `git init` in each workspace and commits checkpoints at milestones and after every green verification. ROLLBACK resets to the last green checkpoint.
- **`github`:** uses the REST API through `httpx` with a fine-grained PAT. It can create a repo, push a branch and open a PR. **Every remote action requires approval.**
- **Token handling:** the token reaches git only through a credential helper that reads it from the environment. It is never written to `.git/config`, remote URLs or logs.
- **GitHub import (`import_github_repo`):**
  - Accepts only `https://github.com/<owner>/<repo>` URLs. SSH and other hosts are refused. Public repos need no approval. Private repos use the PAT and require approval.
  - Runs `git clone --no-recurse-submodules` with `GIT_LFS_SKIP_SMUDGE=1`, the file protocol disabled and hooks disabled. A repo larger than `GITHUB_IMPORT_MAX_MB` is refused.
  - After the clone, a workspace scan rejects symlinks, junctions, submodules and files over the size limit.
  - Records the commit SHA in `imported_sources`. Re-importing the same SHA changes nothing.
  - Repository contents, including READMEs, are untrusted data for GPT and are never executed on the host.
  - Git runs on the host with these options. Its residual risk is in SECURITY §5.

### 8.3 Document retrieval (DocStore)

- Supported uploads: Markdown, plain text, PDF and DOCX. Google Drive files arrive in an exported format (Google Docs as DOCX or text, PDF as PDF) and go through the same path. Other types are refused.
- Parsed and generated project docs are chunked by heading, at about 800 tokens per chunk.
- Chunks are indexed in a SQLite **FTS5** table and ranked with **BM25**.
- `search_docs(query, k)` returns the chunks with source references. GPT receives them as delimited data.
- A hybrid with OpenAI embeddings is a possible later upgrade.
- **Parsing (`ingest_document`):**
  - Parsing runs only in the `document-parser` container (`sandbox/images/document-parser/`), with `--network none`, the SandboxRunner flags of §7.2, a read-only input mount and an output mount. Parsers never run on the host.
  - The host receives only normalized Markdown text and a manifest.
  - Limits come from `.env`: `DOCUMENT_MAX_BYTES`, `DOCUMENT_MAX_PAGES` and `DOCUMENT_MAX_EXTRACTED_CHARS`. A DOCX decompression limit (total size and compression ratio) guards against zip bombs.
  - Macro-enabled files (`.docm`) and encrypted PDFs are refused.
  - The PDF, DOCX and text parser libraries are chosen and version-pinned in Phase 8, then recorded in PROGRESS.md.
  - Each parse records the source SHA-256, MIME type and parser version.

### 8.4 Google Drive via OAuth 2.0

- **Client:** the user creates a Google Cloud OAuth client. Its ID and secret go in `.env` and never in the repo.
- **Flow:** authorization code with PKCE (S256) and a random `state`. The redirect URI is `GET /api/v1/integrations/google/callback` on `127.0.0.1`, registered exactly. Tokens and their handling are in SECURITY §7.
- **Scope:** `https://www.googleapis.com/auth/drive.readonly` only. This architecture requests no write scope. Adding one needs a new approved decision.
- **Selection:** DevPilot never lists or crawls a whole Drive. The user picks files in the dashboard, and `import_google_drive_file` accepts only those Drive file IDs.
- **Export:** Google Docs are exported as DOCX or plain text, and then go to `ingest_document`. Google Sheets and Slides are refused in this phase unless a later decision approves them.
- **Approval:** every import batch needs approval. The approval request lists the file names and Drive IDs, never tokens.
- **Revocation:** `DELETE /api/v1/integrations/google/credentials` revokes the token at Google and deletes the stored refresh token.

### 8.5 Network access policy

Default deny. Each component reaches only the destinations below. The list lives in `config/egress_allowlist.yaml` and is validated at startup. The rules that apply to it are in SECURITY §9.

| Component | Allowed destinations | Notes |
|---|---|---|
| DevPilot backend (orchestrator) | `api.openai.com` (or `OPENAI_BASE_URL`) | Key from `.env`; no other egress |
| OpenCode container | `api.openai.com` only | bash and webfetch denied (§6) |
| GitHub adapter and import | `github.com`, `api.github.com` | Reads by default; writes need approval |
| Google Drive adapter | `accounts.google.com`, `oauth2.googleapis.com`, `www.googleapis.com` | Only through the OAuth flow |
| n8n adapter | `127.0.0.1:5678` | Loopback only |
| Sandbox run and test | none | `--network none` |
| Sandbox install | package registries only (PyPI, npm registry), through an allowlisting proxy | Install step only; no secrets in its environment |
| Document parser | none | `--network none` |

## 9. Innovations: measurable algorithms

### 9.1 Adaptive tool selection (`selection/`)

**Candidates** are the providers registered for a capability:
- `code_generation` → `opencode_server`, `opencode_via_n8n`, `gpt_writer`
- `test_execution` → `pytest_sandbox`, `playwright_sandbox`

**Hard filters.** A provider is excluded if any of the following holds:
- a required task feature is missing from its profile
- its circuit breaker is open
- its expected cost is above the remaining run budget

**Score** for tool *t* and task *k* in task category *c*:

```
S(t,k) = w_s·Suit(t,k) + w_r·Rel(t,c) + w_l·(1 − L̂(t)) + w_c·(1 − Ĉ(t)) + UCB(t,c)
```

| Term | Definition |
|---|---|
| `Suit(t,k)` | Σ_f imp_f · prof_t(f) / Σ_f imp_f. The task features *f* are language, framework, multi-file and size bucket. `prof_t(f) ∈ [0,1]` comes from `selection/profiles.yaml`. |
| `Rel(t,c)` | Beta posterior mean `(s + α) / (s + f + α + β)` from `tool_stats`. The prior is α = β = 1, or a value from the profile. |
| `L̂(t)`, `Ĉ(t)` | `min(1, EWMA / budget)` of the observed latency and cost, with EWMA α = 0.3. |
| `UCB(t,c)` | `C · √(ln N_c / (n_t,c + 1))`, an exploration bonus. `C` is set by `SELECTION_UCB_C`; tests use 0. |

- Default weights are 0.35 / 0.35 / 0.15 / 0.15, set in `.env`. Ties break by tool name.
- The full breakdown is persisted with each `tool_invocations` row and shown in the dashboard.
- **Circuit breaker:** after `SELECTION_BREAKER_THRESHOLD` consecutive infrastructure or transient failures, the provider is excluded for a cooldown period.
- **SWITCH_TOOL:** the selector re-runs with the failing provider excluded.

**Metrics**
- Convergence in simulation (target: the p = 0.9 tool is chosen in at least 90% of the last selections after 50 trials).
- Success rate, latency and cost compared with fixed-provider baselines.
- Selection regret.

### 9.2 Failure classification and recovery (`recovery/`)

**Input.** A `FailureSignal`, which holds:
- the source (sandbox, test, tool or API)
- the exit code and timeout flag
- the stderr tail
- JUnit failures
- the HTTP status and exception type

**Classifier.** Structured rules run first (timeout flag, HTTP status, JUnit), then language-specific regex rules (`recovery/rules/python.yaml`, `node.yaml`). The output is `FailureReport{class, confidence, fingerprint, location, evidence}`.

**Fingerprint.** `sha1(class + normalize(top_frame + message))`. Normalization strips paths, line numbers, hex addresses and other numbers.

| Class | Typical signal | Default action |
|---|---|---|
| SYNTAX_ERROR | `SyntaxError`, TS1005 | REPAIR |
| BUILD_ERROR | Vite or tsc build failure | REPAIR |
| DEPENDENCY_MISSING | `ModuleNotFoundError`, `Cannot find module` | REPAIR the manifest, then reinstall |
| RUNTIME_EXCEPTION | Traceback, failed health check | REPAIR |
| TEST_ASSERTION | JUnit failure with assertion | REPAIR, with the failing criteria |
| TIMEOUT | Sandbox kill | RETRY once with a higher limit, then REPAIR |
| TOOL_TRANSIENT | 5xx, 429, connection reset | RETRY with exponential backoff |
| TOOL_CAPABILITY | Empty diff, refusal | SWITCH_TOOL |
| AUTH_CONFIG | 401/403, missing key | STOP (needs a human) |
| INFRA | Docker daemon unavailable | STOP |
| POLICY_VIOLATION | Path escape, test tampering | REVERT + STOP |
| SPEC_AMBIGUITY | GPT reports an unclear requirement | ESCALATE (`request_human_input`) |
| UNKNOWN | No rule matched | REPAIR once, then STOP |

**`decide(report, history, budget)`**, evaluated in order:
1. If the class is non-recoverable, return its terminal action.
2. If repairs ≥ `MAX_REPAIR_ITERATIONS` or the budget is exhausted, STOP.
3. If the same fingerprint appeared in the last K = 2 consecutive failures, SWITCH_TOOL. If there is no alternative provider, STOP.
4. If the number of failing criteria rose after the last repair, ROLLBACK to the last green checkpoint.
5. If there was no reduction in failing criteria for P = 2 iterations, SWITCH_TOOL or STOP.
6. Otherwise, return the class default.

**Metrics**
- Classifier accuracy on a labeled corpus of 40 or more real failure logs (target ≥ 90%).
- Repair success rate.
- Mean attempts until all tests pass.
- Wasted attempts compared with a naive retry-N baseline.

### 9.3 Verification-guided development (`verification/`)

1. **Write the criteria.** `submit_criteria` takes structured criteria in the form `{code: "AC-001", given, when, then, priority: MUST|SHOULD|COULD, method: unit|api|e2e|static|manual, requirement_refs: ["R-002"]}`.
2. **Validate them in code:**
   - Requirement coverage must be 1, so every `R-xxx` is referenced.
   - A MUST criterion may not use the `manual` method.
   - Codes must be unique.
3. **Write tests first.** Tests are generated before the implementation, and each is tagged with its criterion (§7.3). The traceability matrix maps criterion → tests → latest outcome.
   **3a. Validate the criteria and tests** (`verification/test_validator.py`) before freezing:
   - **Criteria:** `given`, `when` and `then` are non-empty. Each `then` names an observable result, such as a status code, an output, a file or an assertion. Codes are unique. Every `requirement_refs` entry exists in the run's requirement list. Vague criteria are returned to GPT for repair; they do not pass silently.
   - **Traceability:** every criterion has at least one test. Tests with a missing or unknown criterion tag are rejected.
   - **Static checks** (AST for pytest, text rules for Playwright): each test contains at least one assertion. `assert True`, unconditional `skip` or `xfail`, and skip markers on MUST criteria are rejected. A test that only checks a mock it configured itself is flagged.
   - **Red-first check:** MUST tests run against the workspace before any implementation exists. A MUST test that already passes is flagged VACUOUS and must be rewritten, or justified through `amend_tests`.
   - **Determinism:** the suite runs twice in the sandbox. A test whose outcome changes between runs is flagged FLAKY and cannot satisfy a MUST criterion.
   Findings are stored in `validation_findings` (§10) and returned to GPT as a repair directive. A finding is never counted as a pass.
4. **Freeze the tests.** When the test set passes validation (step 3a) and is accepted, the SHA-256 of each test file is stored. Any later diff that touches a frozen file is reverted and recorded as POLICY_VIOLATION. Tests can change only through an approved `amend_tests` request with a justification.
5. **Compute the metrics:**
   - Criteria coverage = |criteria with ≥ 1 test| / |criteria|.
   - Verification score `V = Σ w_p · pass_i / Σ w_p`, with MUST = 3, SHOULD = 2 and COULD = 1.
   - **Gate:** every MUST criterion passes, `V ≥ VERIFICATION_THRESHOLD` (default 0.85), MUST coverage is 1, and no open validation finding (vacuous, flaky or unmapped) remains on a MUST criterion.
   - `finish` additionally requires that the workspace hash equals the last green-verified hash.

**Metrics**
- Benchmark success with verification compared with an ablation run without criteria.
- The number of tampering attempts blocked.
- The final V for each project.

## 10. Data model (SQLite, SQLAlchemy 2.0)

| Table | Key columns |
|---|---|
| `projects` | id, name, requirement_text, stack, workspace_path, status, created_at |
| `runs` | id, project_id, state, resume_state, last_response_id, turns, tokens_in, tokens_out, cost_usd, started_at, ended_at, outcome; Phase 2: tool_calls, nudges, active_seconds, cost_known, pause_reason, status_detail, analysis_json, plan_json, pending_messages_json |
| `requirements` | run_id, code (R-xxx), text, kind (functional / non-functional) |
| `tasks` | run_id, order, title, capability, features_json, status |
| `acceptance_criteria` | run_id, code, given, when, then, priority, method, status, test_refs_json, frozen_hash |
| `tool_invocations` | run_id, seq, call_id (unique per run), response_id, tool, arguments_json (redacted), fingerprint, state_at_call, status (QUEUED → AWAITING_APPROVAL/APPROVED → STARTED → SUCCEEDED/FAILED/DENIED/INTERRUPTED, or SKIPPED), ok, error_class, output_json (exactly what GPT receives), delivered, latency_ms, capability, provider; later phases add cost, tokens, selection_json |
| `tool_stats` | provider, category, successes, failures, ewma_latency_ms, ewma_cost_usd, consecutive_failures, breaker_until |
| `test_runs` / `test_cases` | suite, passed, failed, errors, duration, junit_path / name, criterion_code, outcome, message |
| `failures` / `recovery_actions` | class, confidence, fingerprint, location, evidence / action, reason, attempt_no |
| `approvals` | id, run_id, invocation_id (unique), kind (TOOL / HUMAN_INPUT), tool, risk, payload_json (redacted), status (PENDING / APPROVED / DENIED / CANCELLED), response_text (redacted), created_at, decided_at |
| `events` | project_id, run_id (nullable), seq (monotonic per project, assigned inside the INSERT), type, payload_json (redacted), ts (feeds SSE, n8n and the report) |
| `documents` / `doc_chunks_fts` | title, source, source_kind, mime_type, sha256, parser_version / FTS5(content, heading, document_id) |
| `artifacts` | run_id, kind (report / bundle / junit), path |
| `oauth_credentials` | provider, account_label, scopes_json, refresh_token_enc, created_at, revoked_at. Access tokens are never stored. |
| `imported_sources` | project_id, kind (github / google_drive / upload), external_ref, revision (commit SHA or Drive revision ID), sha256, parser_version, approval_id, imported_at |
| `validation_findings` | run_id, criterion_code, test_ref, check (vacuous / flaky / untagged / assertion_missing / unmapped / skipped_must), detail, created_at |

The schema is created with `create_all` initially. Alembic is introduced once the schema stabilizes.

## 11. API boundaries

### 11.1 REST (`/api/v1`)

All routes require a bearer token (`DEVPILOT_API_TOKEN`) unless noted.

| Area | Routes |
|---|---|
| Health | `GET /health` (no auth, liveness only), `GET /status` (database, workspaces, egress components, counts) |
| Projects | `POST /projects`, `GET /projects`, `GET /projects/{id}` |
| Runs | `POST /projects/{id}/runs` (409 while the project has an active run), `GET /projects/{id}/runs`, `GET /runs/{id}`, `POST /runs/{id}/start` (start or resume; 202; 503 if OpenAI is not configured; may cost money), `POST /runs/{id}/cancel`, `GET /runs/{id}/events` (JSON, `after_seq`), `GET /runs/{id}/events/stream` (SSE, §11.3) |
| Run details | `GET /runs/{id}/analysis`, `/plan`, `/criteria`, `/invocations` (Phase 2); `/tests`, `/failures` (later phases) |
| Approvals | `GET /approvals?status=pending\|approved\|denied\|cancelled\|all&run_id=`, `POST /approvals/{id}/decision` with `{"decision": "approve"\|"deny", "response": "…"}` (an answer is required to approve a human-input request) |
| Files | `GET /projects/{id}/files`, `GET /projects/{id}/files/content?path=`, `GET /projects/{id}/diff` |
| Delivery | `GET /runs/{id}/report`, `GET /runs/{id}/bundle` |
| Documents | `POST /documents` (PDF, DOCX, Markdown, text), `GET /documents/search?q=` |
| Imports | `POST /projects/{id}/imports/github`, `POST /projects/{id}/imports/google-drive` (each creates an approval request first) |
| Tools | `GET /tools` (registry, profiles and live stats), `GET /metrics` |
| Integrations | `POST /integrations/n8n/callback` (HMAC instead of the bearer token); `GET /integrations/google/authorize`; `GET /integrations/google/callback` (validated `state` and PKCE instead of the bearer token, loopback only); `DELETE /integrations/google/credentials` |

### 11.2 Internal interfaces (Python `Protocol`s, each with a fake)

```text
LLMClient.create(...) -> LLMResponse
ToolAdapter.invoke(args, ctx) -> AdapterResult
CodingProvider.generate(brief, workspace) -> EditResult
SandboxRunner.run(spec) -> ExecResult
Selector.choose(capability, task) -> Choice
Classifier.classify(signal) -> FailureReport
RecoveryPolicy.decide(report, history, budget) -> Directive
Verifier.evaluate(test_run, criteria) -> VerificationReport
DocumentParser.parse(file, limits) -> ParsedDocument
SourceImporter.fetch(ref, ctx) -> ImportedSource          # GitHub, Google Drive
OAuthTokenStore.save(grant) / load(provider) / revoke(provider)
EgressPolicy.check(host, component) -> Decision
StackRegistry.validate(entry) -> ValidationResult
```

### 11.3 Live events (SSE) and client authentication

`GET /api/v1/runs/{id}/events/stream` sends `text/event-stream` messages. Each event has:
- `id`: its per-project `seq`
- `event`: the event type, such as `run.state_changed`, `llm.request`, `llm.response`, `llm.error`, `llm.stopped`, `sandbox.started`, `sandbox.finished`, `tool.called`, `tool.result`, `approval.requested`, `approval.decided`, `run.paused` or `run.stopped`
- `data`: the redacted event as JSON

Delivery rules:
- **Source of truth.** The stream reads the `events` table (polled every `SSE_POLL_SECONDS`). A reconnecting client misses nothing: it sends the standard `Last-Event-ID` header (or `?after_seq=`) and receives only newer events.
- **Ending.** A terminal run ends with `event: stream.end` once all its events are delivered. Every stream also closes after `SSE_MAX_STREAM_SECONDS`, and clients simply reconnect. Comment lines (`: keepalive`) are sent every `SSE_HEARTBEAT_SECONDS`.
- **Authentication.** The stream uses the same `Authorization: Bearer` header as every other route. Tokens are never accepted in query strings, so they never appear in URLs, proxy logs or browser history. Event payloads are redacted before storage.

**How the React client (Phase 9) authenticates the stream:** the browser's built-in `EventSource` cannot send headers, so the dashboard will not use it. It will read the stream with `fetch()`, using a streaming-body SSE helper such as `@microsoft/fetch-event-source`:
- It sends `Authorization: Bearer <token>` and `Last-Event-ID` itself.
- It reconnects with backoff after a close.
- It keeps the token in memory, never in `localStorage` or the URL.

The token reaches the dashboard by being entered by the user, or through a same-origin login flow if multi-user support is added later.

### 11.4 Repository layout

```
CLAUDE.md  README.md  .env.example  .gitignore  docker-compose.yml (Phase 10)
docs/        ARCHITECTURE.md  PROGRESS.md  SECURITY.md
backend/
  pyproject.toml
  app/
    main.py  config.py
    api/            health.py projects.py runs.py approvals.py files.py documents.py tools.py integrations.py
    core/           logging.py redaction.py auth.py events.py egress.py crypto.py
    db/             models.py session.py repositories.py
    schemas/
    orchestrator/   llm_client.py fake_llm.py loop.py state_machine.py budget.py prompts.py
                    run_control.py approvals.py run_manager.py
    tools/          base.py registry.py executor.py policy.py gates.py
    demo/           calculator.py (offline FakeLLM demonstration)
      adapters/     workspace_fs.py opencode.py gpt_writer.py sandbox.py test_runner.py
                    git_local.py github.py github_import.py google_drive.py
                    n8n.py docs_retrieval.py document_parser.py registry.py
    selection/      profiles.yaml stats.py scorer.py breaker.py
    recovery/       taxonomy.py classifier.py rules/ fingerprint.py policy.py
    verification/   criteria.py traceability.py freeze.py scorer.py test_validator.py
    stacks/         catalog.yaml catalog.py
    reporting/      report_builder.py bundle.py
  tests/  unit/  integration/  fixtures/failure_logs/
frontend/          React + Vite + TypeScript
sandbox/images/    python-runner/  node-runner/  opencode/  document-parser/
n8n/workflows/     notify-on-event.json  opencode-codegen.json  github-issue-to-run.json
config/            n8n_workflows.yaml  egress_allowlist.yaml
evaluation/        benchmarks/  run_benchmark.py  ablations.md
workspaces/  data/ (gitignored, runtime)
```

## 12. Reference projects and what is borrowed

| Project | Ideas used (no code copied) |
|---|---|
| [OpenHands](https://github.com/OpenHands/OpenHands) | Action/observation event stream; a runtime abstraction over a Docker sandbox; iteration and budget guards on the agent loop |
| [OpenCode](https://github.com/anomalyco/opencode) | Integrated through its documented headless server API (`opencode serve`) and permission configuration |
| [n8n](https://github.com/n8n-io/n8n) | Webhook triggers, HTTP Request nodes and credentials for event-driven integration |
| [n8n-nodes-opencode](https://github.com/ssccio/n8n-nodes-opencode) | OpenCode as a chat model inside n8n agent workflows; used as the `opencode_via_n8n` provider |

## 13. Known design limitations

- **Single-user design.** One bearer token and one run at a time. It is not multi-tenant.
- **Docker Desktop on Windows** runs containers inside a WSL2 VM. That is a strong isolation layer, but it is not a hardened multi-tenant boundary.
- **The dependency install step has network access.** A malicious package could run during install. That happens inside the sandbox, with no secrets present.
- **The OpenCode container holds the OpenAI key and has network egress** so that it can reach OpenAI. bash and webfetch are denied, which limits misuse.
- **The supported stacks are limited** to the StackCatalog entries.
- **Google Drive access depends on the user's own OAuth client.** Apps in Google's "Testing" publishing status issue refresh tokens that may expire after about seven days, and only listed test users can sign in. Re-authorization is then required.
- **Disk space on the current laptop is limited.** C: is nearly full, so Docker and WSL2 data must live on D:.
- **Scanned PDFs are not OCR'd** in this design. Their text may be empty, and the run reports that instead of guessing.
- **Extensibility is by catalog entry and explicit registration.** There is no runtime plugin loading.
