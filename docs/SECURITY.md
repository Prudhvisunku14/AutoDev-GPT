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
| T9 | Unapproved external or destructive actions | The PolicyEngine assigns a risk level to every action. GIT_PUSH, GITHUB_\*, DESTRUCTIVE actions and anything using real credentials **always** go to the human approval queue. EXTERNAL_NETWORK (n8n) needs approval unless `N8N_AUTO_APPROVE=true`. | 2, 8 |
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
| EXTERNAL_NETWORK | `trigger_workflow`, `import_github_repo`, `import_google_drive_file` | Google Drive imports always need approval. Private-repo imports need approval because they use the PAT. `trigger_workflow` needs approval unless `N8N_AUTO_APPROVE=true`. Public GitHub clones on the allowlist need no approval. |
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
- **The OpenCode container holds the OpenAI key and has egress to reach OpenAI.** bash and webfetch are denied there.
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

## 8. Document ingestion (PDF, DOCX, text)

- **Formats.** PDF, DOCX, Markdown and plain text. Other types are refused. The format is detected from the content, not only from the extension, and must match the extension.
- **Isolation.** Parsing runs only in the `document-parser` container, with `--network none`, the SandboxRunner flags of ARCH §7.2, a read-only input mount and a separate output mount. The host receives only normalized Markdown text and a manifest.
- **Limits.** `DOCUMENT_MAX_BYTES`, `DOCUMENT_MAX_PAGES` and `DOCUMENT_MAX_EXTRACTED_CHARS`, plus a DOCX limit on total uncompressed size and compression ratio. Limits are enforced inside the container and again on the host when the output is read.
- **Refusals.** Macro-enabled files (`.docm`), encrypted PDFs and files that fail format validation are refused.
- **Content handling.** PDF JavaScript and embedded files are ignored. Parsers do not fetch external references. Extracted text is untrusted data, and GPT receives it only as delimited data.
- **Provenance.** Each document records its SHA-256, MIME type and parser version.

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

## 12. Phase 4 coding engine: status of each control (2026-10-31)

OpenCode is **not running anywhere** and is not called by any run. The table shows what is tested at this checkpoint.

| Control | Where | Status |
|---|---|---|
| OpenCode's configuration cannot be overridden from the workspace: DevPilot ships managed config (catch-all deny; bash, web, subagents, skills and questions denied; LSP, formatters, plugins and MCP off; sharing off; OpenAI only) | `coding/opencode_policy.py` | Config generator tested. **Not deployed** (no container). |
| The *effective* policy is checked before each task by evaluating OpenCode's rules (last match wins, wildcards, per-agent overrides), not by reading single flags. Since the Phase 4C fix round the rules are taken from `GET /agent` (next row). | `verify_effective_config` | Tested, including bash re-allowed by a later rule, an agent override, a re-enabled subagent, plugins and MCP |
| A workspace that already contains `opencode.json`, `.opencode/`, `AGENTS.md`, `.git/`, `.env` or links is refused before OpenCode sees it | `coding/audit.preexisting_violations` | Tested |
| Python audits every change itself after a task: protected paths, new symlinks or junctions, invalid names, size and count limits, native executables, configured secrets | `coding/audit.audit` | Tested with real files, symlinks and junctions |
| Any violation, or any outcome other than COMPLETED, restores the workspace exactly (a task is kept or dropped as a whole) | `coding/audit.Checkpoint` | Tested, including removing a link without touching its target |
| The brief comes from the saved plan; GPT's instructions and plan text are fenced as untrusted data | `coding/brief.py` | Tested, including attempts to close the fence |
| Slow tools hold no database lock; a cancelled run or a backend shutdown stops them; late results never reach GPT | orchestrator loop | Tested |
| The adapter rejects every permission request and question, aborts on a forbidden tool, an edit outside the project, a timeout, a cancel or a token overrun, never calls the command endpoints, and checks the server's version and API | `tools/adapters/opencode.py` | Decisions tested through the `OpenCodeApi` Protocol; reading calls verified on the real server. **Writing calls have never run on a real server.** It refuses every task without the API filter. |
| The OpenCode binary is the official npm package, unchanged: canonical registry URL, SHA-512 integrity (also pinned), archive entries checked before unpacking, and re-hashed against its manifest before every mount. It is never run on the host. | `coding/opencode_artifact.py`, `scripts/fetch_opencode.py` | **Verified** for 1.19.5 (2026-10-30). The registry's ECDSA signature is not checked, because no crypto library is installed. |
| The real-server contract check runs the server with no network and no published port, a read-only root, no capabilities, user 65534 and limits. `docker inspect` must confirm every setting, or the results are not trusted. The probe runs inside the container and never sends a prompt or a command. | `coding/opencode_check.py`, `scripts/opencode_contract_check.py`, `scripts/opencode_check/probe.py` | **Guarded baseline passes on the real 1.19.5 server** (2026-10-31): 22 PASS, 0 FAIL, 2 NOT VERIFIED.<br>- **Verified:** isolation, password required on every endpoint tested, the enforced build-agent rules, the declared configuration, the reviewed tool inventory, and the workspace untouched.<br>- **Not verified:** refusal of unknown paths (OpenCode answers them with its web interface, so this waits for the API filter), and the ripgrep download path (the check image has no `rg`).<br>- **Recorded:** OpenCode appends an `external_directory` allow for its own tool-output folder in the tmpfs. |
| The effective policy is judged from the rules OpenCode enforces (`GET /agent`, ordered), not from `GET /config`, which re-orders keys. Without `/agent` the check fails closed. Unreviewed permission names must end denied. | `opencode_policy.verify_effective_config`, `agent_rule_problems`, `config_problems` | Tested, including the real server's key order, later allows, agent overrides, unexpected permissions and agents, and malformed answers |
| Only the minimum tools may run: read, glob, grep, edit, write, apply_patch, todowrite and invalid. Each tool's permission was read from the pinned binary; the denied tools are hidden from the model. | `opencode_contract.ALLOWED_TOOLS`, `TOOL_PERMISSIONS` | Verified against the binary and the enforced rules (C12) |
| `apply_patch` checks edit rules only on each change's source path, so a move onto a protected file escapes them. DevPilot's audit of every changed path refuses it. | `coding/audit.audit` | Tested with real files (add, update, move onto `opencode.json`, move into `.opencode/`, delete) |
| A project `opencode.json` that names keys first can reorder the merged rules, so later project wildcards beat managed denials | `verify_effective_config`; `OPENCODE_DISABLE_PROJECT_CONFIG=1` | **Confirmed on the real 1.19.5 server** (precedence run, 2026-10-30): bash became allowed and offered, and a `.opencode/` agent with bash and protected-file edits was added. **DevPilot rejected it** in both views. **With the flag, no project file was loaded** and the result equals the baseline. The flag is mandatory in deployment. |
| OpenCode writes a `.gitignore` into, and runs an in-process npm install in, every writable config folder (global, and a workspace `.opencode/` when project config is on) | install guard: read-only config folders holding OpenCode's own `.gitignore` | Read from the binary. **No install in the guarded precedence runs** (C16, file listing). The earlier unguarded baselines likely attempted it into the RAM tmpfs with no network. |
| Only the build agent may be prompted, because an agent added by a project file can carry its own wider rules | API filter (design) | **Not built.** Required: `prompt_async` must carry `agent: "build"`. |
| Only reviewed tools may run: read, glob, grep, edit, write, apply_patch and todowrite, plus `invalid`, which executes nothing. The server's list must equal the pinned 1.19.5 list, and any other release is unreviewed. | `coding/opencode_tools.py` | Tested for each tool and class of file operation. The list and rules were checked on the real server. |
| A move's destination is judged by DevPilot, because OpenCode asks `edit` only for the source. Every path in a patch is read from the patch itself and from what it reports. The audit of real files stays the authority, and neither replaces isolation. | `tool_problems`, `coding/audit.audit` | Tested, including several-file patches and moves onto protected paths. |
| A prompt carries no `tools` field: on 1.19.5 it replaces the session's whole rule list. Sessions are compared with the exact list DevPilot sent. | adapter, `parse_session` | Read from the binary; the check is tested. The API filter must refuse `tools`. |
| The adapter refuses any server that is not behind the API filter, and any answer that is not the expected status, type and shape, including HTTP 200 pages | adapter, `opencode_contract.json_answer` | Tested, and shown on the real server: a bare server is refused. This shows a filter answers, not that it carries all traffic. |
| Any permission request, question, terminal or MCP event, or unreadable event stops the attempt through the safe path | adapter `_Tracker` | Tested. Real events are not observed yet. |
| Only an API filter can reach OpenCode, and it forwards an explicit allowlist of methods, paths, query keys and bodies | design only | **Not built** (ARCHITECTURE_OPTIMIZATION.md, Phase 4C) |
| OpenCode reaches its model only through a DevPilot gateway that holds the key; the container has no other network | design only | **Not built** ([ARCHITECTURE_OPTIMIZATION.md](ARCHITECTURE_OPTIMIZATION.md) item 3) |
| The production container has one definition with no switch to weaken it: `OPENCODE_DISABLE_PROJECT_CONFIG=1` always, no network, non-root, no capabilities, no-new-privileges, read-only root, limits, the verified pinned binary, and an exact allowlist of environment names | `coding/opencode_container.py` | Tested, including that no caller can pass an override and that no application module imports the precedence experiment. Its flags and environment match the guarded baseline. **No runtime image is approved, so it cannot start yet.** |
| OpenCode never sees a real workspace: only a private copy is writable, and real workspaces, the repository, the home folder and the checkpoints are refused as mount sources | `production_problems` | Tested. The step that makes the copy arrives with the launcher. |
| Provider credentials never reach OpenCode: the managed file may hold only the gateway placeholder, no `{env:}` or `{file:}` reference, and no configured secret | `managed_config_problems`, `environment_problems` | Tested |
| Each installation path is recorded as attempt prevented or artifact prevented, with how that is known | `INSTALL_PATHS` | Read from the binary. **Open: ripgrep.** Without `rg`, OpenCode tries to download it from github.com and only the missing network stops that. The production builder refuses images without `rg`. |
| Provider packages stay bundled: any `npm` override or `skills.urls` is refused, and C17 reads each model's package from the server | `install_source_problems`, C17 | All 83 models use the bundled `@ai-sdk/openai`. No model was called, so the load itself is not exercised. |
| A prompt is sent at most once, and an uncertain delivery is never resent. Only a 204 counts as accepted. | `coding/session_control.py` | Tested with fakes, including delayed acceptance and a crash at every save |
| The workspace is restored only after the container is confirmed gone and the workspace is still. An acknowledged abort or a quiet view never permits it. Otherwise the attempt is quarantined, which nothing undoes automatically. | `AttemptGuard.secure`, `restore` | Tested, including 1500 simulated interleavings and 31 deliberate breaks, all caught. **The guarantee rests on the stop telling the truth;** Stage 9 must confirm independently. |
| One OpenCode container per project, only a private copy mounted, no published port, fresh password per start | design only | **Not built** (item 4) |
