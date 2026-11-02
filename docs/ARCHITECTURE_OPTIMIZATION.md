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
| **P0** | Fix A1 and A2 (an ambiguous prompt, an unconfirmed abort) before any live coding task. **Done in Phase 4D Rounds 1 and 2.** |
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

## A1 and A2: fix and regression-test plan (applied in Phase 4D Round 1; see the end of this file for what changed)

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
  - `GET /global/health`, `GET /doc`, `GET /config`, `GET /agent`, `GET /session/status`, `GET /experimental/tool/ids`
  - `POST /session` (body only `title` and `permission`)
  - `GET /session/{id}`, `GET /session/{id}/message`
  - `POST /session/{id}/prompt_async`: `agent` must be `build`, `model` must be the configured one, only text parts, at most 64 KB. It refuses `tools`, which would replace the session's rule list, and `system`, `noReply`, `format` and `variant`.
  - `POST /session/{id}/abort`
  - `GET /event`: streamed, with a per-event size cap
  - the permission reply (whichever form C11 confirms), whose body can only reject
  - `POST /question/{id}/reject`
- The only query key allowed is `directory=/workspace`.
- Everything else, including unknown paths (C13), gets 403 and an audit line. It is never forwarded.
- Response size caps apply. `GET /devpilot/health` answers JSON `{"service": "devpilot-api-filter", "healthy": true}`. The adapter requires it before every task, so it refuses a bare server.

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

---

# Phase 4D Round 1: what was built (2026-10-31)

Stages 1–4 only. Committed, except the adapter's use of the lifecycle. Everything below is tested offline. The items marked as binary-derived were read from the pinned 1.19.5 binary as bytes, never by running it.

## Changes from the plan above

| Plan | Built | Why |
|---|---|---|
| Restore when `settle` confirms quiet; stop the container otherwise | **A restore always needs confirmed termination.** Observation is recorded but never permits one. | `/session/status` lists only working sessions, so a prompt the server has not started looks idle. A test shows it fooling the watch. |
| `messageID` derived from `(run_id, call_id)` | A fresh ID in the server's own shape, saved before the send | Saving it gives the same crash recovery. The server orders messages by time and then ID, so the shape is kept. |
| A 2xx means accepted | **Only 204 is accepted** | Unknown paths on this server answer 200 with the web page (C13) |
| `coding_tasks` row | `coding_attempts` table, one row per attempt | Each phase is saved before its step |
| The project workspace is mounted | **A private copy is mounted.** Real workspaces are refused. | Required by Stage 1. The copy step arrives with the launcher. |

## The lifecycle

```
PREPARED -> SUBMITTED -> RUNNING or AMBIGUOUS -> ABORT_REQUESTED -> QUIESCENCE_CHECK
         -> SAFE_TO_RESTORE or QUARANTINED
```

Also: `ABANDONED` (nothing was sent), `REJECTED` (refused and nothing running), `FINISHED`, `KEPT` and `RESTORED`. `session_control._NEXT` is the complete list of moves.

| Fact | Means | Recorded as |
|---|---|---|
| Logical cancellation | The server acknowledged an abort. Writes may continue. | `abort_acknowledged` |
| Observed quiet | The session and workspace looked still. Evidence only. | `observed_quiet` |
| Confirmed termination | The container is confirmed gone | `process_terminated` |
| Verified restoration | The workspace equals the checkpoint, by content | `restore_verified` |

Every stop takes the same path, whatever caused it: abort (at most 3 tries), watch, terminate, then confirm the workspace is still. Without confirmed termination the attempt is QUARANTINED. Nothing leaves that state by itself, and the project refuses new attempts until a person clears it.

## Installation paths

| Path | Trigger | What is known | Evidence |
|---|---|---|---|
| Global config folder (`Npm.install`) | Server start | **Attempt prevented:** a read-only folder returns before arborist runs | Observed, guarded baseline (C16) |
| Project `.opencode/` | Server start, if it exists | **Attempt prevented:** the flag removes it from `ConfigPaths.directories`; a work copy holding it is refused | Observed, precedence run B |
| `~/.opencode` | Server start, if it exists | **Attempt prevented while absent:** fresh tmpfs, `external_directory` denied, C16 fails if it appears | Observed absent |
| Provider SDK (`Npm.add`) | First use of an unbundled SDK | **Attempt prevented:** `@ai-sdk/openai` is in the bundled table, checked first. `npm` overrides are refused. All 83 listed models use it (C17). | Binary; the load itself is **not exercised** |
| Plugin package (`Npm.add`) | A `plugin` entry | **Attempt prevented:** `plugin: []` (C8), `OPENCODE_PURE=1`, `OPENCODE_DISABLE_DEFAULT_PLUGINS=1` | Binary |
| **Ripgrep download** | First use of the grep or glob tool without `rg` | **Artifact prevented only.** The attempt happens and fails for lack of a network. The production builder refuses images without `rg`. | Binary. **Open:** needs a runtime image (Stage 9). |
| LSP, model catalog, self-update, skills URLs, remote config | Various | **Attempt prevented** by configuration and flags | Binary |

C16 passing means no artifact and no logged attempt were seen. That is an observation: the install path logs only when it fails. What prevents an attempt is the guard.

Environment variables that would reconfigure OpenCode (`OPENCODE_CONFIG`, `OPENCODE_CONFIG_DIR`, `OPENCODE_CONFIG_CONTENT`, `OPENCODE_PERMISSION`, `OPENCODE_AUTH_CONTENT`, `OPENCODE_MODELS_URL`, `OTEL_EXPORTER_OTLP_ENDPOINT`, `BUN_BE_BUN`) are refused by the exact allowlist in `environment_problems`.

## Residual risks

1. **The stop must tell the truth.** A stop that reports "gone" while a quiet writer survives cannot be detected: a writer that wakes after the last reading is invisible. Stage 9 must confirm with an independent `docker inspect`, not the exit code of `docker rm`.
2. **`recover` is not wired in.** Stage 10 must call it at backend startup.
3. **Every failed attempt costs a container.** Termination is required even when everything looks quiet. The cost is about 5 s per restart, accepted until a live run measures abort-to-idle.
4. **Not measured:** abort-to-idle time on a busy session, session-level rule precedence, and the adapter's HTTP layer.

---

# Phase 4D Round 2: adapter, allowlist and what remains (2026-10-31)

Committed, without the `127.0.0.1:4096` egress line. Items marked as binary-derived were read from the pinned 1.19.5 binary as bytes, never by running it.

## Findings

| # | Finding | How it is known | What changed |
|---|---|---|---|
| 1 | **A prompt's `tools` field replaces the session's whole rule list** (`SessionPrompt.prompt`: `H.permission=he; setPermission(...)`) | Binary | Removed from the adapter. Sessions are compared with the exact list. **The API filter must refuse `tools`.** |
| 2 | The server sends `null` for optional fields its schema types as boolean, number or string | Observed, 7 fields on the `build` agent | Reported by the check. Nothing DevPilot reads depends on them. |
| 3 | A model asking for a hidden tool makes a `pending` part under that name, repaired to `invalid`. Only `tool-call` sets the name for execution. | Binary | `pending` is not a violation; every later state is |
| 4 | `invalid` returns a fixed text, asks no permission and is left out of `activeTools` | Binary | Allowed, as a tool that executes nothing |
| 5 | The loop exits when the newest assistant's `parentID` is the newest user message, its `finish` is not `tool-calls` or `unknown`, and no tool is going | Binary | `turn_finished` uses the same rule |
| 6 | `apply_patch` reports `metadata.files[].movePath`; its parser reads `*** Move to:` at the start of a line | Binary | Both are read, so destinations are judged |

## Tool review (pinned 1.19.5)

| Tool | Decision | Permission | Note |
|---|---|---|---|
| `read` | Allowed | `read` | Reads one file |
| `glob`, `grep` | Allowed | `glob`, `grep` | Run ripgrep (see the runtime image below) |
| `edit` | Allowed | `edit` | One file |
| `write` | Allowed | `edit` | One file |
| `apply_patch` | Allowed | `edit` | Add, update, move, delete. `edit` is asked only for sources, so destinations are judged by DevPilot. |
| `todowrite` | Allowed | `todowrite` | OpenCode's own task list; no project file |
| `invalid` | Allowed, executes nothing | none | See finding 4 |
| `bash`, `task`, `webfetch`, `websearch`, `skill`, `question` | Refused | their own | Denied, hidden from the model, and a stop if one runs |
| `lsp`, `plan_exit`, `codesearch` | Refused | their own | Defined in the binary, not listed by this server |
| Anything else, including MCP tools | Refused | n/a | Not on the allowlist |

Reading events is a tripwire: the tool may already have written. The audit of real files is the authority on what changed. Neither replaces isolation: the audit runs afterwards, and the container's lack of network and its private copy are what limit the damage.

## Read-only check on the real server

The container has no port, so `RelayTransport` carries each request through `docker exec -i` into `http_relay.py`, inside the container. The adapter's real `OpenCodeClient` runs on top, unchanged. The password travels on stdin, never on a command line. Two independent layers refuse anything but an allowlisted GET.

| Verified on the real server | Not verified |
|---|---|
| Health, `/doc`, `/config`, `/agent`, `/session/status`, `/experimental/tool/ids`, `/event` | `POST /session` through the client (a saved real answer passes `parse_session`) |
| Answers match the server's own schema | `prompt_async` answering 204, and `messageID` being honoured |
| The effective-policy and inventory checks pass on real answers | Real tool and message events: they come from the schema and the binary |
| 404, 401 and HTTP 200 HTML are each handled correctly | `abort` on a busy session, and abort-to-idle time |
| The adapter refuses to drive a bare server | Session-level rule precedence |

## What the next stages must do

**1. The launcher must confirm termination independently.** `terminate()` returns True only if a separate read shows the container gone. The exit code of `docker rm` is not that read, and **`DockerCli.inspect` cannot be reused as it stands: it returns `None` for any failure, so an unreachable Docker daemon would look like a container that is gone.** The launcher must see "No such container" specifically, and treat everything else as unconfirmed, which quarantines.

**2. Recovery at backend startup.** Nothing calls `recover` yet. In the `lifespan` of `main.py`, beside `_sweep_sandbox` and before any run can advance:
1. Read `store.unresolved()`.
2. For each, build a guard with that project's launcher and a control. If the server is gone, the control simply fails, and `recover` goes straight to termination.
3. Call `guard.recover(control)`. It sends no prompt.
4. If the result is `SAFE_TO_RESTORE`, find the checkpoint and call `guard.restore`.
5. Report any quarantined project. Nothing clears it automatically.

This must finish before `loop._recover_interrupted` reports the call to GPT, so that a retry does not meet a held project. Quarantine still needs an approved way for a person to clear it.

**3. The adapter cannot code without the API filter.** Enforced, not assumed: the first step of every task requires `GET /devpilot/health` to answer as the filter. A bare server answers with its web page and the task ends INCOMPATIBLE before anything else, which the real-server run showed. Three other gates stand behind it: no runtime image is approved, no launcher exists, and `generate_code` is not registered. Limit: this shows a filter answers, not that it carries all traffic. The filter's own token must do that in Stage 7.

**4. The runtime image still needs ripgrep.** `grep` and `glob` are allowed tools, so the first real task would try to download it from github.com. C18 stays NOT VERIFIED and the production builder refuses any image without `rg`. Building that image needs approval.

**5. C13 stays NOT VERIFIED** until the API filter refuses unknown paths. The server answered 200 HTML again in this run.

**6. A private, disposable workspace.**
- **Create:** per attempt, under a work root on D:, outside the repository. Copy the project with `take_snapshot`, following no links, after `preexisting_violations` has passed.
- **Mount:** only the copy. The real workspace is never in a container.
- **Accept:** audit the copy. Then DevPilot copies only the audited changes into the real workspace through `WorkspaceFS`, after checking that the real files are still as they were snapshotted.
- **Discard:** on any failure, after termination is confirmed, delete the copy. The real workspace was never touched, so nothing needs restoring there.
- **Quarantine:** keep the copy for inspection and hold the project. The real workspace stays safe even then.
- **Clean up:** delete on success, and sweep leftovers at startup. Apply the workspace file limits so copies stay small on D:.
- **To verify in Stage 9:** that user 65534 can write to a bind mount on Docker Desktop. Nothing has written to one yet.

**7. The API filter's allowlist** gains `GET /experimental/tool/ids`. It sits under the sensitive `/experimental` prefix and is the one such operation DevPilot needs. `prompt_async` must also refuse `tools`, `system`, `noReply`, `format` and `variant`. A second API tree exists under `/api/*`; the filter's default refusal covers it.

---

# Phase 4D Round 3: API filter, termination and what remains (2026-10-31)

Committed after review. Stages 5 and 6 are committed as `53070e8`. Neither includes the `127.0.0.1:4096` egress line. No prompt, model call, paid API, image build, download or network change was made. The gateway, launcher and `generate_code` are not built.

This section replaces items 1, 3 (the token), 5 and 7 of "What the next stages must do" above.

## Termination

Captured from real Docker 29.9.2:

| Command | Container missing | Daemon unreachable |
|---|---|---|
| `docker container inspect` | exit 1, `Error response from daemon: No such container: <ref>` | exit 1, `error during connect: ...` |
| `docker inspect` | exit 1, `error: no such object: <ref>` | not captured |
| `docker rm -f` | **exit 0, no output** | not captured |

So the exit code of `docker rm` proves nothing, and only one exact line means "gone".

| Outcome | Meaning | Confirms the stop? |
|---|---|---|
| `RUNNING` | running, paused, restarting or removing | No |
| `STOPPED` | exited, dead or created, with `running=false` and PID 0 | Yes |
| `NOT_FOUND` | Docker's exact line for this exact reference | Yes |
| `UNKNOWN_ERROR` | anything else: daemon down, timeout, permission error, unreadable answer, a contradiction, another container's identity | **No** |

`DockerCli.inspect_container(reference, expected_id=...)` never raises. `ContainerTerminator.terminate()` refuses anything but a full 64-character ID, reads the state, asks Docker to remove the container, and confirms only from a separate read. It retries, and an unconfirmed stop stays unconfirmed. `AttemptGuard` accepts only `confirmed is True`, so anything else quarantines, and the workspace is not restored.

The failure reason is kept. The evidence of the stop goes into its own redacted column, and the container's ID into another, so a restarted backend can stop the right container.

The old `DockerCli.inspect` had one caller, the sandbox's final state. It is now `final_state`, and behaves the same for that caller: `None` still ends in an error, not a pass. `DockerCli.remove` keeps its meaning.

**Limit:** a stopped container counts as terminated, because it runs no process. Nothing in the launcher uses this yet.

## API filter as built

`backend/edge/filter_policy.py` decides, on bytes, with no sockets and no files. `backend/edge/filter_server.py` moves bytes. Both use only the standard library and neither imports `app`, so the filter can run from a read-only mount.

**Order of checks:** framing, authentication, route, body. A caller without the token learns only the status, not which rule was broken.

| Method and path | Directory query | Body | Answer the filter accepts |
|---|---|---|---|
| `GET /devpilot/health` | none | none | Answered by the filter itself |
| `GET /global/health`, `GET /doc` | none | none | 200 JSON |
| `GET /config`, `/agent`, `/experimental/tool/ids`, `/session/status` | required | none | 200 JSON |
| `GET /session/{ses_}`, `/session/{ses_}/message` | required | none | 200 JSON |
| `GET /event` | required | none | 200 `text/event-stream` only, limited per event |
| `POST /session` | required | `title`, `permission` | 200 JSON |
| `POST /session/{ses_}/prompt_async` | required | `agent`, `model`, `parts`, optional `messageID` | 204 |
| `POST /session/{ses_}/abort` | required | none | 200 JSON |
| `POST /permission/{per_}/reply` | required | `reply`, optional `message` | 200 JSON |
| `POST /question/{que_}/reject` | required | none | 200 JSON |

A 400 or 404 in JSON also passes on every forwarded operation except the event stream, so the adapter can tell "missing" from "refused". The filter answers anything else from upstream with a generic error of its own.

**Body rules:**
- **Session:** exactly `title` (at most 200 characters, no control characters) and `permission`, which must equal the approved rule list.
- **Prompt:** `agent` must be `build`. `model` must equal the approved provider and model. `parts` must be exactly one `{type: text, text}`, at most 64 KiB. `messageID` must have the server's shape. `tools`, `system`, `noReply`, `format`, `variant` and anything else are refused.
- **Permission reply:** `reply` must be `reject`.
- **JSON:** duplicate keys, `NaN`, excess nesting and a wrong content type are refused.

**Authentication.** `Authorization: Bearer <token>` with a per-run token of at least 32 characters, read from `DEVPILOT_FILTER_TOKEN`. It is compared with `hmac.compare_digest` over SHA-256 digests, and exactly one header must be present. The OpenCode password stays inside the filter, which adds it to each forwarded request. The client's token is never sent on.

**Bypass protections:**
- Nothing is forwarded as received. The target is rebuilt from the route, the query is written in one form, the headers are a fixed list, and the body is re-serialized from the checked fields.
- Each request gets a new connection upstream, and each client connection carries one request. Request smuggling has nothing to smuggle into.
- The filter parses HTTP itself, strictly: CRLF only, no folding, HTTP/1.1, origin form.
- `%`, `\`, `;`, `#`, empty segments, `.` and `..` are refused, not decoded. An ID must match its exact shape.
- Conflicting or malformed `Content-Length`, any `Transfer-Encoding`, `Upgrade`, `Expect`, method-override and rewrite headers, a body on an operation without one, and bytes after the body are refused.
- Answers are limited to the expected status and type, so an HTML page never reaches the backend. The client sees only four fixed headers.
- The JSON audit line holds the method, path, status and reason, and no header value, query or body.
- Limits: head 16 KiB, 48 headers, request 128 KiB, answer 8 MiB, event 8 MiB, 15 s for the client, 30 s upstream, a stream 30 s idle and 15 minutes in all, 32 connections.

**Changes to the adapter.** `OpenCodeClient` takes a filter token, and Basic login remains only for the read-only check on a bare server. A 403 now means the filter refused the request, a protocol failure, not bad credentials. `OpenCodeProvider.from_settings` is removed: it had no callers, and static credentials contradict a per-run token.

## C13 and the isolated run

One approved run: **42 PASS, 0 FAIL**. Same guarded container as the baseline, confirmed by `docker inspect`: no network, no ports, read-only root, no capabilities, user 65534, no provider credential. The container held the real OpenCode, the real filter, and a small witness between them that logged each request line reaching OpenCode.

**C13 is now verified at the filter:** all 8 unknown paths answered 403. None returned the web page.

| Refused | Count | Status |
|---|---|---|
| Other operations in the server's own spec, including `/api/*` | **175 of 188** (13 allowed) | 403 |
| Unknown paths, unknown methods | 8, 9 | 403 |
| Encoded or normalized paths | 19 | 403 |
| Unapproved directories | 20 | 403 |
| Prompts (tools, other agents and models, file, agent and subtask parts, and more) | 18 | 403 |
| Sessions, permission grants | 7, 2 | 403 |
| Missing or wrong credentials | 7 | 401 |
| Ambiguous framing | 13 | 400 |
| Oversized body, oversized head | 1, 1 | 413, 431 |

The witness counted 10 requests before and after the refusals, so none of the 280 reached OpenCode. All 15 lines it saw were allowed operations. No prompt was sent, and the only POST targets were `session` and `abort`.

Allowed traffic went through the adapter's real client, with the gate, contract, policy, tool and event checks all passing. One inert session was created through the filter and passed `parse_session` with the exact rules, then an idle abort answered `true`. The filter's token got 401 directly on OpenCode. No credential is in the audit, the witness log or the report. Evidence: `D:\DevPilotCache\opencode-check\reports\20261031T064332Z-filter-check.json`.

## Limits of this evidence

- **Production network isolation is not proven.** The filter and OpenCode shared one container and one user. Anything in that container could have reached OpenCode directly. That proof belongs to the launcher stage.
- `docker exec` relays replaced the host socket hop, so real TCP timeouts and a slow client were not tested on the real path.
- A valid `prompt_async` was never forwarded: the real 204, `messageID` and real tool events remain unobserved.
- `reject_permission` and `reject_question` have never run on a real server.
- Abort-to-idle on a busy session and session-level rule precedence are unmeasured.
- The filter's listener, `HttpUpstream` and `main` ran on the real path, but that run measured no coverage for them (see PROGRESS.md).

## Review of what remains

**1. How the filter deploys with the launcher.**
- One filter container per project set: read-only root, non-root, no capabilities, `no-new-privileges`, limits, the two `edge` files and `filter.json` mounted read-only.
- It joins `dp-<p>-ingress` and `dp-<p>-core`. Only `127.0.0.1:<port>:8080` is published. The launcher must pass `--listen-host`, because the default binds to loopback inside the container.
- The launcher creates the token, the password and `filter.json` fresh for each run, and passes the secrets by name. The backend keeps only the token, in memory.
- It uses the Python image already on disk, pinned by digest, so it needs no new image. That choice needs confirming at the launcher stage.
- The launcher waits for `GET /devpilot/health`, then the adapter's own gate runs. A dead filter makes the attempt fail safe, and the attempt is never resumed through a replacement filter.
- **Design gap:** the design says "one set per active project" and also a fixed `127.0.0.1:4096`, and only one set can hold that port. Decision: one active coding project at a time, and the launcher refuses a second. That keeps the single egress line honest, and suits this laptop. Commit the egress line only when the filter publishes there.

**2. How untrusted clients stay off OpenCode's listener.**
- OpenCode publishes no port and joins only internal networks. Its only peers are the filter (core) and the gateway (model).
- The password exists only in the filter and in OpenCode, and the filter's token is worthless upstream. That last part is shown; the rest is not.
- Generated programs never run in that container, and `bash` is denied, so nothing inside should connect to it. A local process on the host meets only the filter, and needs the token.
- **The launcher stage must prove it with real containers:** `docker inspect` shows no published port; a connection from the host and from the ingress network to OpenCode fails; the gateway's network cannot reach the core network; OpenCode cannot reach anything but the gateway.

**3. Startup recovery: yes.** Item 2 of the earlier list stands, with the stop now specified.
- It goes in `main.py`'s `lifespan`, beside `_sweep_sandbox`, before any run advances and so before `loop._recover_interrupted` reports the call to GPT.
- For each unresolved attempt, build `ContainerTerminator` from the persisted `container_id`, then call `guard.recover`.
- An attempt with no ID, or a daemon that is down, is unconfirmed, so it quarantines. The startup must not fail the whole backend for that, and must not block a project that has no attempt.
- `_sweep_sandbox` removes only containers labelled `devpilot.sandbox`. OpenCode's containers carry `devpilot.opencode`, so recovery alone owns them, and a separate sweep for leftovers must run after it, never before.

**4. Quarantine review and release.**
- A person decides, never GPT and never a timer. It is an authenticated API action, with a recorded reason, and no GPT tool can reach it.
- Release requires a fresh independent read of the persisted ID showing `NOT_FOUND`, at the moment of release. A stale record is not enough, and neither is a person's word.
- It is a dedicated guarded transition, with one audit event. Unknown states stay quarantined.
- With the disposable copy in place, the real workspace was never touched, so release needs no restore. The copy is kept for inspection, and deleted only by that decision.

**5. Gateway credentials and budgets.**
- The OpenAI key lives only in the gateway, passed by name. OpenCode holds a per-run gateway token that is worthless anywhere else.
- It allows only the approved model and `function` tools, refuses hosted tools, and caps output tokens on each request.
- Budgets are enforced in the gateway, because OpenCode cannot be relied on: tokens per task, requests per run, a timeout per request. The check happens before forwarding, and over budget it answers 429. The adapter's own token limit stays as a second layer.
- A request with no usable usage figure counts against the budget at its cap, so missing numbers cannot make spending invisible.
- Upstream is fixed: `api.openai.com`, TLS verified, no redirects, proxy variables ignored. A ledger line per request holds no prompt text.
- It should reuse the filter's pattern: a pure policy, a small byte mover, nothing forwarded as received.

**6. Ripgrep without downloads.** Without `rg`, the first `grep` or `glob` tries github.com, and only the missing network stops it. Two routes: a pinned `rg` in the approved runtime image, or a verified `rg` binary mounted read-only on `PATH`, not on the `noexec` tmpfs. **Recommended: the runtime image,** because it already needs `libstdc++` and `libgcc`, and the production builder already refuses an image without `rg`. Either route needs approval for a download. C18 stays NOT VERIFIED until then. Without a model, the check can only show `rg` is found and runs. It cannot show that OpenCode picks it, which waits for the first live task.

**7. Writable mounts for user 65534 on Docker Desktop: unverified.** Nothing has written to a bind mount as that user. The launcher stage needs a no-model test on a throwaway D: folder: create, write, rename and delete inside the container as 65534, then compare contents and modification times on the host. Modification times matter because DevPilot's stability check uses them. **Fallback if it fails:** a per-attempt named Docker volume, which DevPilot copies in and out. That costs speed but keeps the host's file system further away. The check must not change the user to root to make the test pass.

## Recommended Round 4 scope

1. Review and commit Round 3, without the egress line.
2. **Stage 8, gateway, offline.** Policy and byte mover with tests. No key and no upstream call.
3. **Stage 9, launcher, no model.** Per-project networks, filter and OpenCode containers, the one-project rule, the disposable copy, the isolation proof above, and the 65534 write test.
4. **Startup recovery and quarantine release.**
5. **Decide ripgrep,** with the download it needs.

Keep `generate_code` and the first live prompt for the round after that, once isolation is proven. Round 4 needs approval for: Docker networks, starting the filter and OpenCode containers together, and the ripgrep download or image.

# Stage 2: adaptive tool selection (2026-11-02)

Python only. No Docker ran, no model was called, no key was loaded and the real `.env` was not read.

## The old mechanism

`ProviderSelector` took the first provider, in a fixed order, that declared the capability and said it was available. It read no history, stack or budget, and a tool that had just failed was chosen again. It stays, unchanged, behind `SELECTION_POLICY=fixed`.

## What really exists

There is **one real coding provider**, so no real comparison between providers has happened or can happen yet.

| Group | Tools |
|---|---|
| Implemented and verified on real containers, scripted model | The gates, `run_sandbox_command`, `submit_implementation`, `finish`, and the simulated provider |
| Implemented, not fully verified | `opencode_server`, through `generate_code`: real containers and a scripted model, never a real model. Its quality, duration and cost are unknown, so they are recorded as unknown |
| Planned, cannot run | `opencode_via_n8n`, `gpt_writer`. They appear as refused, and have no launcher |

Selection applies to `generate_code`, the one tool with a choice of provider. Only `python-cli` has an approved test command, so no tool claims `python-fastapi`, `react-vite` or `static-html`.

## The adaptive algorithm

GPT stays the only orchestrator: it asks for a capability, and it cannot name a provider (`GenerateCodeArgs` refuses the field). `selection/adaptive.py` is deterministic code that decides which authorized tool does the work.

**Stage A, eligibility.** A tool that fails any check is out, with every reason recorded. No score can bring it back:

| Refusal | Check |
|---|---|
| `CAPABILITY`, `STACK`, `TASK_KIND` | It cannot do this task |
| `NOT_IMPLEMENTED` | It is planned only |
| `UNAVAILABLE` | Its launcher says it cannot run now |
| `CREDENTIALS` | A credential it needs is not configured. Checked for presence, by name |
| `NETWORK` | It needs a destination the egress policy and `CODING_LIVE_APPROVED` do not allow |
| `SECURITY` | It would run on the host, or could write outside the disposable copy |
| `ENVIRONMENT` | Its execution environment is not approved |
| `BUDGET` | The run cannot pay for its cheapest useful attempt or its expected cost, or has too few tokens. A cost that is unknown under a spending limit is refused |
| `RESOURCES` | The machine cannot commit the memory it needs |
| `SIMULATED` | A real tool is eligible, so a simulation never replaces it. A deployment can forbid simulations outright |

**Stage B, utility.** The rest are ranked by:

```
U = 0.30·C + 0.25·R + 0.15·Q + 0.10·L + 0.10·K + 0.10·M
```

| Term | Meaning | Value |
|---|---|---|
| C | Capability match | Mean of the declared suitability for the capability, the task kind and the stack |
| R | Reliability | `(s + α) / (n + α + β)`, from verified outcomes on comparable tasks |
| Q | Expected quality | Declared, in [0, 1] |
| L | Latency efficiency | `120 / (120 + seconds)` |
| K | Cost efficiency | `0.25 / (0.25 + usd)` |
| M | Resource efficiency | `2048 / (2048 + MB)` |

These weights are the initial configuration, not tuned, and are set by `SELECTION_WEIGHT_*`. They must add up to 1. Every term is in [0, 1].

- **Prior.** α = β = 2, as if four attempts had split evenly. An untested tool scores 0.5, never 1. One result moves R by 0.1, so U by 0.025. Twenty successes in twenty give 0.917.
- **Fixed references** rather than min-max scaling, so adding an unrelated tool cannot change another tool's score, and a lone candidate cannot divide by zero.
- **Unknown numbers** score 0.25, the value of three times the reference, so an unmeasured tool cannot look efficient. The report lists which metrics were unknown.
- **Estimates** blend the declared value with what was measured: `(2·prior + Σx) / (2 + n)`, so one sample cannot replace it.
- **Determinism.** Ties compare scores to 9 digits, then the configured order, then the name. Nothing is random.

Each choice returns the tool, the ranking with its breakdown, every rejection, and an explanation. `confidence` is `n / (n + α + β)`. `compared` and `real_candidates` keep a lone tool, or a comparison of simulations, from reading as a real comparison. The full record goes in the `selection.chosen` event, and GPT gets a short summary in `generate_code`'s result.

## Historical feedback

`tool_outcomes` holds one row per attempt, unique on `(run_id, call_id)`, with tool, task category, evidence, status, failure category, duration and cost. Statistics are computed from the rows, never kept as running totals, so an attempt cannot count twice.

- A kept attempt is only `PENDING`: a file existing proves nothing. Passing tests make it `VERIFIED`, and failing tests make it `FAILED` with the failure's class.
- An attempt that failed is settled at once as `CODING_<status>`.
- `CANCELLED` and `BUSY` are not recorded, and a sandbox that gave no answer leaves attempts `PENDING`: neither says anything about the tool.
- Categories are `capability:stack:kind`, so building, repairing and writing tests are never mixed.
- Evidence is `real` only when a real provider's work was judged by the real `DockerSandbox`. A real tool reads only `real` rows.

## Fallback

A retry remains the decision of `decide_coding`. Selection only says where it goes: a tool that failed this task is ranked behind the others, so another eligible tool is tried first. With no alternative the same tool may retry, inside the existing attempt limit. Each attempt has its own guard and call ID, so A1 and A2 are unchanged: a stop must still be confirmed, and a quarantine or policy breach still ends the run on every tool.

## Benchmark

`python -m app.demo.selection_benchmark`. **Everything here is simulated:** hypothetical tools in a seeded world, 20 seeds of 60 tasks per scenario. The selectors are the real code. Outcomes are never written to `tool_outcomes`.

- "Correct" comes from an oracle that ignores the scoring: the allowed tool with the highest true chance of success, or a refusal if none is allowed.
- Both policies meet identical random outcomes.
- The weights are untuned. Every tool states its true cost, time and memory, and none states a quality.
- Three scenarios have no real execution path, so they report the decision only.

| Scenario | Right answer | Correct: fixed | Correct: adaptive | Violations: fixed | Violations: adaptive | Success within policy: fixed | Success within policy: adaptive | Cost: fixed | Cost: adaptive |
|---|---|---|---|---|---|---|---|---|---|
| python application (fixed preference already best) | best_first | 100.0% | 14.5% | 0 | 0 | 91.7% | 72.0% | $0.200 | $0.114 |
| backend API, decision only | api_specialist | 0.0% | 70.3% | 0 | 0 | n/a | n/a | $0.200 | $0.200 |
| frontend application, decision only | ui_engine | 0.0% | 100.0% | 0 | 0 | n/a | n/a | $0.200 | $0.200 |
| debugging failing tests | debugger | 0.0% | 61.5% | 0 | 0 | 49.3% | 68.0% | $0.200 | $0.200 |
| repository modification, decision only | repo_engine | 0.0% | 100.0% | 0 | 0 | n/a | n/a | $0.200 | $0.200 |
| preferred tool unavailable | up | 100.0% | 100.0% | 0 | 0 | 68.3% | 68.3% | $0.200 | $0.200 |
| budget too small for the preferred tool | cheap | 0.0% | 100.0% | 1200 | 0 | 0.0% | 70.8% | $0.600 | $0.100 |
| safe fallback after a failure | backup | 0.0% | 75.2% | 0 | 0 | 74.2% | 78.9% | $0.300 | $0.281 |
| poor history on the preferred tool | untried | 0.0% | 100.0% | 0 | 0 | 20.2% | 80.4% | $0.200 | $0.200 |
| missing history, worst tool preferred | best | 0.0% | 79.2% | 0 | 0 | 31.1% | 79.2% | $0.200 | $0.200 |
| unsafe tool preferred | sandboxed | 0.0% | 100.0% | 1200 | 0 | 0.0% | 71.0% | $0.200 | $0.200 |
| unapproved credential or network preferred | allowed | 0.0% | 100.0% | 1200 | 0 | 0.0% | 57.0% | $0.200 | $0.200 |
| refusal is the right answer | refuse | 0.0% | 100.0% | 1200 | 0 | 0.0% | 0.0% | $0.200 | $0.000 |

| Each scenario counted equally | Fixed | Adaptive |
|---|---|---|
| Correct selection | 15.4% | 84.7% |
| Simulated success within policy | 33.5% | 64.6% |
| Simulated success counting forbidden tools | 70.9% | 64.6% |
| Policy violations | 4800 | 0 |
| Cost per task | $0.238 | $0.177 |
| Switched tool after a failure | 0 of 598 | 488 of 488 |
| Median time per choice | 1.7 µs | 47.8 µs |

How to read this:
- **The overall figures describe this scenario mix and predict nothing.** In 11 of the 13 scenarios the preferred tool is wrong or forbidden, so fixed preference is set up to lose.
- **Fixed preference wins the control:** 100.0% correct against 14.5%, and 91.7% success against 72.0%. The adaptive policy settles on the cheaper tool and never tries the better one.
- **A first version of this table counted a forbidden tool's success as a success,** which gave fixed preference 70.9%. Success within policy leaves those out, and the raw figure is kept above.
- **A violation by the fixed policy is at selector level.** On the real path a later layer, such as the launcher or the gateway, would stop some of them.
- **Most of the cost saving is not efficiency.** It comes from the budget and refusal scenarios, where the fixed policy spends money it was not allowed to.
- **The time per choice covers the decision only.** The benchmark uses a stub launcher and no memory probe, so it leaves out the history query and the availability check a real choice also makes. Neither was measured.
- **The Phase 7 target is not met:** the p = 0.9 tool was chosen in 85.0% of selections after 50 trials, against 90%, and 17 of 20 seeds met it.

## Limitations

- **No exploration.** The ranking only exploits what it knows, so a better untested tool can be missed for good. That is the cause of both results above: with evidence on both tools, the more reliable one wins even at a higher cost (`test_with_evidence_on_both_the_more_reliable_tool_wins_though_it_costs_more`). §9.1's UCB term would address this, but it was not built: it is not in the agreed formula, and on a paid path exploring costs money.
- **With one real provider, adaptive selection adds refusals and explanations, not a better choice.** The only real effect today is refusing an attempt the budget or memory cannot support.
- **Quality is declared, not measured.** No signal beyond verification exists yet.
- **The simulated benchmark is biased toward adaptive selection,** because 11 of its 13 scenarios make the fixed preference wrong or forbidden.
- **The simulated 90% target was missed,** at 85%. The weights were not tuned to close the gap.
- **Verification judges the whole project,** so every waiting attempt in a run shares its verdict.
- **The memory check reads the host's commit headroom,** which is only an approximation for work inside Docker.
- **The dollar check needs prices.** With `OPENAI_PRICE_INPUT_PER_MTOK` or `OPENAI_PRICE_OUTPUT_PER_MTOK` unset, a run's cost is unknown, so selection can apply only the token check. The existing run and gateway guards are unchanged.
- **§9.1's circuit breaker is not built.** Its `SELECTION_*` lines were dropped from `.env.example`, which no code read.
- **`recover_attempts` still uses the first launcher.** A second real provider would need the provider recorded with each attempt.

**Deferred, because they need Docker:** `tests/integration/test_mvp_end_to_end.py` (7), `tests/integration/test_sandbox_docker.py` (9), and a real-container pass of `generate_code` under the adaptive policy. Real `tool_outcomes` rows cannot exist until a real model runs.

**Tests:** The full offline suite passed on this code: 2,827 passed, 17 skipped and 0 failed, in 102 s, peak 217 MB, one process. The 17 skipped are the opt-in tests: 16 need Docker and 1 calls OpenAI. The real `.env` was never opened, and nothing reached Docker or an external host. 39 deliberate breaks of the selection logic were tried on throwaway copies with no `.env`, and the final tests catch all 39. The first 20, on eligibility and learning, were all caught. The next 19, on tie-breaking, the budget arithmetic, what reaches the selector and how authorization is built, found 5 that no test caught: floating-point ties, remaining tokens reaching the selector, verified history reaching the selector, a verdict staying inside its own run, and only a failure marking a tool as failed. The code was correct in all five. The tests were the gap, and they were added or fixed. `ruff` and `mypy` are not installed, so neither ran.

# Stage 3: intelligent failure recovery (2026-11-02)

**Implemented and tested offline. Not validated on real failures.** Python only: no Docker ran, no model was called, no key was loaded and the real `.env` was not read. GPT stays the only orchestrator.

## Recovery algorithm

A failed attempt or test run goes to `recovery/diagnosis.py`, which names a category with its evidence, a confidence, whether recovery is safe, the actions that could apply, and a fingerprint. `recovery/policy.py` then picks one action: RETRY, REPAIR, SWITCH_TOOL, RECONFIGURE or STOP. It is deterministic, reads no database and starts nothing. GPT gets the decision and writes the fix. It cannot name a tool or a time limit: Python applies those from `runs.recovery_json`.

`RECOVERY_POLICY=intelligent` is the default. `RECOVERY_POLICY=bounded` runs the original `decide_coding` and `decide_verification`, unchanged, for comparison and as a way back.

## Failure categories

Structured signals come first, because program output cannot fake them: the attempt's lifecycle phase, the audit, the sandbox's exit status and the test runner's counts. Text only tells one fault in the code from another.

| Category | Signal | Actions, best first |
|---|---|---|
| `SYNTAX_ERROR`, `TEST_FAILURE`, `MISSING_DEPENDENCY`, `RUNTIME_EXCEPTION` | The test output and the runner's counts | REPAIR, SWITCH_TOOL, STOP |
| `TIMEOUT` | Tests: the limit is never raised | REPAIR, SWITCH_TOOL, STOP |
| | A coding attempt | RECONFIGURE, SWITCH_TOOL, RETRY, STOP |
| `RESOURCE_EXHAUSTION` | The sandbox's memory or output limit | REPAIR, SWITCH_TOOL, STOP |
| | The task's tokens ran out | SWITCH_TOOL, STOP |
| `TOOL_UNAVAILABLE` | The sandbox gave no answer | RETRY, STOP |
| | The provider failed or could not be reached | SWITCH_TOOL, RETRY, STOP |
| `INVALID_CODE` | No tests, an unproved MUST criterion | REPAIR, SWITCH_TOOL, STOP |
| | The audit refused only the size of the output | RETRY, SWITCH_TOOL, STOP |
| `SECURITY_VIOLATION` | A policy breach, refused credentials, an unsafe configuration, any other audit finding | STOP |
| `AMBIGUOUS_STATE` | An attempt not proved undone, an answer that cannot be interpreted, a held project, a test container nobody removed | STOP, keeping the copy |
| `UNKNOWN` | No rule matched. Confidence LOW | REPAIR or RETRY once, then STOP |

**Fail closed.** Only `ABANDONED`, `REJECTED` and `RESTORED` count as undone. Any other phase, including one added later, is unsafe.

**The same failure.** A test failure is known by its failing tests and its messages without paths or numbers. A test run that never ran is known by the sandbox's status alone. A coding attempt is known by its task and status, because a provider's message can name the attempt's own container.

## Decision rules

1. An unsafe diagnosis stops the run.
2. The bounded attempt and repair limits still hold, and a switch counts against them.
3. The first offered action allowed now is taken. Nothing runs unless the selector says a tool can, within budget, tokens and memory. An action needs the time the last attempt took. After the same failure twice in a row, only SWITCH_TOOL is left. SWITCH_TOOL needs another eligible tool. REPAIR is skipped when at least 5 verified repairs of this failure show a smoothed rate under 0.25.
4. If nothing is left, the run stops and says why. If no tool can start at all, the run ends at once with `no_eligible_tool`.

## Defaults

| Setting | Default | Meaning |
|---|---|---|
| `RECOVERY_POLICY` | `intelligent` | `bounded` keeps the original rules |
| `RECOVERY_REPEAT_LIMIT` | 2 | Kept at 2. A limit of 3 did better in simulation (below), but changing it needs unbiased or real evidence |
| `RECOVERY_MIN_REPAIR_RATE`, `RECOVERY_MIN_REPAIR_SAMPLES` | 0.25, 5 | Verified repairs only |
| `RECOVERY_MAX_ATTEMPT_TIMEOUT_SECONDS` | Unset: `OPENCODE_TIMEOUT_SECONDS` | **Escalation is off.** Only a larger value turns RECONFIGURE on. It has been tested, but it is disabled. It never raises a test timeout, a memory limit, the network, a spending limit or the run's deadline |

## Integration with adaptive tool selection

- Recovery asks the Stage 2 selector what can run next, so budget, tokens, memory and the security rules are judged by one set of rules, and a simulation never replaces a real tool.
- SWITCH_TOOL ranks the failed tool last, as Stage 2 does. REPAIR, RETRY and RECONFIGURE keep its rank.
- `tool_outcomes.recovers_from` and `recovery_action` record what each attempt was for. Verification settles them as before, so reliability counts verified results only.
- A1 and A2 are unchanged: a retry is a new attempt with its own call, and a test rerun sends no prompt.
- `ToolError.after_rollback` lets `begin_external` end a run after rolling back a refused call.

## Benchmark (simulated)

`python -m app.demo.recovery_benchmark` prints every scenario. 20 seeds × 50 failures × 28 scenarios, 22 of them recoverable. The decisions are the real code, reading real-format logs, scored against an oracle that knows each scenario's true chances. Both policies meet identical luck.

| Each scenario counted equally | Bounded | Intelligent |
|---|---|---|
| Recovery success | 66.3% | 59.8% |
| Incorrect decisions | 28.9% | 24.2% |
| Attempts per failure | 1.12 | 0.78 |
| Recovery time per failure | 76 s | 58 s |
| Estimated cost per failure | $0.225 | $0.155 |
| Safe termination | 95.0% | 100.0% |
| Unsafe decisions (a later guard would refuse) | 1393 | 0 |

**The intelligent policy recovers less than the baseline: 59.8% against 66.3%**, despite lower cost and no unsafe decisions. It recovered more in 4 scenarios, and the baseline in 15. This table lets a timed-out attempt be given longer where a scenario approves it. **As deployed, with escalation off, the intelligent policy recovers 55.7% against 66.3%, and its incorrect decisions are no longer lower (26.5% against 25.8%).** No claim of better real-world recovery is made.

The table below changes one assumption at a time. It changes no default.

| World | Policy | Recovery success | Incorrect decisions | Attempts per failure | Cost per failure |
|---|---|---|---|---|---|
| each try has the same chance | bounded | 66.3% | 28.9% | 1.12 | $0.225 |
| each try has the same chance | intelligent, repeat limit 2 (the default) | 59.8% | 24.2% | 0.78 | $0.155 |
| each try has the same chance | intelligent, repeat limit 3 | 79.6% | 10.9% | 1.05 | $0.209 |
| a failed try halves the next one's chance | bounded | 58.3% | 27.8% | 1.12 | $0.225 |
| a failed try halves the next one's chance | intelligent, repeat limit 2 (the default) | 58.8% | 24.3% | 0.77 | $0.155 |
| a failed try halves the next one's chance | intelligent, repeat limit 3 | 70.5% | 11.2% | 1.04 | $0.207 |
| no attempt is given longer, as deployed | bounded | 66.3% | 25.8% | 1.12 | $0.225 |
| no attempt is given longer, as deployed | intelligent, repeat limit 2 (the default) | 55.7% | 26.5% | 0.78 | $0.155 |
| no attempt is given longer, as deployed | intelligent, repeat limit 3 | 75.7% | 14.5% | 1.08 | $0.216 |

A repeat limit of 3 recovers more (79.6% at $0.209) in every world. That is an experimental finding, and the default stays at 2.

**Limits of the benchmark.** The scenarios and their chances were written for it, so its figures describe that mix and predict nothing. Each try is assumed independent, which is the kindest assumption for trying again. The classifier's 96.3% is on logs written for the benchmark. The time per decision leaves out the database and selector work a real decision does.

## Tests

Whole offline suite: 3,206 passed, 17 skipped (16 opt-in Docker, 1 live), 0 failed, in 83 s, in one process with a 226 MB peak, and the fence refused nothing. 376 are Stage 3's own (220 classification, 90 decisions, 47 through the real flow, 19 benchmark), plus 3 on the configuration. A targeted check broke 25 safety rules one at a time in a throwaway copy, and the tests caught all 25. The three fixes made after review each have tests that fail without them.

## Limitations

- **No real-failure dataset exists.** No real failure has been classified, and the 40-log corpus §9.2 calls for has not been collected.
- **Only one real coding provider exists,** so SWITCH_TOOL has only moved between simulated tools.
- A retry has no backoff. Progress is judged by fingerprint only, and §9.2's rollback to the last green checkpoint is not built.
- Verification judges the whole project, so every waiting attempt in a run shares its verdict.
- The time check is a lower bound, because `active_seconds` is saved only when a turn ends.

## Deferred Docker verification

- `tests/integration/test_mvp_end_to_end.py` (7) and `tests/integration/test_sandbox_docker.py` (9), under the intelligent policy.
- A real-container pass of `generate_code` through RETRY, where a confirmed stop is real.
- A real unconfirmed stop and a real test container that cannot be removed, ending in AMBIGUOUS_STATE.
- The classifier on real sandbox output, and real `recovers_from` rows, which need a real model.

# Stage 4: final integration and competition demonstration (2026-11-02)

**A working offline demonstration of the whole workflow. Not validated with a real model or on real Docker in this stage.** No Docker ran, no model was called, no key was loaded and the real `.env` was not read.

## Integration audit

The path REST API > orchestrator > selector > coding provider > verification > recovery > history > delivery > report was read in the code and exercised end to end. No defect was found in it. Production builds its selector through `build_selector`, so `SELECTION_POLICY` and `RECOVERY_POLICY` reach every run. Four parts were missing, and were added: independent acceptance, an offline sandbox, a dashboard and an end-to-end evaluation. The old `app.demo.mvp` still uses the Stage 1 selector and needs Docker; the new demonstration uses `build_selector`.

## Independent acceptance

`app/verification/acceptance.py`. Tests written by the coding provider can only show that the code does what that provider meant.

- **Defined first.** The evaluator gives the suite with the project (`POST /projects`, field `acceptance`): criteria AT-001, ... and unittest files whose test names carry the codes. It is checked by parsing, never executed by the API, and cannot be changed later.
- **Frozen.** Each run gets a copy and its SHA-256 when it is created. A copy that no longer matches stops the run with `acceptance_suite_changed`, keeping the copy for review.
- **Hidden.** It is not in any workspace, brief or tool result. GPT learns which criteria failed and what they say, never the tests or their output. Tests check this on the briefs, the files the provider could see, and every tool output.
- **Separate.** It runs in the sandbox after the provider's tests pass, in its own folder: the candidate without its `test*.py` files, then the evaluator's files on top.
- **Evidence.** Per criterion: PASSED, FAILED or UNVERIFIED, the tests, and the runner's own words for a failure. PASSED needs every MUST criterion. Partial completion is reported (for example 4 of 5).
- **Consequences.** A failure blocks delivery and goes to recovery as a TEST_FAILURE whose fingerprint is the unmet criteria. The selector's history learns from the independent verdict.

## Offline execution

`app/sandbox/replay.py`. `ReplaySandbox` starts no process. It answers only when the command and every file byte match a recording, so a verdict belongs to the files really produced. Anything else, including anything a real model writes, gets "needs the Docker sandbox". The six recordings (`app/demo/recordings.json`) come from `python -m app.demo.record`, which runs the sandbox's unittest command on repository fixtures only, and a unit test re-derives each one on every run. Reports name it: "test execution (replayed from recorded runs, not Docker)".

## Competition demonstration

`python -m app.demo.competition` runs six scenarios through the real REST API in about 10 seconds, with structured reports. See [COMPETITION.md](COMPETITION.md).

## Evaluation of three configurations (simulated)

`python -m app.demo.evaluation`. Each episode is a real backend run: REST API, orchestrator, database, the selector and recovery policy that the settings name, the audit, verification, the frozen acceptance suite and the history the selector learns from. **Simulated:** GPT is a stand-in that follows DevPilot's own decisions; the tools are hypothetical, with outcomes drawn from a fixed distribution; test execution is replayed.

**Pre-registered** before the first run (SHA-256 `c8eebcd833e542e920bc0fe913211f0f56e75976330829c7c0df36ac6eb5fe8d`): ten situations, five where the fixed order is already right; a failure mix of test failures 45%, syntax errors 20%, transient failures 25% and unsafe changes 10%; two episodes per situation per seed; development seeds 0 to 4 and held-out seeds 100 to 104; the metrics and the rules for judging a decision. Settings are the deployed defaults: repeat limit 2, timeout escalation off, budgets unchanged. The development split ran first, then the held-out split, with no change to any policy or to the evaluation file in between (its SHA-256 is the same in both results). A success on a tool the policy forbids does not count as a completion.

**Held-out seeds** (100 episodes per configuration):

| Metric | A: fixed + bounded | B: adaptive + bounded | C: adaptive + intelligent (deployed) |
|---|---|---|---|
| Verified completion | 45.0% | 75.0% | 72.0% |
| Seed range | 40.0% to 50.0% | 70.0% to 80.0% | 65.0% to 80.0% |
| Recovery success | 69.6% of 23 | 83.3% of 24 | 70.8% of 24 |
| Incorrect recovery decisions | 26 of 75 (34.7%) | 22 of 69 (31.9%) | 24 of 73 (32.9%) |
| Attempts per episode | 1.60 | 1.44 | 1.35 |
| Simulated time per episode | 124 s | 114 s | 107 s |
| Simulated cost per episode | $0.550 | $0.144 | $0.135 |
| Policy violations | 42 | 0 | 0 |
| Safe termination | 60.0% | 100.0% | 100.0% |
| Raw completion, forbidden tools included | 75.0% | 75.0% | 72.0% |

**Development seeds**, for comparison: verified completion 43.0%, 76.0% and 74.0%; incorrect decisions 37.0%, 29.7% and 29.6%; violations 46, 0 and 0.

Per situation, held-out (completion, and its range over seeds):

| Situation | A | B | C |
|---|---|---|---|
| nothing can succeed | 0.0% (0.0% to 0.0%) | 0.0% (0.0% to 0.0%) | 0.0% (0.0% to 0.0%) |
| preferred tool needs a missing credential | 0.0% (0.0% to 0.0%) | 90.0% (50.0% to 100.0%) | 90.0% (50.0% to 100.0%) |
| preferred tool is weaker | 80.0% (0.0% to 100.0%) | 100.0% (100.0% to 100.0%) | 100.0% (100.0% to 100.0%) |
| preference already best | 100.0% (100.0% to 100.0%) | 100.0% (100.0% to 100.0%) | 100.0% (100.0% to 100.0%) |
| preferred tool over budget | 0.0% (0.0% to 0.0%) | 90.0% (50.0% to 100.0%) | 90.0% (50.0% to 100.0%) |
| preferred tool would run on the host | 0.0% (0.0% to 0.0%) | 100.0% (100.0% to 100.0%) | 100.0% (100.0% to 100.0%) |
| transient failures | 70.0% (50.0% to 100.0%) | 70.0% (50.0% to 100.0%) | 40.0% (0.0% to 100.0%) |
| preferred tool unavailable | 100.0% (100.0% to 100.0%) | 100.0% (100.0% to 100.0%) | 100.0% (100.0% to 100.0%) |
| one good tool | 100.0% (100.0% to 100.0%) | 100.0% (100.0% to 100.0%) | 100.0% (100.0% to 100.0%) |
| nothing is eligible | 0.0% (0.0% to 0.0%) | 0.0% (0.0% to 0.0%) | 0.0% (0.0% to 0.0%) |

How to read this:
- **Adaptive selection is where the measured difference is.** Fixed selection runs tools the policy forbids (on the host, a missing credential, over budget). Those runs are violations, and their "successes" are not counted.
- **The deployed configuration (C) is not the best one here.** Against B it completes 72.0% against 75.0% and recovers 70.8% against 83.3% on the held-out seeds. Most of the gap is in transient failures (40.0% against 70.0%), where the repeat limit stops after the same failure twice. C uses fewer attempts and costs slightly less. This agrees with Stage 3, whose benchmark stands as recorded: **bounded 66.3% against intelligent 55.7%** recovery with escalation off, as deployed. Nothing here replaces it, and no default was changed.
- These figures describe a world written for this evaluation. They are no forecast of real use, and no real-world improvement is claimed.

## Dashboard

`backend/app/ui`, served at `/ui` by the API. Plain HTML, CSS and JavaScript with no package and nothing loaded from elsewhere (a Content-Security-Policy keeps it so). It reads the API with the user's token and never writes HTML from data. It shows the requirement, the plan, the tool chosen and why, coding progress, the provider's tests, the acceptance criteria with evidence, failure classes and recovery decisions, the final state, the delivered files and the report, with a banner naming what was simulated. Its controls are Refresh, the token form, the run list, the delivered files and "Show the full report"; none starts, changes or deletes anything. It was checked by automated tests and visually in the installed Chrome, headless, on loopback (2026-11-02), not by hand.

## Deployment

Proposed, not built: [DEPLOYMENT.md](DEPLOYMENT.md). The dashboard and the API can run on Vercel and Render; real test execution and real coding need a separate Docker worker.

## Tests

Whole offline suite: 3,288 passed, 17 skipped (16 opt-in Docker, 1 live), 0 failed, in 106 s, in one process with a 232 MB peak. The fence refused nothing, and no `.env` was opened outside pytest's temp folders. 82 tests are new in Stage 4: acceptance, the replay sandbox and its recordings, acceptance through the orchestrator, the API, the demonstration, the evaluation and the dashboard. The demonstration ran 6 of 6 scenarios as expected in 6 s, and the evaluation made 600 backend runs (300 per split) with nothing refused.

## Limitations

- No real model has written code, and test execution in this stage is replayed, not Docker.
- **The acceptance suite is not an adversarial security boundary.** It runs in the same Python process as the candidate's code, so code written to subvert the test runner could fake a pass. It catches honest mistakes and self-graded passes; each criterion in its own process would be needed against deliberate cheating.
- The GPT stand-in follows decisions perfectly; a real model may not.
- The evaluation's tools and chances are hypothetical.
- The dashboard was checked in headless Chrome by script, not by a person, and only on the demonstration's data.
