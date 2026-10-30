# DevPilot AI: architecture review before finishing Phase 4

> Reviewed 2026-10-30, at the Phase 4 foundation checkpoint. Nothing in this document is implemented yet unless it says so. The goal is to finish the OpenCode integration safely, not to redesign the system.

**Priorities**
- **P0**: must be resolved before any live OpenCode run.
- **P1**: should be done while finishing Phase 4. Each one costs money, time or safety if left.
- **P2**: worthwhile later. Not needed for Phase 4.

## Summary

| # | Priority | Area | Finding | Recommendation |
|---|---|---|---|---|
| 1 | P0 | Testing | The OpenCode adapter has 0% test coverage | Agree a test route for it before wiring it into runs |
| 2 | P0 | Contract | `/doc` may return HTML, but the adapter requires JSON | Check against one real 1.19.5 server before relying on the contract check |
| 3 | P0 | Security | No safe model route exists for OpenCode | Build the model gateway before any live run |
| 4 | P0 | Security | The OpenCode server can run commands and reach any folder in its container | One container per project, no published port, a fresh password per start |
| 5 | P0 | Recovery | The coding tool cannot find its checkpoint after a crash | **Done (Phase 4B):** tools receive the invocation's `call_id`, and a test shows recovery gets the same key |
| 6 | P1 | Database | Every read takes SQLite's write lock | Use deferred transactions for read-only sessions |
| 7 | P1 | Cost | Large tool outputs are re-sent to GPT on every later turn | Send GPT summaries; keep full output in the database |
| 8 | P1 | Cost | Each coding task needs a separate GPT turn to run its tests | Run the task's tests in the same tool call |
| 9 | P1 | Security | The sandbox image is pinned by tag, not by digest | Pin the recorded digest in the approval table |
| 10 | P1 | Recovery | The orphan-container sweep has never run against a real container | Add a real-container test |
| 11 | P1 | Duplication | Commands are defined both in the sandbox approval table and in the stack catalog design | Choose one source before Phase 5 |
| 12 | P2 | Efficiency | Each coding task re-checks the server three times and copies the whole workspace | Cache per server start; consider a git-based checkpoint |
| 13 | P2 | Cancellation | Cancel is detected by polling once a second | Notify the worker directly from the cancel route |
| 14 | P2 | Security | The dependency-install proxy is not built | Needed before any project can install packages |

## P0: before any live OpenCode run

### 1. The OpenCode adapter is untested
- **Evidence.** [`backend/app/tools/adapters/opencode.py`](../backend/app/tools/adapters/opencode.py) has 0% coverage. Its HTTP-level tests could not be written in this session: the generated content was stopped twice by a safety filter. The policy checker, the audit, the brief and the transaction-free tool path, which the adapter relies on, are tested.
- **Risk.** Completion detection, permission rejection, abort on timeout and reconnection have never been run, not even against canned responses.
- **Recommendation.** Before wiring the adapter into runs, agree with the user how it will be tested. Options:
  - Tests the user writes or reviews.
  - A live contract test against one pinned OpenCode server in its container, with no model attached. It would cover health, `/doc`, `/config`, session creation and abort. This needs the image, which needs approval.

  Keep the adapter out of the verified commit until then.

### 2. The `/doc` format is unconfirmed
- **Evidence.**
  - The server documentation's table describes `GET /doc` as an HTML page containing the spec. Its text also calls it an OpenAPI 3.1 spec.
  - `verify_contract` refuses anything that is not a JSON object with `paths`.
- **Risk.** The adapter could refuse every real server, or someone could later loosen the check without noticing what it protects.
- **Recommendation.** On the first approved contact with a real server:
  1. Record `/doc`'s content type.
  2. If it is HTML, read the JSON spec from wherever the server actually serves it, and keep the check strict.

### 3. No safe model route for OpenCode
- **Evidence.**
  - [`opencode_policy.managed_config`](../backend/app/coding/opencode_policy.py) points OpenCode at `http://devpilot-model-gateway:8787/v1` with a placeholder key. That gateway does not exist.
  - The current design ([ARCHITECTURE.md](ARCHITECTURE.md) §6) would put the real OpenAI key and internet access inside the OpenCode container.
- **Recommendation.** Build a small gateway as DevPilot's own code. It is the only network peer of the OpenCode container and does three things:
  - It forwards only `POST /v1/responses` (and chat completions if OpenCode needs them) to `api.openai.com`.
  - It adds the key outside the container.
  - It enforces the model allowlist (`OPENCODE_MODEL`) and a per-task token budget, so OpenCode cannot overspend even if the adapter's own check fails.

  Generated programs keep running only in the Phase 3 sandbox, with `--network none`.

### 4. The OpenCode server is a command runner
- **Evidence.**
  - The 1.19.5 API includes `POST /session/{id}/shell` and `POST /session/{id}/command`, which run commands. The adapter never calls them ([`NEVER_CALLED`](../backend/app/tools/adapters/opencode.py)), but any client that holds the password can.
  - Every operation also accepts a `directory` parameter, so one server can work on any folder its container can see.
- **Recommendation.**
  - Run one OpenCode container per active project, with only that project's workspace mounted.
  - Do not publish its port on the host. The backend reaches it through the gateway, or over an internal network.
  - Generate a fresh random `OPENCODE_SERVER_PASSWORD` for each container start, and pass it through the environment, never on a command line.
  - Keep the hardening flags already used for the sandbox: `--cap-drop ALL`, `no-new-privileges`, `--read-only` with a tmpfs, a non-root user, and memory and PID limits.

### 5. The coding tool cannot find its checkpoint after a crash
- **Evidence.**
  - The recovery hook receives the invocation's `call_id` ([`RecoveryInfo`](../backend/app/tools/base.py)).
  - Checkpoints are keyed by run and `call_id` ([`Checkpoint.location`](../backend/app/coding/audit.py)).
  - `ToolContext` does not carry the `call_id`, so the tool's `begin` step cannot create the checkpoint under the key that recovery will look for.
- **Recommendation.** Add `call_id` to `ToolContext`, filled by the orchestrator loop. This is a small change and must come before the `generate_code` tool.

## P1: while finishing Phase 4

### 6. Every read takes SQLite's write lock
- **Evidence.** [`_begin_immediate`](../backend/app/db/session.py) starts every transaction with `BEGIN IMMEDIATE`, including read-only ones. Examples:
  - the cancel watcher's once-a-second poll ([`_CancelWatch._run_ended`](../backend/app/orchestrator/loop.py))
  - the SSE stream's poll every `SSE_POLL_SECONDS` ([`backend/app/api/runs.py`](../backend/app/api/runs.py))
  - every `GET` route
- **Risk.** Readers queue behind writers and behind each other. Contention is low now, but Phase 4 adds a second polling loop per run.
- **Recommendation.** Keep `BEGIN IMMEDIATE` for writes, and add a read-only session factory that opens deferred transactions. WAL mode then lets reads run alongside a writer.

### 7. Large tool outputs inflate every later GPT turn
- **Evidence.**
  - Runs chain with `previous_response_id`, so OpenAI bills the whole conversation again on each turn. The live smoke test showed call 2 re-billing call 1.
  - A sandbox result can return up to `SANDBOX_MAX_OUTPUT_BYTES` (64 KB) per stream, and it is then capped only at `TOOL_OUTPUT_MAX_CHARS` (20,000 characters) per call.
  - In IMPLEMENTING, each coding task and test run adds such an output.
- **Recommendation.**
  - Return a short summary to GPT: status, exit code, the first and last lines of output, and failing test names.
  - Store the full output in the database and the events, where GPT can request it on demand.
  - Coding results should list changed paths and violations, never file contents.

### 8. One GPT turn per step during implementation
- **Evidence.** One tool call per turn (`parallel_tool_calls: false`) means "generate task N" and "run its tests" are separate turns. Each turn re-bills the conversation, as described in item 7.
- **Recommendation.** After a coding task is accepted, run the project's tests in the same tool call and return both results together. The code still decides what runs, and GPT gets the test result one turn earlier for every task.

### 9. Image digest pinning
- **Evidence.** [`APPROVED_IMAGES`](../backend/app/sandbox/policy.py) uses `python:3.12-alpine`. The digest pulled on 2026-10-30, `sha256:e6dae589c1c05976188fd3bd019afd091445fc533ed2ea29001beddc7368a9c8`, is recorded only in [SECURITY.md](SECURITY.md) §11.
- **Recommendation.**
  - Use `python:3.12-alpine@sha256:<digest>` in the approval table. The reference format already allows it.
  - With `--pull never`, a re-tagged or tampered image can then never be run.
  - Verify the change with the real-container tests.

### 10. The orphan-container sweep is untested against Docker
- **Evidence.** `sweep_orphans` is tested only against the fake CLI.
- **Recommendation.** Add a Docker-marked test that creates a labelled container, sweeps, and confirms the container is gone. It needs no new image.

### 11. Two places define runnable commands
- **Evidence.**
  - Phase 3's approval table ([`APPROVED_COMMANDS`](../backend/app/sandbox/policy.py)) defines what can run.
  - The stack catalog design ([ARCHITECTURE.md](ARCHITECTURE.md) §7.1) also gives each stack `install`, `test` and `run` commands.
- **Recommendation.** Before Phase 5, generate the per-stack approved commands from the catalog, keeping the approval table as the only thing the sandbox accepts. Until then, add commands only to the approval table.

## P2: later

### 12. Per-task overhead in the coding path
- **Re-checks.** Each task calls health, `/doc` and `/config` before prompting. This is cheap on loopback. Cache the contract while the server's reported version is unchanged, but keep the `/config` check on every task: a task could have changed the workspace configuration.
- **Workspace copies.** The checkpoint copies the whole workspace before each task. The workspace limits bound the cost. A git-based checkpoint would also serve the planned `git_checkpoint` tool, so the two features would not duplicate each other.

### 13. Cancellation latency
- The cancel watcher notices a cancelled run within about a second, because it polls.
- The cancel route could notify the run manager in-process instead.
- This is low value until runs are long.

### 14. Dependency installs
- Projects cannot install packages yet: the restricted install proxy is not built, and the sandbox has only `--network none`.
- This blocks the `python-fastapi` and `react-vite` stacks, but not `python-cli` with the standard library.
- It is a prerequisite for Phase 5 on those stacks. Phase 4 does not need it.

## Already addressed at this checkpoint

| Problem | Status |
|---|---|
| Tool handlers held SQLite's write lock for the whole tool run, so a sandbox run over 30 s made API writes fail | **Fixed.** Slow tools run with no transaction open. `run_sandbox_command` uses this path. Tested. |
| A cancelled run kept its container or coding session running | **Fixed for slow tools.** A per-run watcher stops them. Tested with a blocking sandbox. |
| A tool result that arrived after the run ended was treated as a normal result | **Fixed.** It is stored as `DISCARDED` and never sent to GPT. Tested. |
| After a backend shutdown during a slow tool, the loop made one more GPT call | **Fixed.** The stop flag is checked again after the tool queue. Tested. |
| A crash in a slow tool left side effects | **Hook in place.** `recover` runs once, and the call is never re-run. Tested with a test tool. The coding tool still needs item 5. |

---

# Phase 4B: OpenCode contract and security review (2026-10-30)

No OpenCode server was started and nothing was downloaded. "Verified" below means confirmed by evidence in this repository's tests. **No API contract is verified against a running server yet.**

## Version and sources

| Source | What it says |
|---|---|
| npm `opencode-ai` | dist-tag `latest` = **1.19.5**. `2.2.2` does not exist on npm (404). |
| GitHub releases (anomalyco/opencode) | **v1.19.5 marked Latest** (28 Oct). v2.1.4 to v2.2.2 also exist (27 to 29 Oct). The 2.2.0 notes mention changed external-directory permission checks and a new "Console workspace configuration" layer. |
| GHCR `ghcr.io/anomalyco/opencode` | `latest` = 1.19.5 (digest `sha256:f4cfe16d91efbbfca01a51c44094fba4cf82e15c7fa17de5f11f9059699b1e6e`). Tags 2.1.4 to 2.2.2 exist. |
| npm `opencode-linux-x64-musl@1.19.5` | Official platform binary: tarball `https://registry.npmjs.org/opencode-linux-x64-musl/-/opencode-linux-x64-musl-1.19.5.tgz`, integrity `sha512-y6nUbH3ViajN37KaVVy0/KnWv/i48Bx5QPv9Ul9ONJya1ossV7CHVb+ZNFc61M5GqlVtjCvZV7T0eBMqhJG7mg==`, 2 files, 221.6 MB unpacked. |

**Decision.** Target **1.19.5**. The adapter's supported range stays 1.19.0 up to, not including, 2.0.0. The 2.x line changes permission checks and adds a configuration layer, so it needs its own review and verification before it is accepted.

## API compatibility matrix (1.19.5)

Status key:
- **Inferred**: stated by the published 1.19.5 SDK types, and where noted by the server docs. Not seen on a server.
- **Conflict**: the sources disagree.
- **Unknown**: no reliable source.

| Operation or behaviour | Adapter use | Docs | SDK 1.19.5 | Status |
|---|---|---|---|---|
| `GET /global/health` → `{healthy, version}` | Health and version gate | ✓ | ✓ | Inferred |
| `GET /doc` | Contract check | "OpenAPI 3.1 spec" in the text, but "HTML page with OpenAPI spec" in the table, and "view in a Swagger explorer" | Not an operation | **Unknown format** |
| `GET /config` (merged config) | Effective-policy check | ✓ | ✓ | Inferred. Whether `permission` comes back as an object or a list is unknown; the checker accepts both. |
| `POST /session` `{title, permission}` | Create a session | `{parentID?, title?}` | adds `permission?`, `agent?`, `model?` | Inferred. The exact shape of the `permission` rule list is unknown. |
| `GET /session/{id}` | Reuse a session | ✓ | ✓ | Inferred. The status code for an unknown id (404?) is unknown. |
| `GET /session/status` → `{[id]: {type}}` | Busy check, completion | ✓ | idle, busy, retry | Inferred. Whether idle sessions are listed is unknown (the adapter treats absent as idle). |
| `POST /session/{id}/prompt_async` → 204 | Send the brief | ✓ | parts, model, agent, tools | Inferred |
| `POST /session/{id}/abort` | Timeout, cancel, violations | ✓ | ✓ | Inferred. How long a session takes to reach idle after an abort is **unknown**. |
| `GET /session/{id}/message` | Confirm completion, usage | ✓ | adds `limit`, `before` | Inferred. Ordering and the effect of compaction are unknown. |
| `GET /event` (SSE) | Progress and permissions | First event is `server.connected` | Event types | Inferred. Framing and heartbeats are unknown. |
| Permission reply | Reject every request | `POST /session/{id}/permissions/{pid}` `{response}` | `POST /permission/{id}/reply` `{reply}`, plus the older form | **Conflict**. The adapter uses whichever `/doc` lists. |
| `POST /question/{id}/reject` | Reject questions | not listed | ✓ | Inferred (one source) |
| Events: `session.idle`, `session.status`, `session.error`, `message.updated`, `message.part.updated`, `permission.asked`, `permission.v2.asked`, `question.asked`, `file.edited` | Tracking | `server.connected` only | ✓ | Inferred. Which permission events 1.19.5 actually emits is unknown. |
| `directory` query parameter | Pins calls to `/workspace` | not described | on every operation | Inferred |
| `/session/{id}/shell`, `/session/{id}/command`, `PATCH /config`, `PUT /auth/{id}`, MCP, TUI and other groups | **Never called** | ✓ | ✓ | Present, so the deployment must block them (see the security review) |

## `/doc` findings

- **The adapter in the working tree** parses `/doc` as JSON only. An HTML page becomes status PROTOCOL ("did not return JSON"). That is safe, because it fails closed and no prompt is sent, but the status is misleading and gives no hint.
- **New tested module** [`opencode_contract.spec_from_doc`](../backend/app/coding/opencode_contract.py). An HTML answer, detected by content type or by a leading `<`, is **incompatible with a clear message and is never scraped**. Non-JSON is a protocol error, and JSON without `paths` is incompatible. It is not yet wired into the adapter.
- **Resolution.**
  1. The real-server check records `/doc`'s content type and first bytes.
  2. If it is HTML, the check records where the page loads its JSON from.
  3. The adapter then reads the spec from that path, still accepting JSON only.

## Static review of the untested adapter

These are findings only. The adapter file was not changed in this stage.

| # | Severity | Finding | Fix |
|---|---|---|---|
| A1 | **P0** | If `prompt_async` times out or the connection drops after the server accepted the prompt, the adapter returns UNAVAILABLE **without aborting**. The session could keep editing files after DevPilot restores the checkpoint. | Treat any non-4xx failure of `prompt_async` as "possibly sent": abort, then wait for idle. |
| A2 | **P0** | `_abort` sends the abort and returns at once. A write in progress can land after the workspace is restored. | Poll the session status until idle, with a time limit. If idle is not confirmed, stop the OpenCode container and re-snapshot before restoring. |
| A3 | P1 | After `abort_first`, the busy check runs immediately and will likely report BUSY. | Wait for idle, using the same helper as A2. |
| A4 | P1 | An HTML `/doc` is reported as PROTOCOL. | Use `spec_from_doc`. |
| A5 | P1 | SSE lines and events are buffered with no size limit. | Use the tested `SSEParser`, which has a 1 MB cap per event. |
| A6 | P1 | Forbidden tools are a denylist, so a renamed or new shell tool would pass. | Use the tested allowlist (`tool_violation`). |
| A7 | P1 | Completion counts assistant messages from before the prompt. Compaction or a deleted message could shift the count. | Find the new user message, then wait for the assistant message whose `parentID` matches it (the SDK's AssistantMessage has `parentID`). |
| A8 | P2 | Each completion poll loads the whole message history. | Use `limit` once its semantics are confirmed. |
| A9 | n/a | **Duplicate submission.** Reconnects never resend the prompt (a `prompt_sent` flag), and a quiet first read sends it once. The only risk is A1, and it is an un-aborted run, not a duplicate. | n/a |
| A10 | n/a | Cancel and timeout latency is up to `poll_seconds` (5 s) plus one HTTP call. | Acceptable |
| A11 | P1 | The adapter still has its own copies of the version, spec, SSE, path and configuration helpers that `opencode_contract.py` now provides with tests. | Wire the adapter to the shared module. |

## Security review: what the deployment must block

| Capability | Exposure | Controls | Status |
|---|---|---|---|
| Command endpoints (`/session/{id}/shell`, `/command`) | Anyone with the password and network access to the server | The adapter never calls them. A fresh random password per container start. The server binds only inside its container. **An API filter in front of OpenCode that forwards only the operations the adapter needs.** | Adapter side done; filter is **design** |
| Configuration changes (`PATCH /config`, a project `opencode.json`, `.opencode/`) | The API, or files in the workspace | Managed config, which wins **only for the keys it sets** because OpenCode merges key by key. The effective config is checked before every task (tested). Workspaces holding these files are refused (tested). Edits to them are denied and reverted (tested). **The build agent's rules are pinned in managed config (new, tested).** The API filter blocks `PATCH /config`. | Partly tested; filter is design |
| File access outside the project | Tools, `directory` parameter | Only one workspace is mounted, with a read-only root. `external_directory` is denied. The audit checks every change. | Audit tested; container is design |
| Plugins | Config `plugin`, or plugin files under `.opencode/` loaded at start (inferred) | `plugin: []`. `.opencode/` is refused before a task and reverted after one. A fresh container per run means a reverted file is never loaded (P1). | Partly tested |
| MCP tools | Config `mcp`. Managed `mcp: {}` does **not** remove a server defined by a project. | Workspace pre-check. The effective check requires MCP to be absent or disabled. | Tested |
| Subagents | `task` | `task` denied; plan, general and explore disabled | Tested (config) |
| Credential storage (`PUT /auth/{id}`) | The API | The container holds no key (gateway design). Its home directory is on a tmpfs. The API filter blocks `/auth`. | Design |
| External network | webfetch, LSP downloads, auto-update, share | All denied or off in config. The container's only network peer is the gateway. | Config tested; network is design |
| Another project's workspace | A shared server, or a shared network | One container and one internal network per active project. Only that workspace is mounted. Runs are one at a time (RunManager). The container stops when the run pauses or ends. | Design |

**Managed precedence.** The docs place managed files (`/etc/opencode/` on Linux) above project and environment configuration. The sentence saying they "cannot be overridden" appears in the macOS section. Linux behaviour is therefore **inferred**; run 2 of the real-server plan checks it.

## Egress rule assessment (`127.0.0.1:4096` for the backend, uncommitted)

- **Safe in itself.** It allows one loopback port only, so it does not widen internet access. Loopback entries need an explicit port.
- **Premature.** It is correct only if the final deployment exposes OpenCode, or the API filter in front of it, on the host's loopback port 4096. A container on an internal Docker network cannot publish a port, so the filter or gateway would be what listens there.
- **Keep it uncommitted** until the launcher and gateway design is fixed. The verification plan below does not need it.

## Real-server verification plan (awaiting approval; nothing has been run)

**Scope.** Startup, health and version, `/doc` format, the session API, effective configuration, and authentication. No prompts, no model, no generated code.

1. **Version and source.** `opencode-linux-x64-musl@1.19.5`, the official platform binary of `opencode-ai` 1.19.5, from `registry.npmjs.org`. It is checked against the published sha512 before use.
2. **Method.** Run the binary inside `python:3.12-alpine`, which is **already on D:** (musl libc, digest `e6dae589…`). No new image is needed. Alternative: `ghcr.io/anomalyco/opencode:1.19.5`, which needs an image pull of unknown size.
3. **Disk.** The tarball is an estimated 60 to 100 MB; npm does not publish the compressed size. It unpacks to 221.6 MB. That is about 0.3 GB in `D:\DevPilotCache\opencode\1.19.5`. No new Docker layers. The container's filesystem is read-only, and its temp folders are in RAM. Nothing is written to C:.
4. **Container:**
   - `--network none`: only loopback inside, and no port published
   - `--read-only`, plus tmpfs `/tmp` and tmpfs home
   - `--cap-drop ALL`, `--security-opt no-new-privileges`, `--user 65534:65534`
   - `--memory 1g --memory-swap 1g --cpus 1 --pids-limit 256`
   - read-only mounts: the binary at `/opt/opencode`, DevPilot's managed config at `/etc/opencode`, and the check script at `/checks`
   - one writable mount: an empty temporary workspace at `/workspace`
   - no Docker socket, no DevPilot files, no `.env`
   - a random `OPENCODE_SERVER_PASSWORD`, passed through the environment and never printed
5. **Network.** None. Requests run inside the container through `docker exec`, against `127.0.0.1:4096`. With no credentials and no network, no model call is possible.
6. **Checks.** A small standard-library script, which I will write and show you before it runs. Its JSON results are analysed on the host with DevPilot's tested `check_health`, `spec_from_doc`, `check_spec` and `verify_effective_config`.
   - Startup within 30 s.
   - A request without a password is refused (401?), and so is one with a wrong password.
   - Health and version are `1.19.5`.
   - `/doc`: status, content type, first bytes, and the operation list if it is JSON.
   - Sessions: create (with the rule list), get, status, abort, list messages, delete; and the status code for an unknown session id.
   - `/event`: the first event and how it is framed.
   - `/config`: the effective configuration.
   - An inventory of sensitive operations.
   - **Run 2 (optional, same approval).** A fresh workspace containing a hostile project `opencode.json` that allows bash, overrides the build agent and adds an MCP server whose command is `/bin/true`. The check: does the effective `/config` still pass?
7. **Cleanup.**
   - `docker rm -f` on the labelled container, then confirm none remain.
   - Delete the temporary workspace and reports after copying the summary into the docs.
   - Clear the password variable.
   - The downloaded binary is kept or deleted at your choice.

## Remaining Phase 4 interfaces (smallest changes)

| Piece | Interface |
|---|---|
| `ToolContext.call_id` | **Done** (tested): the loop fills it, and `recover` receives the same key. |
| `coding_tasks` table | `id, run_id, task_order, attempt, call_id (unique per run), provider, session_id, status (RUNNING, COMPLETED, REJECTED, FAILED, INTERRUPTED), accepted, changes_json, violations_json, tokens_in, tokens_out, cost_usd, detail, created_at, finished_at`. A new table needs no migration. |
| `generate_code` (ExternalStep, state IMPLEMENTING, risk WORKSPACE_WRITE) | Arguments: `task_order: int`, `instructions: str` (at most 4,000 characters). The steps are:<br>1. `begin`: load the plan task and refuse unknown or non-coding tasks; refuse a workspace that holds engine configuration; cap attempts; mark stale RUNNING rows as INTERRUPTED; build the brief; create a checkpoint keyed by `(run, call_id)`; insert a RUNNING row.<br>2. `run`: run the provider task; confirm idle after any abort; audit; restore on a violation or any status other than COMPLETED.<br>3. `finish`: update the row; add tokens to the run; mark the plan task DONE; discard the checkpoint.<br>4. `recover`: restore the checkpoint and ask the launcher to stop the container. |
| `submit_implementation` (gate) | IMPLEMENTING → VERIFYING. Requires every code and test task to have an accepted attempt, a non-empty workspace and no RUNNING row. VERIFYING then pauses (Phase 5). |
| Orchestrating IMPLEMENTING | `create_app(coding=...)` adds IMPLEMENTING to the orchestrated states only when **both** a coding provider and a sandbox are configured. Otherwise the run pauses as today. |
| Idempotency | Each `call_id` runs exactly once (the invocation claim). `(run_id, task_order, attempt)` is unique. Attempts are capped by `OPENCODE_MAX_ATTEMPTS_PER_TASK`. An interrupted attempt is marked INTERRUPTED on the next `begin`, and its session is aborted and waited on before reuse. |

## Recommendations added in Phase 4B

| Priority | Recommendation |
|---|---|
| **P0** | Fix A1 and A2 (an ambiguous prompt, an unconfirmed abort) before any live coding task. |
| **P0** | Put an API filter in front of OpenCode that forwards only the operations the adapter needs. It blocks the command, config, auth and MCP endpoints even for a caller with the password. |
| **P0** | Run the real-server contract check above, which is the adapter's test route for now. Then confirm `/doc`, the session-status semantics and the time from abort to idle. |
| **P0** | Run one container and one internal network per active project, and stop it when the run pauses or ends. |
| P1 | Wire the adapter to `opencode_contract.py` (A4, A5, A6, A11), and use `parentID`-based completion (A7). |
| P1 | Use a fresh OpenCode container per run, so plugin files reverted by the audit can never be loaded. |
| P1 | **Token efficiency:** return summaries of tool results to GPT, and keep full output in the database (item 7). In IMPLEMENTING, send the task list and criterion codes once, not the full text every turn. |
| P1 | **Task-level context:** the brief already carries only the task's own requirements, plus the criteria. Keep repair briefs to the failing tests and the diagnosis. |
| P1 | **Independent verification:** after each accepted task, run the project's tests in the sandbox inside the same tool call (item 8). The verdict comes from the sandbox, never from OpenCode's own report. |
| P2 | Cache the contract while the server version is unchanged, notify cancellation directly, use a git-based checkpoint, and build the install proxy (items 12 to 14). |
