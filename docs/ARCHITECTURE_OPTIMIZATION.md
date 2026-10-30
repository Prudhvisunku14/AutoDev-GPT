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
| 5 | P0 | Recovery | The coding tool cannot find its checkpoint after a crash | Give tools the invocation's `call_id` |
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
