# DevPilot AI: Security Model

> Status: **design approved in Phase 0 (2026-10-29) and revised the same day** for Google Drive OAuth, repository and document import, the network policy and test validation. These controls are not implemented yet. Each control is built and tested in the phase listed in [PROGRESS.md](PROGRESS.md).

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
