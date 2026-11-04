# DevPilot AI: Security Model

> Status: **design approved in Phase 0 (2026-10-29) and revised the same day** for Google Drive OAuth, repository and document import, the network policy and test validation. These controls are not implemented yet. Each control is built and tested in the phase listed in [PROGRESS.md](PROGRESS.md).
>
> Phase 3 sandbox controls (T1, T2, T13) are implemented. The isolation controls (network, read-only, non-root, capabilities, memory, timeout, cancel, output limit) were verified in real containers on 2026-10-30. The install step's proxy (T13) is not built yet. §11 lists each one with its status.

DevPilot runs code it did not write, written by an LLM, from requirements it did not author. The security model assumes that **generated code, model output, uploaded documents and tool logs are untrusted**.

## 1. Assets to protect

- The host machine: its filesystem, processes and network.
- Credentials: the OpenAI key, OpenCode password, n8n secrets, GitHub token, DevPilot API token, Google OAuth refresh tokens and the token encryption key (`DEVPILOT_TOKEN_KEY`).
- Users' external accounts (GitHub repos, Google Drive files, n8n workflows), and protection from runaway spend.
- The integrity of results, so that "tests pass" means the requirement is actually met.

## 2. Threats and mitigations

| # | Threat | Mitigation | Phase |
|---|---|---|---|
| T1 | Generated code damages or reads the host | Code runs **only** in Docker through `SandboxRunner` with these flags: `--cap-drop ALL`, `--security-opt no-new-privileges`, a non-root user, `--read-only` with a tmpfs, memory/CPU/pids limits, a wall-clock kill, a workspace-only bind mount and no Docker socket. Unit tests use `FakeSandbox`. | 3 |
| T2 | Generated code exfiltrates data | `--network none` for run and test. Network exists only during `install_dependencies`, which runs with no secrets in its environment. The host environment is never forwarded. | 3 |
| T3 | The coding engine runs shell commands | OpenCode runs in a container with `bash: {"*": "deny"}`, `webfetch: deny` and `external_directory: deny`. It can edit `/workspace` only. Permission requests are answered by the PolicyEngine. | 4 |
| T4 | Path traversal through tool arguments | `WorkspaceFS` resolves every path inside `workspaces/<project_id>/`. It rejects `..`, absolute paths, drive letters, UNC paths, and symlinks or junctions that point outside. File-size and file-count limits apply. | 1 |
| T5 | Prompt injection through requirements, docs, files or logs | Untrusted content is passed to GPT as delimited data. Permissions, approvals, limits and gates are enforced **in code**, so model output cannot grant itself a capability. Phase-scoped tool exposure limits which tools are reachable at all. | 2 |
| T6 | A credential leaks | Credentials load only from env or `.env` (pydantic-settings). `.env` is gitignored. A redaction filter masks known secret values and token patterns in logs, events, DB rows and tool results. Secrets are never sent to GPT or placed in a sandbox. Tests assert that no secret appears in the logs or DB. | 1–2 |
| T7 | Unauthorized use of the backend API | A bearer token is required on every route except `/health`, including the SSE stream. Tokens are accepted only in the `Authorization` header, never in URLs. The server binds to `127.0.0.1` by default, and CORS is limited to the dashboard origin. | 1, 2 |
| T8 | Forged or replayed n8n callbacks | HMAC-SHA256 over `timestamp.body`, a 5-minute timestamp window and a constant-time compare. Outbound webhooks go only to allowlisted URLs. | 8 |
| T9 | Unapproved external or destructive actions | The PolicyEngine assigns a risk level to every action. GIT_PUSH, GITHUB_\*, DESTRUCTIVE actions and anything using real credentials **always** go to the human approval queue. EXTERNAL_NETWORK (n8n) needs approval; `N8N_AUTO_APPROVE=true` skips approval only for a workflow its operator declared `read_only` or `notify` in `config/n8n_workflows.yaml`; `write` and `destructive` workflows always wait for a person. | 2, 8 |
| T10 | The model "passes" by editing tests | Accepted tests are frozen with SHA-256 hashes. A diff that touches a frozen test is reverted and recorded as POLICY_VIOLATION. Tests change only through an approved `amend_tests`. | 5 |
| T11 | Runaway cost or infinite loops | `MAX_TURNS`, `RUN_BUDGET_USD`, `RUN_WALL_CLOCK_SECONDS`, `MAX_REPAIR_ITERATIONS`, repeated-fingerprint detection, a stall detector and circuit breakers. | 2, 6, 7 |
| T12 | The GitHub token leaks through git | The token is supplied through an env-based credential helper. It is never written to `.git/config` or remote URLs, and never logged. A fine-grained PAT scoped to specific repos is recommended. | 8 |
| T13 | Malicious dependencies | Installation happens only inside the sandbox, with no secrets present. Later run and test containers have no network. | 3 |
| T14 | OAuth token theft or over-broad access | Least-privilege scope (`drive.readonly`). PKCE and `state`. The refresh token is encrypted at rest with `DEVPILOT_TOKEN_KEY`. Access tokens stay in memory. Revocation endpoint. Tokens never reach GPT, logs, events, plaintext DB columns, sandboxes or reports. (§7) | 8 |
| T15 | OAuth CSRF or redirect abuse | The redirect URI must match the registered value exactly. The callback accepts only a `state` created for the current browser session, verifies the PKCE verifier, and binds to `127.0.0.1`. (§7) | 8 |
| T16 | Malicious documents (parser exploit, zip bomb, macros, external references) | Parsing only in the no-network `document-parser` container with a read-only input. Size, page and decompression-ratio limits. `.docm` and encrypted PDFs are refused. External references are not fetched. Parsed text is data. (§8) | 8 |
| T17 | Hostile repository import (symlinks escaping the workspace, submodules, hooks, LFS, huge repos, prompt injection in files) | Hardened `git clone`: no submodules, hooks off, LFS smudge off, file protocol disabled. The post-clone scan rejects symlinks, junctions and oversize files. Imported content reaches GPT as delimited data. (§10) | 8 |
| T18 | Outbound abuse or SSRF from DevPilot or generated code | Default-deny egress allowlist. Only validated GitHub URLs and Drive file IDs are fetched. Redirects are revalidated. Run, test and parse containers have no network. The install step reaches only package registries, through a proxy. (§9) | 1, 3, 8 |
| T19 | Vacuous or flaky tests that pass without meeting a requirement | Test validation before freeze (ARCH §9.3 step 3a): assertion checks, red-first check, skip rules on MUST criteria and a determinism check. Findings block the gate for MUST criteria. | 5 |

## 3. Risk levels (PolicyEngine)

| Risk | Examples | Default |
|---|---|---|
| READ | `read_file`, `search_docs`, gate tools | allow |
| WORKSPACE_WRITE | `generate_code`, `git_checkpoint` | allow (post-guards apply) |
| SANDBOX_EXEC | `run_tests`, `run_application`, `install_dependencies` | allow (sandbox only) |
| EXTERNAL_NETWORK | `trigger_workflow`, `import_github_repo`, `import_google_drive_file` | Google Drive imports always need approval. Private-repo imports need approval because they use the PAT. `trigger_workflow` needs approval; `N8N_AUTO_APPROVE=true` skips approval only for a workflow its operator declared `read_only` or `notify` in `config/n8n_workflows.yaml`; `write` and `destructive` workflows always wait for a person. Public GitHub clones on the allowlist need no approval. |
| GIT_PUSH / GITHUB | `github_publish` | **always approval** |
| DESTRUCTIVE | delete project, force push, delete remote | **always approval** |

## 4. Audit trail

Each of the following is recorded in `events` and `tool_invocations`, with arguments redacted:
- tool calls
- policy decisions
- approval requests and their outcomes
- selection breakdowns
- failure classifications
- recovery directives
- repository and document imports (source, commit SHA or Drive file IDs, SHA-256 hashes)
- OAuth grants and revocations (never token values)
- test validation findings

The final report includes a summary of approvals and policy refusals.

## 5. Residual risks (accepted for this project)

- **Docker Desktop isolation is not multi-tenant grade.** It uses a WSL2 VM with a shared kernel inside the VM.
- **The install step has network egress.** A malicious package could run during install, inside the sandbox and without secrets.
- **The model gateway holds the OpenAI key and has a route out.** It is the only container with either. OpenCode holds no key and has no route out, and bash and webfetch are denied there.
- **Authentication is single-user.** One bearer token, with no roles or per-user audit identity.
- **Git clone runs on the host.** It uses the hardening in ARCH §8.2, not a container. A flaw in git itself could affect the host. Mitigation: the git version is pinned (currently 2.49.0), and a Phase 8 review decides whether the clone moves into a container.
- **Document parser libraries can have their own vulnerabilities.** The container limits the damage, but it does not remove parser bugs.
- **The egress proxy for install depends on Docker Desktop networking on Windows.** Phase 3 must verify the rule that an unlisted host is unreachable from a container.
- **Google OAuth depends on the user's own client configuration and on Google's policies.** A revoked or expired grant stops imports until the user re-authorizes.

## 6. Developer rules

- Never commit `.env` or real keys. `.env.example` holds placeholders only.
- Never run generated code on the host, including "just to check".
- Live tests that use real credentials or cost money are opt-in (`RUN_LIVE_*`) and require explicit user approval each time.
- Tests never call real Google, GitHub or document services. These are faked with respx and fixtures. Live tests use `RUN_LIVE_GOOGLE_TESTS` or `RUN_LIVE_GITHUB_TESTS`, and each run needs approval.
- Test fixtures use fake tokens only. No test writes a real token to a fixture, a log or a snapshot.
- Offline tests never open the real `.env`. `tests/conftest.py` installs an audit hook that refuses it before it is read, so a test that tries fails. Only an approved `RUN_LIVE_OPENAI_TESTS=true` run lifts it.
- `live_demo.py` (its preflight too), `check_openai_access.py`, `live_smoke_test.py` and `demo_sandbox.py` load `.env`, so the real key enters their process. None belongs in the offline workflow. The no-model check, `opencode_launcher_check.py`, never reads it.
- Orchestrator tests use `FakeLLM` (scripted GPT responses). The only test that calls the real OpenAI API is `tests/live/test_openai_live.py`. It is skipped unless `RUN_LIVE_OPENAI_TESTS=true` is set for that one command, after the user approves the spend.
- A run calls GPT only after `POST /runs/{id}/start` or an approval decision. Nothing resumes automatically after a restart.

## 7. OAuth and token handling

Applies to Google Drive now and to any later OAuth provider.

- **Scope.** Request the minimum scope. Google Drive uses `https://www.googleapis.com/auth/drive.readonly`. Each stored grant records its scopes, and every call checks the scope before it runs.
- **Flow.** Authorization code with PKCE (S256) and a random `state` bound to the browser session. The callback rejects a missing, reused or mismatched `state`. The redirect URI must match the registered value exactly.
- **Storage.** Only the refresh token is stored, encrypted with Fernet using `DEVPILOT_TOKEN_KEY`. Access tokens stay in memory and expire. The key comes only from the environment. It is never written to the database, logs or the repository.
- **Use.** Tokens are used only by the adapter that needs them. They never reach GPT, tool results, events, sandbox environment variables or the final report.
- **Revocation.** `DELETE /api/v1/integrations/google/credentials` revokes the token at Google and deletes the stored refresh token. The dashboard offers the same action.
- **Approval.** Each Drive import batch needs approval. The approval request shows file names and Drive IDs, never tokens.

**Implementation status (2026-11-03): offline only, never run against Google.**
- **Implemented in `app/drive/oauth.py` and `app/api/integrations.py`:**
  - the scope check;
  - PKCE;
  - a single-use `state` that expires after 10 minutes, bound to the project and to an HttpOnly browser cookie;
  - a strictly validated redirect URI;
  - the callback;
  - refresh under a per-grant lock;
  - deletion. It takes `?project_id=`, because each grant belongs to one project. It reports local deletion and Google's revocation separately.
- **Fails closed:** `cryptography` (Fernet) is not installed, so consent completes nowhere, and no other cipher stands in for it.
- **Storage:** encrypted refresh tokens are kept in memory; persistent storage is pending.
- **Approvals** show sanitized names, types, folder paths, the operation and the reason. See docs/MILESTONE_2_GOOGLE_DRIVE_RAG.md §14.

## 8. Document ingestion (PDF, DOCX, text)

- **Formats.** PDF, DOCX, Markdown and plain text. Other types are refused. The format is detected from the content, not only from the extension, and must match the extension.
- **Isolation.** Parsing runs only in the `document-parser` container, with `--network none`, the SandboxRunner flags of ARCH §7.2, a read-only input mount and a separate output mount. The host receives only normalized Markdown text and a manifest.
- **Limits.** `DOCUMENT_MAX_BYTES`, `DOCUMENT_MAX_PAGES` and `DOCUMENT_MAX_EXTRACTED_CHARS`, plus a DOCX limit on total uncompressed size and compression ratio. Limits are enforced inside the container and again on the host when the output is read.
- **Refusals.** Macro-enabled files (`.docm`), encrypted PDFs and files that fail format validation are refused.
- **Content handling.** PDF JavaScript and embedded files are ignored. Parsers do not fetch external references. Extracted text is untrusted data, and GPT receives it only as delimited data.
- **Provenance.** Each document records its SHA-256, MIME type and parser version.

**Implementation status (2026-11-03): interface only, not container-verified.**
- **Routing:** `app/rag/parsers.py` sends every real document to the `document-parser` container through the sandbox, and validates the answer on the host.
- **No in-process fallback:** a runtime that is not simulated refuses an in-process parser.
- **The image is not built:** it is a specification (`backend/sandbox_images/document-parser/`), never built or run, and not approved. So every real document is refused as `parser_unavailable`.
- **Output channel:** the parser answers on stdout, capped by `SANDBOX_MAX_OUTPUT_BYTES`, instead of a separate output mount.
- **Formats:** only TXT, Google Docs and DOCX are accepted; Markdown files are not yet. PDF waits for an approved parser.

## 9. Network access policy

- **Default deny.** Each component may reach only the destinations listed in ARCH §8.5 and `config/egress_allowlist.yaml`. The allowlist is validated at startup. A request to any other destination is refused and logged as a policy event.
- **No user-supplied URLs.** The only URLs fetched are validated GitHub repository URLs and Google Drive file IDs chosen by the user. Private and link-local addresses, cloud metadata addresses, and loopback addresses other than the n8n port are refused. Redirects are checked against the allowlist again.
- **Two enforcement layers.** Application code (`core/egress.py`) checks every outbound call. Docker network settings enforce the same rules for containers: `--network none` for run, test and parse, and for install an internal network whose only route is an allowlisting proxy.
- **Install step.** Package registries only (PyPI and the npm registry). No secrets in its environment. The proxy uses the same allowlist file.
- **Verification.** Phase 1 tests the application-layer check. Phase 3 tests that a container cannot reach an unlisted host, which is the "outbound connection fails" criterion in PROGRESS.md.

## 10. Repository import

- Only `https://github.com/<owner>/<repo>` URLs are accepted. SSH and other hosts are refused. Public repositories can be imported. Private repositories use the PAT, and each such import needs approval.
- The clone runs with hooks disabled, no submodules, LFS smudge off, the file protocol disabled, and the PAT supplied only through the credential helper.
- After the clone, the scan refuses symlinks, junctions, submodules and any file above the size limit. A repository larger than `GITHUB_IMPORT_MAX_MB` is refused.
- The commit SHA is recorded in `imported_sources`. Importing the same SHA again changes nothing.
- Repository contents, including READMEs, are untrusted data for GPT. They are never executed on the host.
- The residual risk of running `git` on the host is in §5.

## 11. Phase 3 sandbox: what is verified

The unit tests use a fake Docker CLI and real child processes, so they never start a container. The real-container tests (`tests/integration/test_sandbox_docker.py`, marker `docker`, opt-in with `RUN_DOCKER_TESTS=true`) ran on 2026-10-30 against Docker Desktop 4.97.0 (engine 29.9.2, Linux, WSL2 backend) with the image `python:3.12-alpine` (digest `sha256:e6dae589c1c05976188fd3bd019afd091445fc533ed2ea29001beddc7368a9c8`). All 9 passed.

| Control | Checked by | Status |
|---|---|---|
| Only approved images and commands (T1, T9) | `test_sandbox_policy.py`, `test_sandbox_docker_args.py` | Verified offline |
| No shell; argument lists only | `test_sandbox_process.py` (real child processes) | Verified offline |
| Every hardening flag is present; no privileged, host-network, socket, device or extra-volume options | `test_sandbox_docker_args.py`; `docker inspect` of a hardened container (2026-10-30) | Verified offline and on the real engine: NetworkMode none, Privileged false, read-only rootfs, CapDrop ALL, no-new-privileges, memory = swap, PID limit set |
| Workspace is the only mount, read-only, inside the approved folder; no symlink; no mount-breaking characters | `test_sandbox_docker_args.py` | Verified offline |
| Wall-clock timeout kills the process; cancel kills it; output flood kills it; excess output is dropped and marked | `test_sandbox_process.py`, `test_sandbox_runner.py` | Verified offline for the CLI process. Killing a real container is not yet verified. |
| Container removed on every path (success, failure, timeout, cancel, exception); leftovers swept at startup | `test_sandbox_runner.py`; real: timeout, cancel and OOM tests assert no leftover containers | Verified on the real engine (no `devpilot.sandbox` containers left after the run). The startup sweep is verified only against the fake CLI. |
| No network inside the container (T2) | `test_the_container_has_no_network`; control probe with `--network bridge` connects | **Verified in a real container** |
| Read-only workspace; non-root; no capabilities | `test_the_workspace_mount_is_read_only` (write fails with Errno 30), `test_the_process_is_non_root_with_no_capabilities` (uid 65534, CapEff all zero) | **Verified in real containers** |
| Memory limit enforced by the kernel | `test_the_memory_limit_is_enforced` (512 MB allocation under a 128 MB limit: OOM-killed) | **Verified in a real container** |
| Timeout and cancel against a real container | `test_the_time_limit_kills_and_removes_the_container`, `test_cancellation_removes_the_container` | **Verified in real containers** |
| Output limit and truncation against a real container | `test_output_over_the_limit_is_truncated` | **Verified in a real container** |
| Install step reaches only listed registries through a proxy (T13, §9) | none | **Not implemented.** Only `--network none` exists. |
| The container cannot reach an unlisted host (§9) | none | **Not verified** (depends on the install proxy) |

Residual risks specific to Phase 3:
- The Docker CLI runs on the host. Its own bugs are outside this sandbox.
- Images are pinned by version tag only. The digest is recorded above but is not yet enforced in the approval table ([ARCHITECTURE_OPTIMIZATION.md](ARCHITECTURE_OPTIMIZATION.md) item 9).
- Docker Desktop's VM shares its kernel with other Windows workloads (§5).

## 12. Phase 4 coding engine: status of each control (2026-11-01)

OpenCode has run only in the no-model check: real containers on internal networks, with a scripted model and no key. **No real model has been called through it, and no paid call is authorized.** The table shows what is tested at this checkpoint.

| Control | Where | Status |
|---|---|---|
| OpenCode's configuration cannot be overridden from the workspace: DevPilot ships managed config (catch-all deny; bash, web, subagents, skills and questions denied; LSP, formatters, plugins and MCP off; sharing off; OpenAI only) | `coding/opencode_policy.py` | Tested, and **verified on real containers:** the effective policy passes on the running server, and an `opencode.json` and `.opencode/` placed after start change nothing. |
| The *effective* policy is checked before each task by evaluating OpenCode's rules (last match wins, wildcards, per-agent overrides), not by reading single flags. Since the Phase 4C fix round the rules are taken from `GET /agent` (next row). | `verify_effective_config` | Tested, including bash re-allowed by a later rule, an agent override, a re-enabled subagent, plugins and MCP |
| A workspace that already contains `opencode.json`, `.opencode/`, `AGENTS.md`, `.git/`, `.env` or links is refused before OpenCode sees it | `coding/audit.preexisting_violations` | Tested |
| Python audits every change itself after a task: protected paths, new symlinks or junctions, invalid names, size and count limits, native executables, configured secrets | `coding/audit.audit` | Tested with real files, symlinks and junctions |
| Any violation, or any outcome other than COMPLETED, restores the workspace exactly (a task is kept or dropped as a whole) | `coding/audit.Checkpoint` | Tested, including removing a link without touching its target |
| The brief comes from the saved plan; GPT's instructions and plan text are fenced as untrusted data | `coding/brief.py` | Tested, including attempts to close the fence |
| Slow tools hold no database lock; a cancelled run or a backend shutdown stops them; late results never reach GPT | orchestrator loop | Tested |
| The adapter rejects every permission request and question, aborts on a forbidden tool, an edit outside the project, a timeout, a cancel or a token overrun, never calls the command endpoints, and checks the server's version and API | `tools/adapters/opencode.py` | Decisions tested through the `OpenCodeApi` Protocol. Reading and writing calls verified on the real server, through the filter, with a scripted model. It refuses every task without the API filter. |
| The OpenCode binary is the official npm package, unchanged: canonical registry URL, SHA-512 integrity (also pinned), archive entries checked before unpacking, and re-hashed against its manifest before every mount. It is never run on the host. | `coding/opencode_artifact.py`, `scripts/fetch_opencode.py` | **Verified** for 1.19.5 (2026-10-30). The registry's ECDSA signature is not checked, because no crypto library is installed. |
| The real-server contract check runs the server with no network and no published port, a read-only root, no capabilities, user 65534 and limits. `docker inspect` must confirm every setting, or the results are not trusted. The probe runs inside the container and never sends a prompt or a command. | `coding/opencode_check.py`, `scripts/opencode_contract_check.py`, `scripts/opencode_check/probe.py` | **Guarded baseline passes on the real 1.19.5 server** (2026-10-31): 22 PASS, 0 FAIL, 2 NOT VERIFIED.<br>- **Verified:** isolation, password required on every endpoint tested, the enforced build-agent rules, the declared configuration, the reviewed tool inventory, and the workspace untouched.<br>- **Not verified:** refusal of unknown paths (OpenCode answers them with its web interface, so this waits for the API filter), and the ripgrep download path (the check image has no `rg`).<br>- **Recorded:** OpenCode appends an `external_directory` allow for its own tool-output folder in the tmpfs. |
| The effective policy is judged from the rules OpenCode enforces (`GET /agent`, ordered), not from `GET /config`, which re-orders keys. Without `/agent` the check fails closed. Unreviewed permission names must end denied. | `opencode_policy.verify_effective_config`, `agent_rule_problems`, `config_problems` | Tested, including the real server's key order, later allows, agent overrides, unexpected permissions and agents, and malformed answers |
| Only the minimum tools may run: read, glob, grep, edit, write, apply_patch, todowrite and invalid. Each tool's permission was read from the pinned binary; the denied tools are hidden from the model. | `opencode_contract.ALLOWED_TOOLS`, `TOOL_PERMISSIONS` | Verified against the binary and the enforced rules (C12) |
| `apply_patch` checks edit rules only on each change's source path, so a move onto a protected file escapes them. DevPilot's audit of every changed path refuses it. | `coding/audit.audit` | Tested with real files (add, update, move onto `opencode.json`, move into `.opencode/`, delete) |
| A project `opencode.json` that names keys first can reorder the merged rules, so later project wildcards beat managed denials | `verify_effective_config`; `OPENCODE_DISABLE_PROJECT_CONFIG=1` | **Confirmed on the real 1.19.5 server** (precedence run, 2026-10-30): bash became allowed and offered, and a `.opencode/` agent with bash and protected-file edits was added. **DevPilot rejected it** in both views. **With the flag, no project file was loaded** and the result equals the baseline. The flag is mandatory in deployment. |
| OpenCode writes a `.gitignore` into, and runs an in-process npm install in, every writable config folder (global, and a workspace `.opencode/` when project config is on) | install guard: read-only config folders holding OpenCode's own `.gitignore` | Read from the binary. **No install in the guarded precedence runs** (C16, file listing). The earlier unguarded baselines likely attempted it into the RAM tmpfs with no network. |
| Only the build agent may be prompted, because an agent added by a project file can carry its own wider rules | `edge/filter_policy.check_body` | Built. Other agents and a missing `agent` were refused by the filter on the real server's path, and nothing reached OpenCode. No prompt was ever forwarded. |
| Only reviewed tools may run: read, glob, grep, edit, write, apply_patch and todowrite, plus `invalid`, which executes nothing. The server's list must equal the pinned 1.19.5 list, and any other release is unreviewed. | `coding/opencode_tools.py` | Tested for each tool and class of file operation. The list and rules were checked on the real server. |
| A move's destination is judged by DevPilot, because OpenCode asks `edit` only for the source. Every path in a patch is read from the patch itself and from what it reports. The audit of real files stays the authority, and neither replaces isolation. | `tool_problems`, `coding/audit.audit` | Tested, including several-file patches and moves onto protected paths. |
| A prompt carries no `tools` field: on 1.19.5 it replaces the session's whole rule list. Sessions are compared with the exact list DevPilot sent. | adapter, `parse_session`, `edge/filter_policy.check_body` | Read from the binary; the check is tested. The API filter refuses `tools` and any other unlisted field. |
| The adapter refuses any server that is not behind the API filter, and any answer that is not the expected status, type and shape, including HTTP 200 pages | adapter, `opencode_contract.json_answer` | Tested, and shown on the real server: a bare server is refused. This shows a filter answers, not that it carries all traffic. The per-run token now also has to be accepted. |
| Any permission request, question, terminal or MCP event, or unreadable event stops the attempt through the safe path | adapter `_Tracker` | Tested. Real events are not observed yet. |
| The API filter forwards an explicit allowlist of methods, paths, one directory query and bodies. Unknown operations get 403 and are never forwarded (C13). | `edge/filter_policy.py`, `edge/filter_server.py` | Built. **One isolated run against the real 1.19.5 server: 42 PASS, 0 FAIL.** All 175 other operations in the server's spec, 8 unknown paths, 9 unknown methods and 19 encoded or normalized paths were refused, and a request witness in front of OpenCode saw none of them. |
| Every filter request needs a per-run token of at least 32 characters, compared in constant time over SHA-256 digests. The token gives nothing on OpenCode, and the OpenCode password stays in the filter. | `edge/filter_policy.authenticated`, `HttpUpstream` | Tested. On the real server the filter's token got 401 directly on OpenCode. The token reaches the filter through the environment, by name. |
| Ambiguous requests are refused before routing: conflicting or malformed lengths, any transfer encoding, folded headers, duplicate `Host`, a body on a GET, or bytes after the body. Nothing is forwarded as received: target, headers and body are rebuilt on a new connection, and each connection carries one request. | `edge/filter_policy.check_framing`, `edge/filter_server._forward` | Tested, and 13 ambiguous requests refused in the real run. The check ran over the relay, so real TCP timeouts were not exercised. |
| No token, password, header value, query or body reaches the filter's log, and the client sees only four fixed response headers | `audit_target`, `_Audit`, `_send` | Tested. The saved audit, witness log and report hold no credential. |
| Only an API filter can reach OpenCode | network layout, launcher | **Verified on real containers, no model.** The host cannot reach OpenCode. The filter's and gateway's tokens get 401 on it. The filter and gateway cannot reach each other. |
| A container counts as stopped only when a separate read of its full ID says so. `docker rm` exits 0 for a missing container, so its exit code proves nothing. A daemon failure, a timeout, an unreadable answer, a permission error or a different identity is never "gone". | `DockerCli.inspect_container`, `coding/container_stop.ContainerTerminator` | Built. Tested against Docker's real error text (29.9.2), and on one real container: running, wrong identity, stopped and gone. Not yet used by a launcher. |
| The reason an attempt failed is kept, with the stop's evidence stored beside it, redacted. | `AttemptRecord.stop_evidence`, `coding/attempt_store.py` | Tested. |
| OpenCode reaches its model only through a DevPilot gateway that holds the key. Each run has its own gateway token. Only the approved model and function tools pass, and requests, tokens and dollars are limited before forwarding. | `edge/gateway_policy.py`, `edge/gateway_server.py` | **Verified on real containers with a scripted upstream.** The production entry point also started with a placeholder key and no route: it refused bad tokens and failed closed. **No real API call has gone through it, and the TLS path has never reached OpenAI.** |
| OpenCode's container sits only on two internal networks, publishes no port, and mounts only a disposable copy. The launcher confirms this from `docker inspect` and `docker network inspect` before any prompt. | `coding/launcher.py`, `opencode_container.deployment_problems`, `network_problems` | **Verified on real containers.** It refuses to start without an approved image and `CODING_LIVE_APPROVED`. **The published loopback port and the ingress and egress networks have never run:** they need a network with a route out. |
| The real project is written once, after verification, with exactly the files that were tested. A project changed during the run is never overwritten, and a failed delivery restores it. | `coding/candidate.py` | Tested with real files, and through real Docker runs. |
| A pass needs exit code 0, a readable summary saying OK, at least one test, and a passing test named after every MUST criterion. An empty suite, truncated output or a sandbox that gave no answer is never a pass. | `verification/results.py` | Tested on real unittest output and real Docker runs. The tests come from the same engine as the code. |
| Repairs and coding attempts are limited in code, not by GPT. A policy breach, a refused credential or a quarantine is never retried. | `recovery/engine.py` | Tested, and shown with real Docker runs. |
| The production container has one definition with no switch to weaken it: `OPENCODE_DISABLE_PROJECT_CONFIG=1` always, no network, non-root, no capabilities, no-new-privileges, read-only root, limits, the verified pinned binary, and an exact allowlist of environment names | `coding/opencode_container.py` | Tested, including that no caller can pass an override and that no application module imports the precedence experiment. **Verified running:** the approved runtime image, with `deployment_problems` empty against the real container. |
| OpenCode never sees a real workspace: only a private copy is writable, and real workspaces, the repository, the home folder and the checkpoints are refused as mount sources | `production_problems`, `coding/candidate.py` | Tested, and verified on real containers: a sentinel in the original project is not visible anywhere in the container. |
| Provider credentials never reach OpenCode: the managed file may hold only the gateway placeholder, no `{env:}` or `{file:}` reference, and no configured secret | `managed_config_problems`, `environment_problems` | Tested |
| Each installation path is recorded as attempt prevented or artifact prevented, with how that is known | `INSTALL_PATHS` | Read from the binary. **ripgrep closed for the approved image:** `rg` 15.1.0 is on `PATH`, grep and glob ran with it, and no download appeared in OpenCode's log or folder. An image without `rg` is refused by the builder. |
| Provider packages stay bundled: any `npm` override or `skills.urls` is refused, and C17 reads each model's package from the server | `install_source_problems`, C17 | All 83 models use the bundled `@ai-sdk/openai`. No model was called, so the load itself is not exercised. |
| A prompt is sent at most once, and an uncertain delivery is never resent. Only a 204 counts as accepted. | `coding/session_control.py` | Tested with fakes, including delayed acceptance and a crash at every save |
| The workspace is restored only after the container is confirmed gone and the workspace is still. An acknowledged abort or a quiet view never permits it. Otherwise the attempt is quarantined, which nothing undoes automatically. | `AttemptGuard.secure`, `restore` | Tested, including 1500 simulated interleavings. **Verified on real containers** for a timeout, a person's cancel, recovery at startup with a real writer running, and a stop that cannot be confirmed: the writer was really still running, nothing was restored, and the copy was kept. |
| One set of containers per attempt, only a private copy mounted, no published OpenCode port, fresh secrets per start | `coding/launcher.py` | Verified on real containers. |
| When the process that started the containers dies, a separate process stops them: the writer first, confirmed by a read of its full ID, then the edges, then the networks. It holds no credential, sits outside the process tree and the console, and enforces its own deadline. An unconfirmed stop is never clean: everything stays and a marker blocks the next run until a confirmed stop resolves it. | `coding/guard_process.py` | **Verified on real containers:** Ctrl-C under PowerShell, a hard kill, a wedged process, and a Docker that cannot be reached. Needed because PowerShell terminates a child part-way through its `finally` (measured). |
| A gateway request is on the ledger, charged in full, before it leaves. If the gateway dies mid-request, that line stands. | `edge/gateway_server._forward`, `coding/launcher.ledger_rows` | Tested, and verified on real containers: the request in flight when the gateway was removed was charged its whole reservation. |
| GPT calls and gateway requests draw on one budget, and each reserves its maximum before it is sent. A call that could pass the limit is refused, however little it may really cost. | `orchestrator/budget.py`, `edge/gateway_policy.Budget` | Tested and mutation-checked. Real containers show the run's cost is GPT's plus the gateway's. |
| The configured prices hold: every call is pinned to Standard processing, because `auto` follows the project's setting, which may be the dearer Fast tier, and nothing that could reach long-context prices (over 272,000 input tokens, $4 and $15) is sent. A token covers at least a byte, so the request's size is the proof. | `llm_client`, `orchestrator/budget.py`, `edge/gateway_policy.check_request` | Tested. |
| Overload fails closed. With every slot taken, or no thread available, a connection gets 503, nothing of it is read, and the refusal is in the audit log. | `edge/filter_server.build_server` | **Verified on real containers,** with OpenCode's own log as a witness: a valid `POST /session` refused under overload created no session, and the same request afterwards did. The three 503s first seen in a sequential run were not reproduced, and their cause is unknown. |
