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

> **Superseded by Phase 4C below.** Two things changed. The binary cannot run in `python:3.12-alpine` alone, because it needs `libstdc++`. And run 2 no longer adds an MCP server: only inert permission and agent text is used.

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

---

# Phase 4C: OpenCode binary verification and check preparation (2026-10-30)

**What happened in this stage:**
- The official binary was downloaded and verified.
- The real-server check was written and dry-run.

**What did not happen:**
- No OpenCode server was started and no check container was run.
- No image was built or pulled, and no paid call was made.
- The HTTP adapter is still **untested** (0%) and unchanged. Downloading and statically checking the binary does not verify the adapter.

## Binary download (done, verified)

| Item | Value |
|---|---|
| Source | `https://registry.npmjs.org/opencode-linux-x64-musl/1.19.5` (version document), then its canonical tarball `…/-/opencode-linux-x64-musl-1.19.5.tgz`. No redirects were followed. |
| Tarball | `opencode-linux-x64-musl-1.19.5.tgz`, 79,167,835 bytes (matches Content-Length) |
| SHA-512 integrity | `sha512-y6nUbH3ViajN37KaVVy0/KnWv/i48Bx5QPv9Ul9ONJya1ossV7CHVb+ZNFc61M5GqlVtjCvZV7T0eBMqhJG7mg==`. **Matches** the registry value and the value pinned in Phase 4B. |
| SHA-1 shasum | `75a1a1be2d1ac95c4c07e4681524846dd0ce85d5` (matches) |
| Tarball SHA-256 | `33fc5689591cbb65049b193ff84f2cb248fe7e3e4052ff54ae12526009e1a4d2` |
| Archive | 2 plain files (`package/package.json`, `package/bin/opencode`). No links, devices, absolute paths or `..`. Unpacked with Python's `data` filter after DevPilot's own entry check. |
| Binary | `D:\DevPilotCache\opencode\1.19.5\extracted\package\bin\opencode`, 221,590,952 bytes, SHA-256 `528058cf8f2a7196ec94dc113052b785f8f94f6802ced3980ad7359369ddacc8` |
| ELF headers | x86-64. Loader `/lib/ld-musl-x86_64.so.1`. Needs `libstdc++.so.6` and `libc.musl-x86_64.so.1`. Read from the headers; never executed. |
| Manifest | `D:\DevPilotCache\opencode\1.19.5\manifest.json`. The check re-hashes the binary against it before every mount. |
| Disk | 300,761,614 bytes (0.30 GB) on D:, covering the tarball, the metadata, the unpacked files and the manifest. Nothing on C:. |
| Not verified | The npm registry's ECDSA signature: no crypto library is installed, and adding one is a new dependency. The SHA-512 integrity comes from the same registry over TLS. |

Code: [`opencode_artifact.py`](../backend/app/coding/opencode_artifact.py) (42 tests), and [`fetch_opencode.py`](../backend/scripts/fetch_opencode.py), which ran once. `--verify-only` re-checks the binary with no network.

**CPU note.** OpenCode's own image uses the "baseline" (non-AVX2) musl build. The plain build here needs AVX2. This laptop's i5-1155G7 has AVX2, so this is low risk. If a run fails with "Illegal instruction", the baseline package is a separate download that needs approval.

## Runtime compatibility: `python:3.12-alpine` cannot run the binary as it is

- **Evidence:** I exported the local image with `docker save` and read it on the host; no container was started. It is Alpine 3.24.3 (ID `sha256:e6dae589…`) with 38 apk packages. **It has no `libstdc++` and no `libgcc`.** The binary needs `libstdc++.so.6`, which in turn needs `libgcc_s.so.1`. OpenCode's own Dockerfile installs `libgcc libstdc++ ripgrep` on Alpine.
- **Proposal: build a check image.** The [Dockerfile](../backend/scripts/opencode_check/image/Dockerfile) is the pinned base (`python:3.12-alpine@sha256:e6dae589…`) plus `apk add --no-cache libstdc++ libgcc`, with user 65534.
  - apk verifies package signatures with the Alpine keys already in the base image.
  - The download is about 1.5 MB, and the new layer is a few MB.
  - The image holds **no OpenCode code**: the verified binary is mounted read-only.
  - After the build, the check confirms that the image is exactly the pinned base plus one layer.
  - **Building needs approval:** it is a package download and a build container with network access.
- **Alternatives (not recommended now):**
  - `ghcr.io/anomalyco/opencode:1.19.5` pinned by digest. It is a new image pull of unknown size, and it has no Python, so the probe would need a sidecar container.
  - Fetching the two `.apk` files and checking their signatures with custom code.

## Container configuration (prepared, not run)

One container per server start, built by `build_run_args` (tested). `docker inspect` is checked against every row below after the start (`isolation_problems`, tested). Results from a container that fails this check are not trusted.

| Setting | Value | Why |
|---|---|---|
| Image | `devpilot/opencode-check:1.19.5`, `--pull never` | Nothing is downloaded implicitly |
| Network | `--network none`, no `-p`, no published port | Only loopback inside. The server is unreachable from the host and the internet. |
| Root filesystem | `--read-only`, plus `--tmpfs /tmp:rw,noexec,nosuid,nodev,size=256m,mode=1777` | HOME, XDG folders and TMPDIR live in the tmpfs, which is RAM and gone on removal |
| Privileges | `--cap-drop ALL`, `--security-opt no-new-privileges`, `--user 65534:65534`, `--init` | Non-root and no capabilities |
| Limits | `--memory 1g --memory-swap 1g --cpus 1 --pids-limit 256 --ulimit nofile=1024:1024` | Bounded resources |
| Mounts | Binary folder → `/opt/opencode` (ro); managed `opencode.json` → `/etc/opencode` (ro); probe and `input.json` → `/checks` (ro); a fresh empty folder → `/workspace` (the only writable mount) | No Docker socket, no repository, no real workspace, no `.env`. `mount_problems` refuses anything else. |
| Server | `/opt/opencode/opencode serve --hostname 127.0.0.1 --port 4096 --print-logs`, workdir `/workspace` | Loopback only |
| Password | `secrets.token_urlsafe(32)`, fresh per start, passed as `--env OPENCODE_SERVER_PASSWORD` **by name**, so docker reads it from its own environment | It never appears on a command line, in the report or in saved logs (logs are redacted, including the base64 auth token) |
| Environment | HOME and XDG under `/tmp/home`, `BUN_RUNTIME_TRANSPILER_CACHE_PATH=0`, `OPENCODE_DISABLE_AUTOUPDATE`, `_MODELS_FETCH`, `_LSP_DOWNLOAD`, `_DEFAULT_PLUGINS`, `_CLAUDE_CODE`, `_EXTERNAL_SKILLS`, `OPENCODE_PURE`, and `OPENCODE_DISABLE_PROJECT_CONFIG` (except run 2a) | No credentials. The flags come from the current CLI docs, which may describe a newer release; an unknown flag is ignored, and the probe observes the effect. |
| Managed config | `managed_config()` with placeholder model `openai/devpilot-contract-check` and gateway `http://devpilot-model-gateway.invalid/v1` | No key. No prompt is sent, and with no network nothing could be reached. |

**Authentication and networking.**
- The Windows host makes **no** HTTP request to port 4096.
- The host runs `docker exec --user 65534:65534 <container> python3 -I /checks/probe.py --wait 60`. The [probe](../backend/scripts/opencode_check/probe.py) is a separate process that uses only the standard library. Its script is mounted read-only and kept apart from the application under test.
- The probe calls `127.0.0.1:4096` inside the container's own network namespace, using HTTP basic auth with the password from the container environment.
- It has a fixed allowlist (tested). Its only POSTs are `/session` and `/session/{id}/abort`. It never sends a prompt or a command, and never calls the shell, config-change, auth, MCP, TUI or PTY endpoints. It never follows links.
- It prints raw observations as JSON. The judgment is made on the host by [`opencode_check.analyze`](../backend/app/coding/opencode_check.py) (tested), which reuses `check_health`, `spec_from_doc`, `check_spec`, `SSEParser`, `verify_effective_config` and `ruleset_problems`.

## Commands for later (each step needs its own approval)

```powershell
cd D:\DOWNLOAND_NEW_D\AI\AI; . .\scripts\dev-env.ps1; cd backend
& $env:DEVPILOT_PYTHON -I scripts\opencode_contract_check.py                      # dry run: prints the exact docker arguments (done, starts nothing)
& $env:DEVPILOT_PYTHON -I scripts\opencode_contract_check.py --build-image        # A: docker build of the check image (apk downloads ~1.5 MB)
& $env:DEVPILOT_PYTHON -I scripts\opencode_contract_check.py --run baseline       # B: one server start, the probe, cleanup
& $env:DEVPILOT_PYTHON -I scripts\opencode_contract_check.py --run precedence     # C: two server starts; refused unless B passed for this binary
& $env:DEVPILOT_PYTHON -I scripts\opencode_contract_check.py --cleanup            # removes this check's containers and run folders only
```

What each run does, in order:
1. Re-hash the binary against the manifest.
2. Check both images and the check image's provenance.
3. Remove leftover containers that carry the label `devpilot.opencode-check=true`.
4. Create `D:\DevPilotCache\opencode-check\runs\<stamp>-<label>-<random>\{etc-opencode,checks,workspace}`.
5. Start the container with `docker run`, then check the applied isolation with `docker inspect`.
6. Run the probe with `docker exec`.
7. Read the server log tail (redacted), its state, and the workspace changes.
8. In `finally`: `docker rm --force`, confirm that no container is left, delete the run folder, and drop the password.
9. Save the report and summary under `D:\DevPilotCache\opencode-check\reports\`.

Exit codes: 0 means no FAILED check; 2 means a check failed; 1 means the run stopped before the server started.

## Contract checklist and expected outputs

Expected values come from the 1.19.5 SDK types and the docs, so they are inferred until a run confirms them.

| ID | Check | Expected | Status rule |
|---|---|---|---|
| C0 | Isolation applied (`docker inspect`) | Every row of the table above | Any difference: FAILED, and the run is not trusted |
| C1 | Server starts | `/global/health` answers 200 within 60 s | Otherwise FAILED, and every later check is UNKNOWN |
| C2 | Exact version | `{"healthy": true, "version": "1.19.5"}` | Any other version: FAILED |
| C3 | Authentication required | 401 for health without a password, health with a wrong password, `/doc` without a password, `POST /session` without a password, and `/event` without a password | Any 2xx: FAILED. A timeout or odd status: UNKNOWN. |
| C4 | Health endpoint | 200 JSON, `healthy: true` | — |
| C5 | `/doc` | JSON OpenAPI 3.1 with `paths` (per the code), or an HTML page (per the docs table) | JSON: VERIFIED. HTML: UNSUPPORTED (the links found are recorded, not followed). Otherwise FAILED. |
| C6a–f | Sessions | Create with DevPilot's rule list → `{id: "ses_…"}`. Get by id. Status is an object keyed by id. Messages `[]`. Abort while idle → 2xx. An unknown id → 404. | Rules refused but a plain session works: UNSUPPORTED (that layer is lost). An unknown id answered as found: FAILED. |
| C7 | Event stream | `text/event-stream`, first event `server.connected` | Wrong content type: FAILED. Other first event: UNKNOWN. |
| C8 | Effective configuration | `GET /config` passes `verify_effective_config` | Any problem: FAILED |
| C9 | Build agent's merged rules | `GET /agent` → the build agent's rules pass, and plan, general and explore are not listed as enabled | Unknown format: UNKNOWN |
| C10 | Sensitive endpoints | Inventory from the spec: shell, command, `PATCH /config`, auth, MCP, TUI, PTY, experimental, instance | Recorded for the API filter |
| C11 | Adapter operations | `check_spec` passes. Which permission-reply form exists. The prompt body fields, and whether a client `messageID` is accepted (for the A1 fix). | Missing operation: FAILED |
| C12 | Tool inventory | `GET /experimental/tool/ids` lists only reviewed tools | Unreviewed tools: UNKNOWN, needs review |
| C13 | Unknown paths | 404 | Served or forwarded (for example the web UI): UNKNOWN, and the API filter must refuse such paths |
| C14 | Workspace untouched | No file created, changed or deleted by starting and probing | Any change: FAILED |

## Configuration-precedence test (run 2, inert)

**Workspace files.** Text only, with no plugin, MCP server, command, hook or code:
- An `opencode.json` that allows `*`, `bash`, `b*sh`, `external_directory` and `webfetch`, allows editing `opencode.json` and `.env`, gives the build agent `*`, `bash` and `b*sh`, re-enables `general`, and sets the marker `username: devpilot-precedence-marker`.
- The same inert agent file, `devpilot-hostile.md`, in both `.opencode/agent/` and `.opencode/agents/`.

**Hypothesis being tested (new finding).** OpenCode merges configuration key by key, and JavaScript keeps a key at the position where it first appeared. The project file can therefore name keys first:
- Managed config still wins every key it sets: `bash` stays `deny`.
- But the merged order is the project's. Under last-match-wins, the project's later `b*sh: allow` beats the managed `bash: deny`.
- An offline test reproduces the merge and confirms that DevPilot's effective-config check **catches** it: bash is not denied, and editing `opencode.json` and `.env` is not denied.

**Runs:**
- **2a:** project configuration allowed. This measures managed precedence alone.
- **2b:** the same files with `OPENCODE_DISABLE_PROJECT_CONFIG=1`.

| ID | Question | Possible results |
|---|---|---|
| P1 | Was the project file loaded (marker present, 2a)? | If not, P2 and P3 are UNKNOWN |
| P2 | Does managed config alone hold? | VERIFIED, or FAILED with the problems and the merged key order |
| P3 | Is any weakening visible to the `/config` check? | FAILED if `/agent` shows weaker build rules than `/config` (the adapter would then also have to check `/agent`) |
| P4 | Is an agent added through `.opencode/` visible to the config check? | FAILED if `/agent` lists it but `/config` does not |
| P5 | Does `OPENCODE_DISABLE_PROJECT_CONFIG` ignore both files (2b)? | VERIFIED, or UNSUPPORTED (marker still loaded), or FAILED |

Whatever the outcome, DevPilot keeps its other layers: it refuses a workspace that holds these files, re-checks the effective config before each task, denies edits to them, and audits and reverts every task.

## Failure handling

The script never retries with weaker settings. Each change below needs approval.

| Symptom | Meaning | Action |
|---|---|---|
| `docker run` fails | Image missing or invalid argument | Stop. Fix and ask again. |
| C1 FAILED, log says `Error loading shared library` | Runtime library missing | Stop. Image problem. |
| `Illegal instruction` | CPU lacks AVX2 | Download the baseline package (approval) |
| C1 FAILED, log shows a `dlopen`/`mmap` failure under `/tmp` | The binary unpacks a native module and needs exec on the tmpfs | Re-run with `--allow-tmp-exec`, which affects that tmpfs only (approval) |
| `EROFS` on a path outside `/tmp` | The server writes somewhere unexpected | Record the path; decide on another tmpfs (approval) |
| Container state shows `OOMKilled` | 1 GB is not enough | Decide on more memory (approval) |
| Probe timeout or crash | Partial report | The partial report is kept; the `finally` block still cleans up |
| C0 FAILED | Isolation not as specified | Results discarded; stop |
| C3 FAILED | Server answers without the password | Stop; never deploy without the filter |
| Cleanup reports a container left | Removal failed | `--cleanup`, then `docker ps -a --filter label=devpilot.opencode-check=true` |

## Disk-space impact

| Item | Size | Where |
|---|---|---|
| Verified binary cache (kept) | 0.30 GB | `D:\DevPilotCache\opencode\1.19.5` |
| Check image (after approval) | One new layer of a few MB on top of the existing 74.7 MB base | Docker data on D: |
| Per run | Under 1 MB of run folder (deleted). Up to 256 MB of tmpfs in RAM (gone with the container). | D:, RAM |
| Reports (kept) | Probably under 2 MB per run (`/doc` and config JSON); unknown until a run | `D:\DevPilotCache\opencode-check\reports` |
| `docker save` export used for the library check | 18.7 MB, deleted | `D:\DevPilotCache\temp` |

## A1 and A2: fix and regression-test plan (prepared, not applied)

**Approach.** Move session stopping out of the HTTP code into a small module that DevPilot tests through a `SessionControl` Protocol:
- operations: `statuses()`, `abort(id)`, `messages(id)`
- tests use an in-memory fake, like `FakeSandbox` — no HTTP server and no HTTP mocking
- the HTTP client implements the Protocol
- no real coding is enabled until these tests pass

**A1, ambiguous submission** (`opencode.py` lines 415–436). Today a failed `prompt_async` leaves `prompt_sent = False`, so line 436 re-raises it as UNAVAILABLE with no abort.
1. Classify a `prompt_async` failure:
   - **REJECTED:** an HTTP 4xx other than 408 or 429. The server did not accept it.
   - **AMBIGUOUS:** a timeout, a transport error after the request was written, a 5xx, a 408 or a 429.
2. If the server accepts a client-chosen `messageID` (C11 checks this), send one per attempt, derived from `(run_id, call_id)`. Reconciliation then looks for exactly that message. Otherwise compare the session's message list with the one recorded just before the send.
3. **AMBIGUOUS is never resent.** Instead:
   - Read the status and messages. A busy session, or a new user message, means the prompt started.
   - In both cases, send an abort and wait for quiescence (below).
   - Report UNAVAILABLE with "the submission was ambiguous; the session was stopped and confirmed quiet", or with "could not be confirmed quiet", which leads to quarantine.
   - Any retry is a new attempt with a new `call_id`, counted against `OPENCODE_MAX_ATTEMPTS_PER_TASK` and decided by GPT through the tool.
4. **REJECTED:** check the status once anyway. Busy means AMBIGUOUS. Otherwise report the error, with nothing to stop.

**A2, restoring before quiescence** (`_abort`, lines 498–521; also A3 at lines 364–371). Today the abort is sent and the outcome returned at once, and the caller restores the checkpoint.
1. Add `settle(control, session_id, workspace_fingerprint, clock, sleep, timeout_s=30, poll_s=0.5, stable_polls=2) -> Quiescence(confirmed, waited_s, polls, last_state, abort_errors)`. It:
   - Sends the abort. An abort is idempotent, so a failed abort request is retried at most 3 times.
   - Polls until, on `stable_polls` consecutive polls, all of these hold: the session is idle or absent, the last assistant message has `time.completed` or an error, and the message and part counts are unchanged.
   - Also requires the workspace fingerprint (paths, sizes, mtimes) to be unchanged across the same window.
2. `CodingOutcome` gains `quiescent: bool`. In `generate_code.run`, the checkpoint is restored **only** when `quiescent` is true.
3. Otherwise the launcher stops the OpenCode container (`docker rm --force`), which is definitive quiescence. The workspace is then re-fingerprinted and restored.
4. If the container cannot be stopped:
   - The workspace is **quarantined**: the `coding_tasks` row becomes QUARANTINED and the project refuses further coding tasks until a person clears it.
   - The tool reports a failure to GPT. It does not restore while writes may continue.
5. `abort_first` (A3) uses `settle` before the busy check.
6. **Late results.** The loop already stores a result that arrives after the run ended as `DISCARDED`. `finish` additionally refuses to mark a plan task DONE unless its `coding_tasks` row is still RUNNING for an active run.

**Regression tests** (fake `SessionControl`, fake clock):
1. A timeout on submit → the prompt is sent exactly once, an abort is sent, polling settles to idle, and the result is UNAVAILABLE with "confirmed quiet".
2. An ambiguous submit where the session was actually busy → abort, wait for idle, "the prompt had started".
3. An HTTP 400 on submit with an idle session → no abort and no restore race. A 400 with a busy session → treated as ambiguous.
4. The abort is accepted but the session stays busy past the time limit → `quiescent` is false, so no restore. The launcher stops the container, and only then does the restore run. If the stop fails, the result is QUARANTINED.
5. The abort request keeps failing → at most 3 retries, then `quiescent` is false.
6. The session is idle but the workspace fingerprint is still changing → not quiescent until it is stable.
7. `abort_first` on a busy session → idle first, and a prompt is never sent while busy.
8. Order: `Checkpoint.restore` is called strictly after `settle` confirms (checked with a call-order spy).
9. A reconnect after an ambiguous submit → still exactly one prompt.
10. The run is cancelled during `settle` → the result is DISCARDED, and the restore still waits for quiescence.

## API filter and model gateway (approved in principle; design only)

```
Windows host                                Docker (one set per active project)
DevPilot backend ──127.0.0.1:4096──▶ [api-filter]  on dp-<p>-ingress (bridge, port published on loopback only)
  (filter token)                          │           and dp-<p>-core (internal)
                                          ▼ basic auth (fresh password)
                                    [opencode]     on dp-<p>-core and dp-<p>-model (both internal): no route out
                                          │ gateway token (fresh, in managed config as apiKey)
                                          ▼
                                    [model-gateway] on dp-<p>-model (internal) and dp-egress (bridge)
                                          │ OpenAI key (only here)
                                          ▼
                                    https://api.openai.com
```

Why the filter is a container: a container attached only to internal networks cannot publish a port. The filter is therefore the single bridge between the host and OpenCode. It is DevPilot's own small code and holds no OpenAI key.

**API filter interface:**
- `POST/GET <allowlisted path>` with `Authorization: Bearer <filter token>`. The token is random per start and held in backend memory. It is compared in constant time.
- Upstream is `http://opencode:4096` with basic auth.
- Allowlist (method, path template, query keys, body rule):
  - `GET /global/health`, `GET /doc` (or the confirmed spec path), `GET /config`, `GET /agent`, `GET /session/status`
  - `POST /session` (body only `title` and `permission`)
  - `GET /session/{id}`, `GET /session/{id}/message`
  - `POST /session/{id}/prompt_async`: `agent` must be `build`, `model` must be the configured one, only text parts, at most 64 KB
  - `POST /session/{id}/abort`
  - `GET /event`: streamed, with a per-event size cap
  - the permission reply (whichever form C11 confirms), whose body can only reject
  - `POST /question/{id}/reject`
- The only query key allowed is `directory=/workspace`.
- Everything else, including unknown paths (C13), gets 403 and an audit line. It is never forwarded.
- Response size caps apply. `GET /devpilot/health` reports the filter's own health.

**Model gateway interface:**
- `POST /v1/<model endpoint OpenCode's OpenAI provider uses>`, with `Authorization: Bearer <gateway token>`. The endpoint (Responses or Chat Completions) is to be confirmed by a later check.
- Rules:
  - The model must equal `OPENCODE_MODEL`.
  - Only `function` tools are allowed. Hosted tools (web search, file search, code interpreter, computer use) are refused.
  - The output-token cap is enforced.
- Budgets:
  - tokens per task (`OPENCODE_MAX_TOKENS_PER_TASK`)
  - requests per run
  - a timeout per request
- Over budget, it answers HTTP 429 so that OpenCode stops, and records the event.
- Upstream is only `https://api.openai.com`: TLS verified, no redirects, proxy environment variables ignored.
- The key reaches the gateway by name from the backend. It is never logged, and every output is redacted.
- **Usage:** one JSONL line per request goes to a write-only ledger mount. Each line has the time, request id, model, input, cached and output tokens, cost in USD (prices from settings) and status, but no prompt text. The backend reads the ledger into the run's token and cost totals.

**Backend interface** (Protocol, faked in tests):
`OpenCodeDeployment.start(project_id, workspace) -> Endpoint(base_url, token)`, `.stop(project_id) -> bool` (also the A2 quarantine step), and `.usage(project_id) -> list[UsageRecord]`.

**Residual risks:**
- The gateway's destination allowlist is enforced in code. Docker cannot restrict a bridge network by destination. P1: route it through the same allowlisting proxy planned for installs.
- The key is visible to `docker inspect` on the host, which is the same trust boundary as `.env`.

## Network architecture and the `127.0.0.1:4096` egress rule

| Component | Listens on | Reached by | Can reach |
|---|---|---|---|
| Backend (host process) | `127.0.0.1:8000` | The browser on this machine | `api.openai.com` (GPT), and the filter at `127.0.0.1:4096` |
| API filter (container) | `:8080`, published as `127.0.0.1:4096` only | The backend, with the filter token | `opencode:4096` |
| OpenCode (container) | `:4096` on the internal core network | The filter, with the password | `model-gateway:8081` only (internal networks have no route out) |
| Model gateway (container) | `:8081` on the internal model network | OpenCode, with the gateway token | `api.openai.com:443` (code allowlist) |
| Contract-check container (Phase 4C) | `127.0.0.1:4096` **inside its own network namespace** | Only the probe, through `docker exec` | Nothing (`--network none`) |

**Recommendations:**
- **The offline contract check does not need the host egress rule**, and does not use it.
- Keep `127.0.0.1:4096` **uncommitted**. Once the API filter exists and publishes on `127.0.0.1:4096`, commit it with the comment "the API filter in front of OpenCode (loopback only)". It will then name the filter, not OpenCode.
- At the same time:
  - change the `opencode` component's allowlist to empty
  - add a `model_gateway` component with `api.openai.com`

That makes the file match who actually connects where.

---

# Phase 4C baseline run against the real server (2026-10-30)

**Approved and done:** the check image was built, and the baseline ran once to completion.

**Not run:** the precedence test, `--allow-tmp-exec`, prompts, models and OpenAI.

**Evidence** (redacted; no password, token or key):
- `D:\DevPilotCache\opencode-check\reports\20261030T071728Z-baseline.json`: the full record, including the 480 KB OpenAPI document and `/config`
- `…-baseline-summary.json`
- `build-image.json`, `build-image.log`

**Overall result: not passed.** Of 21 checks, 18 PASS, 1 FAIL (C8) and 2 NOT VERIFIED (C12, C13). The first report of this run said "17 PASS"; that was a counting mistake, corrected from the saved summary. The single failure is a wrong assumption in DevPilot about the `/config` response, not a server security problem (see C8). Fixed and re-run in the "Phase 4C fix round" below.

## Image build

| Item | Value |
|---|---|
| Image | `devpilot/opencode-check:1.19.5`, ID `sha256:c380fdb5d0cb5cd9a59322029b665bd9c484d7bf861e0d9842893ecde0470bd6`, 78,330,261 bytes |
| Base | Local `python:3.12-alpine@sha256:e6dae589…`. It resolved from the local store: no download lines, metadata in 0.0 s. |
| Packages | `libgcc 15.2.0-r5` and `libstdc++ 15.2.0-r5`, nothing else installed or upgraded. Installed with `apk add --no-cache --no-scripts`, with signature checks on (no `--allow-untrusted`). |
| Provenance | Exactly the base layers plus one; user `65534:65534`; linux/amd64 |
| Disk | New unique data about 4.9 MB (the rest shares the base). Docker's data disk `D:\DockerData\disk\docker_data.vhdx` grew by 67 MB. C: is unchanged (5.91 GB free). |

## Baseline results

The run took 7.7 s from creation to removal.
- **Isolation:** `docker inspect` confirmed network `none`, no port bindings, a read-only root, `CapDrop ALL`, no-new-privileges, user 65534, 1 GiB memory with swap equal to memory, 1 CPU, 256 PIDs, and only `/tmp` as a `noexec,nosuid,nodev` tmpfs of 256 MB. There were four mounts: three read-only and the fresh workspace.
- **Environment:** no secret-like variable names apart from the server password.
- **Time limit:** the runner's timeouts plus a forced `docker rm` bounded the container's life.

| ID | Check | Verdict | Observed |
|---|---|---|---|
| C0 | Isolation applied | PASS | As listed above |
| C1 | Startup | PASS | Healthy after 4.0 s (1.6 s on the first, lost run) |
| C2 | Version | PASS | `{"healthy": true, "version": "1.19.5"}` |
| C3 | Authentication | PASS | 401 with `WWW-Authenticate: Basic realm="Secure Area"` for health without a password, health with a wrong password, `/doc` without a password, `POST /session` without a password, and `/event` without a password |
| C4 | Health | PASS | 200 JSON |
| C5 | `/doc` format | PASS | **JSON**, `application/json`, OpenAPI 3.1.0, 162 paths, 479,667 bytes (`info.version` is "1.0.0"). The docs' "HTML page" description is wrong for 1.19.5. |
| C6a | Session create, no prompt | PASS | 200. Fields: `id` (`ses_…`), slug, projectID `global`, directory `/workspace`, cost, tokens, title, version, time, permission. **DevPilot's rule list was echoed exactly.** |
| C6b | Session get | PASS | 200, same id |
| C6c | Session status | PASS | `{}`: idle sessions are absent, as the adapter assumes |
| C6d | Message list | PASS | `[]` |
| C6e | Abort without a prompt | PASS | 200 `true`; status still `{}` |
| C6f | Unknown session | PASS | 404 `{"name": "NotFoundError", …}` |
| C7 | Event stream | PASS | `text/event-stream`. First event `data: {"id":"evt_…","type":"server.connected","properties":{}}`, followed by a blank line. |
| C8 | Effective configuration (`GET /config`) | **FAIL** | See below |
| C9 | Managed permissions (build agent, `GET /agent`) | PASS | The 101 merged rules pass DevPilot's rule check. Agents listed: build, compaction, summary, title. Plan, general and explore are disabled. |
| C10 | Sensitive operations present | PASS (inventory) | **59**, including `/pty/*` (interactive terminals), `/session/{id}/shell` and `/command`, `PATCH /config`, `PUT /auth/{id}`, `/mcp/*`, `/tui/*`, `/instance/dispose`, and `/experimental/worktree`, `/workspace`, `/console` and `/control-plane`. None was called. |
| C11 | Adapter operations | PASS | Every required operation is present. **Both** permission-reply forms exist (`/permission/{id}/reply` and `/session/{id}/permissions/{id}`), and `/question/{id}/reject` exists. `prompt_async` answers 204. **`messageID` is accepted in the prompt body.** |
| C12 | Tool inventory | NOT VERIFIED | 14 tools: invalid, question, bash, read, glob, grep, edit, write, task, webfetch, todowrite, websearch, skill, **apply_patch**. Not yet reviewed: `apply_patch`, `invalid`, `question`. |
| C13 | Unknown paths | NOT VERIFIED | 200 `text/html`: the embedded web UI answers any path |
| C14 | Workspace untouched | PASS | No file created, changed or deleted |
| C15 | Cleanup | PASS | `docker rm --force` succeeded, no container was left, and the run folder was deleted |

## C8: why it failed, and the smallest fix

**What the server returns.**
- `GET /config` returns the merged permission object with its keys **re-ordered**: `read, edit, glob, grep, list, bash, task, external_directory, todowrite, question, webfetch, websearch, lsp, doom_loop, skill, "*", codesearch`. The declared permission names appear to come first and the rest after.
- DevPilot evaluates that object with last-match-wins. With `"*": "deny"` near the end, it concludes that reading and editing project files are denied, which is wrong and fails closed.

**What the server enforces.**
- The build agent's merged rule list from `GET /agent` keeps DevPilot's **original order**:
  1. OpenCode's built-in defaults (rules 0–13)
  2. DevPilot's global rules with `* deny` first (14–56)
  3. DevPilot's pinned build-agent rules (57–99)
  4. one rule OpenCode appends (100)
- That list passes (C9): bash, web, subagents and external folders are denied, and reading and editing project files are allowed.

**Conclusion.** `/config` is a re-serialized view whose key order does not show evaluation order, so DevPilot cannot judge rule order from it.

**Minimum change (not made; needs approval).**
1. `verify_effective_config` evaluates permission rules only from the build agent's merged list (`GET /agent`). It keeps `/config` only for keys where order does not matter: LSP, formatters, plugins, MCP, share, autoupdate, providers and disabled agents.
2. `GET /agent` joins the adapter's required operations and the API filter's allowlist.
3. Offline tests, then a re-run of the baseline (approval).

This also affects run 2: P2 and P3 must judge rule order from `/agent`, not `/config`.

## Other findings

1. **OpenCode adds rules of its own.** It has an allow-all base (rule 0), `read *.env ask`, and new permission names `plan_enter` and `plan_exit`, all overridden by DevPilot's later `* deny`. It also **appends** `external_directory /tmp/home/.local/share/opencode/tool-output/* allow` after all configured rules. Managed config cannot remove that rule. It covers only OpenCode's own tool-output folder inside the container's RAM tmpfs, so it is accepted and recorded here.
2. **Tools.** GPT models edit through `apply_patch`, which the adapter's `ALLOWED_TOOLS` lacks; the adapter would abort every GPT task. 1.19.5 has no `list`, `patch`, `multiedit` or `todoread` tool. `question` is denied by permission, and `invalid` is OpenCode's handler for malformed tool calls. Review these and update the allowlist with the adapter work.
3. **Unknown paths serve the web UI,** and 59 sensitive operations include interactive terminals. The API filter (P0) is required before any non-test deployment.
4. **Timing.** `/doc` takes 1.4 s for 480 KB, and `/config` 0.3 s. Caching the contract per server version (P2) saves about 1.7 s per task.
5. **`messageID` is accepted,** so the A1 fix can match an ambiguous submission exactly.
6. **The first baseline attempt** ran fully and cleaned up, but the runner crashed while printing a non-cp1252 character, before saving. That evidence was lost. The runner now saves evidence before printing and replaces characters the console cannot show. The identical run was then repeated, with no change to isolation, probe or server.

## Remaining uncertainties
- The real merge in the precedence case (run 2, not approved yet).
- Whether `OPENCODE_DISABLE_PROJECT_CONFIG` and the other environment flags take effect. No project file was present, and the log shows no project config being loaded.
- How long a busy session takes from abort to idle (no prompt was sent).
- Event types during real work.
- Which model endpoint OpenCode's OpenAI provider calls (needed for the gateway).

---

# Phase 4C fix round: effective-policy check corrected, baseline passed (2026-10-30)

**Not done:** the precedence test, prompts, models, OpenAI, the gateway and Phase 4D. The image was not rebuilt; its ID matched the recorded one. The adapter is unchanged and still untested.

## C8: root cause and fix

**Root cause.** DevPilot judged allow and deny decisions from `GET /config` with last-match-wins. On 1.19.5, `/config` returns permission keys re-ordered, with `"*"` second to last, so that order says nothing about evaluation. The order the server enforces is the build agent's merged rule list in `GET /agent`.

**Fix** ([`opencode_policy.py`](../backend/app/coding/opencode_policy.py)):

1. **`agent_rule_problems(GET /agent)`** judges every decision from the ordered, enforced rules. It requires:
   - a list of named agents, with no duplicates
   - only the build agent plus the hidden system agents (compaction, summary, title)
   - a well-formed rule list with known actions
   - the existing must-deny and must-allow samples, now also in the worktree-relative `workspace/…` form OpenCode matches
   - **every permission name DevPilot has not reviewed ends denied** (for example `plan_enter` and `plan_exit` on 1.19.5)
   - no wildcard permission name that allows anything
   - `external_directory` allowed only for OpenCode's own tool-output folder in the tmpfs
2. **`config_problems(GET /config)`** checks only what does not depend on order:
   - LSP, formatters, plugins, MCP, sharing, autoupdate and providers
   - the declared rules, which must equal DevPilot's managed rules **as key/value sets**: no missing key, no changed value, no added key
   - plan, general and explore disabled, and no other agent enabled
3. **`verify_effective_config(config, agents)`** combines both. Called without `/agent`, it **fails closed**, so the untested adapter's old one-argument call refuses every task until it is updated.
4. `GET /agent` joins the adapter's required operations.

**Regression tests (all passing):**
- reordered `/config` keys pass C8, and the test shows why order cannot be read from them
- the correct enforced rules pass (101 rules, as on the server)
- a malicious later allow fails
- an agent-specific override fails even when the declarations look right
- a missing build agent fails
- unexpected permission names (`pty`, `plan_enter` set to ask) fail
- a wildcard permission name fails
- an `external_directory` allow elsewhere fails
- added, duplicate and visible system agents fail
- malformed agent lists and rule lists fail closed
- an empty list, or OpenCode's defaults alone, fail

## `apply_patch` and the tool allowlist (verified in the pinned binary)

The 1.19.5 binary embeds its JavaScript bundle. I read the tool code from it as bytes; nothing was run.

| Tool | Permission it asks | Notes |
|---|---|---|
| `read` | `read` (path relative to the worktree) | `external_directory` first if the path is outside the project |
| `glob`, `grep` | `glob`, `grep` (the search pattern) | |
| `edit` | `edit` (path relative to the worktree) | |
| `write` | `edit` | `external_directory` first for a path outside the project |
| **`apply_patch`** | **`edit`, one pattern per change** (add, update, move, delete) | `external_directory` for every path and every move destination. **The edit patterns list only each change's source path:** a move's destination is never matched against edit rules. |
| `todowrite` | `todowrite` (`*`) | Session storage only |
| `invalid` | none | Returns "the arguments provided to the tool are invalid"; changes nothing |
| `bash` | `bash` (`var Tw="bash"`) | Plus `external_directory` for directories its commands touch |
| `task`, `question`, `webfetch`, `websearch`, `skill`, `lsp` | Their own names | |

- **Hiding from the model.** OpenCode's own function maps `edit`, `write` and `apply_patch` to `edit`. It removes a tool from the model's tool list when the last rule matching its permission is `* deny`. DevPilot's `hidden_from_model` reproduces this.
- **Minimum allowlist** (`ALLOWED_TOOLS`): read, glob, grep, edit, write, apply_patch, todowrite, invalid. `patch`, `multiedit`, `list` and `todoread` were dropped, because 1.19.5 does not have them. No wildcard authorizes anything. bash, terminals, plugins, MCP and subagents remain denied by name and by the catch-all.
- **Auditing every path.** Because a move's destination escapes the edit rules, DevPilot's own snapshot diff is the control.
  - A new test performs one patch's worth of changes on real files: an add, an update, a move onto `opencode.json`, a move into `.opencode/plugin/`, and a delete.
  - The audit lists all six paths and flags both protected destinations and the new protected folder.
- **Matching paths.** Edit and read patterns are relative to the worktree, which is `/` for `/workspace` without git, so rules see `workspace/<file>`. The managed `*/…` deny forms cover this, and the samples now include it.

## C12 and C13

- **C12, tool inventory: now VERIFIED, read-only.** It uses the existing `GET /experimental/tool/ids` and `GET /agent` answers, and invokes no tool.
  - Every listed tool must be in the reviewed mapping.
  - Each allowed tool must be offered and allowed.
  - Each denied tool must be denied and hidden.
  - These decisions are derived from the enforced rules with the logic read from the binary; they are not observed directly.
  - An unreviewed tool still makes C12 NOT VERIFIED.
- **C13, unknown paths: still NOT VERIFIED.** OpenCode answers unknown paths with 200 and its embedded web interface. Refusing unknown methods and paths, whatever status OpenCode returns, is the API filter's job. The filter is not built, so the protection cannot be verified within this scope.

## Reporting reliability

1. **Order.** The raw record is written first, then the summary. Only then is anything printed, so a console failure cannot lose evidence.
2. **Format.** Reports are UTF-8 (`ensure_ascii=False`) and written atomically: a temporary file in the same folder is flushed and fsynced, then renamed over the target. A failure leaves an earlier file intact and no temporary file.
3. **Console.** The console is set to replace characters it cannot encode.
4. **Analysis errors.** If the analysis itself fails, a summary with a bounded reason (2,000 characters) is saved next to the evidence.
5. **Tests.** Regression tests use a strict cp1252 console:
   - unguarded, printing still fails, but only after both files exist intact
   - guarded, it prints `?`

## Baseline re-run against the real server

- **Same assets:** image `sha256:c380fdb5…0bd6` and binary `528058cf…acc8`.
- **Same hardened configuration:** no network, no port, read-only root, no capabilities, user 65534, limits, a fresh workspace, and a fresh password.
- **Evidence:** `reports\20261030T074203Z-baseline.json` and `…-summary.json`. No password, token or key.

| | Previous run (07:17Z) | This run (07:42Z) |
|---|---|---|
| PASS | 18 | **20** |
| FAIL | 1 (C8) | **0** |
| NOT VERIFIED | 2 (C12, C13) | **1 (C13)** |
| Changes | — | C8: FAIL → PASS; C12: NOT VERIFIED → PASS |
| Duration | 7.7 s | 7.1 s (healthy after 3.6 s) |
| Cleanup | complete | complete (no container, no run folder) |

Re-analysing the previous run's saved evidence with the fixed checker gives the same 20 / 0 / 1, so the change comes from the checker, not from a different server.

## Static findings for the precedence test

These were read from the binary and are not yet observed:
- **Project files.** `OPENCODE_DISABLE_PROJECT_CONFIG` is honoured by 1.19.5. It skips project `opencode.json` files and workspace `.opencode/` folders, and with them their agents, commands and plugins. It also skips `AGENTS.md` and `CLAUDE.md` instructions.
- **Config folders.** Without the flag, every `.opencode/` folder between the workspace and the worktree is loaded. OpenCode also starts a dependency install (`@opencode-ai/plugin`) inside each such folder, which would write `node_modules` and `package.json` into the workspace if one existed. This makes the flag, the workspace pre-check and the audit all necessary.

Run 2 confirms the effect.

---

# Phase 4C configuration-precedence experiment (2026-10-30)

**Approved and done:** the two precedence runs, both offline with the baseline hardening.

**Not done:** prompts, models, OpenAI, Phase 4D and any commit. The changes from this round stay in the working tree.

**Evidence** (redacted):
- `D:\DevPilotCache\opencode-check\reports\20261030T080356Z-precedence-*`, including a side-by-side `…-precedence-report.md`
- `20261030T080401Z-precedence-disabled.json`

## Dependency installation: found, then prevented

Read from the binary, for every config folder OpenCode loads:
1. `Config.ensureGitignore` writes a `.gitignore` unless one exists.
2. `Npm.install` then runs an **in-process** npm install of `@opencode-ai/plugin` (`@npmcli/arborist`, `ignoreScripts: true`, no subprocess), **unless the folder is not writable**.

This covers OpenCode's global config folder on every start, and the workspace's `.opencode/` when project configuration is on. The original fixture would therefore have made OpenCode write into the workspace and attempt an install there. It was not run as it stood.

**Guard used in both runs, without changing what is tested:**
- The fixture gets `.opencode/.gitignore`, the same text OpenCode writes.
- The workspace's `.opencode/` is mounted read-only over itself, so OpenCode can still read the agent files.
- OpenCode's global config folder is a read-only folder holding only that `.gitignore`. It is reached through `XDG_CONFIG_HOME=/opt/devpilot/xdg-config`, outside `/tmp/home`, because a mount inside the tmpfs would make `/tmp/home` root-owned.

**Result.** The probe's read-only file listing (new check C16) found no `node_modules`, `package.json`, `package-lock.json` or `bun.lock` in either run. The workspace was unchanged (C14).

**Earlier runs.** The two earlier baseline runs had no guard, so OpenCode most likely attempted the global install into its RAM tmpfs. With no network and scripts disabled, it could download and run nothing, and the workspace was untouched.

## Results

| | A: project config enabled | B: `OPENCODE_DISABLE_PROJECT_CONFIG=1` |
|---|---|---|
| Project `opencode.json` loaded | yes (marker present; log shows `/workspace/opencode.json` and `/workspace/.opencode/…` read) | no (no workspace file read) |
| Agents available (`GET /agent`) | build, **devpilot-hostile**, compaction, summary, title | build, compaction, summary, title |
| Enforced build rules | 103 | 101 (same as the baseline) |
| bash | **allow, and offered to the model** | deny, hidden |
| edit `workspace/opencode.json`, `workspace/.env` | deny | deny |
| edit `workspace/calculator.py` | allow | allow |
| external folders, webfetch, task | deny | deny |
| plan, general, explore | still disabled | disabled |
| DevPilot verdict | **rejected**: C8, C9 and C12 fail with 7 findings | **accepted**: 20 PASS, 0 FAIL, 1 NOT VERIFIED (C13) |
| Dependency install (C16) | none | none |
| Isolation (C0), workspace (C14), cleanup (C15) | pass | pass |

**Why bash became allowed.**
- Managed configuration still won every key it set: `bash` stayed `deny` as a key.
- But the project file named `*`, `bash` and `b*sh` first, so the merged and enforced order became `* deny`, `bash deny`, `b*sh allow`. This happened in both the global and the build-agent parts, at rules 14–16 and 58–60.
- Last match wins, so `b*sh allow` decides for bash.
- **This confirms on the real server the key-order weakness predicted in Phase 4C:** managed configuration alone cannot keep a project file from widening permissions.

**The injected agent.** The `.opencode/agent/` file added a visible primary agent. Its own rules allow bash and editing protected files: its `edit: allow` comes after the global denies. DevPilot prompts only `build`, but a client that names this agent would get those rights.

**Precedence checks:**
- **P1 PASS:** the project file was loaded.
- **P2, P3 and P4 FAIL, which are the intended findings.** The enforced rules and the declarations were both weakened, and an agent was added. DevPilot's checker caught all of it.
- **P5 PASS:** the flag removed every effect.

## Conclusions and how each is known

No coding prompt was sent and no model was called in either run. The containers had no network, held no credentials, and invoked no tool. Each conclusion below is tagged with how it is known:
- **Observed:** seen on the running 1.19.5 server in these runs.
- **Binary:** read from the code embedded in the pinned binary, without running it.
- **Requirement:** a design decision that follows from the first two.

| # | Conclusion | Basis |
|---|---|---|
| 1 | With project configuration on, a project `opencode.json` changes the enforced permissions of OpenCode 1.19.5 | **Observed:** the build agent's enforced rules went from 101 to 103, and bash went from deny to allow |
| 2 | A wildcard permission key (`b*sh: allow`) overrides an explicit `bash: deny` through rule order. Managed config wins each key it sets but not the order, and the last matching rule wins. | **Observed:** the enforced order was `* deny`, `bash deny`, `b*sh allow`. The order mechanism was predicted offline, and the matching rule (`findLast`) is in the binary. |
| 3 | A project `.opencode/agent/` file adds an agent, with its own wider rules | **Observed:** `devpilot-hostile` was listed by `GET /agent`, and its rules allow bash and protected-file edits |
| 4 | DevPilot rejects that configuration | **Observed:** `verify_effective_config` and checks C8, C9 and C12 failed run A with 7 findings, and accepted run B |
| 5 | `OPENCODE_DISABLE_PROJECT_CONFIG=1` stops OpenCode from loading the project `opencode.json` and the workspace's `.opencode/` folder | **Observed:** no workspace file was read in run B, the marker and agent were absent, and the rules matched the baseline. **Binary:** the flag gates both loaders and project instruction files. |
| 6 | Production must **enforce** the flag in every OpenCode container, not rely on detection alone. Detection stays as a second layer, together with the workspace pre-check and the audit. | **Requirement** |
| 7 | The API filter must accept only `agent: "build"` on `prompt_async` | **Requirement:** a project-added agent would otherwise be selectable |
| 8 | OpenCode writes a `.gitignore` into, and starts an in-process npm install in, every writable config folder. A read-only folder that already holds the `.gitignore` gets neither. | **Binary** (`Config.ensureGitignore`, `Npm.install`). **Observed:** no install files in the guarded runs (C16). |
| — | Whether the per-session rule list DevPilot sends is applied after the agent's rules when a tool runs | **Not verified.** No conclusion is drawn about session-level precedence. |

## What this means for Phase 4D

1. **`OPENCODE_DISABLE_PROJECT_CONFIG=1` is mandatory** for every OpenCode container, enforced in the launcher's fixed configuration, not left to detection.
2. **Keep the other layers:** the workspace pre-check that refuses `opencode.json` and `.opencode/`, the effective-policy check before each task, edit denials, and the audit with exact restore.
3. **Use the install guard in the deployment:** a read-only global config folder with `.gitignore`.
4. **The API filter must force `agent: "build"`** on `prompt_async` and refuse any other agent.
5. **Not verified:** whether the per-session rule list DevPilot sends is applied after the agent's rules when a tool runs. It would neutralise a reordering at prompt time. Checking it needs a prompt, or more reading of the binary.
