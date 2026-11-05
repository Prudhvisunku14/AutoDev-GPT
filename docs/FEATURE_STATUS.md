# Feature status: historical snapshot

This matrix is a historical snapshot for `feature/general-purpose-platform` dated 2026-11-05; it is not a current status report for `feature/evaluation-upgrade`. Use the root [README](../README.md), current source, and current test results for the checked-out branch. Each feature below appears once, under the strongest evidence recorded in that snapshot:

1. **Tested live:** the real service, model or container ran, and the result is recorded.
2. **Tested offline:** built, and covered by automated tests with fakes (scripted GPT, in-process GitHub, OpenAI, n8n and Drive). It has never met the real service.
3. **Implemented, blocked:** built, but it cannot work on this computer until a missing runtime, account or approval exists.
4. **Not built:** a requirement with no implementation yet.

CI on GitHub (first run, 2026-11-05, run `41976410351`): backend on Windows and frontend on Ubuntu, both passed.

## 1. Tested live

| Feature | Evidence |
|---|---|
| GPT orchestration: a real model analyses and plans through strict function calls and the code gates | Live analysis accepted on 2026-10-29. Planning CLI on 2026-11-03: 32 requirements and 9 tasks accepted, about $0.039 (docs/PROGRESS.md) |
| Docker sandbox isolation: no network, timeout kill, memory limit | Real containers, 2026-10-30 |
| OpenCode on real containers behind DevPilot's filter and gateway | Real containers with a **scripted** model, 2026-11-01 |
| Website verification: frozen browser checks in Chromium, repair, delivery | Real Docker and Chromium with **scripted** GPT and coding, 2026-11-04 |
| GitHub CI | Both jobs passed on this branch |

**Tested live and failed:** the one real OpenCode build with a real model, on 2026-11-03. Task 1 completed, task 2 failed on DevPilot's own gateway budget, and nothing was delivered. That cause was fixed offline; the build has not been re-run.

## 2. Tested offline only

| Feature | Where | Tests |
|---|---|---|
| Tool catalog, comparisons and recommendations; `compare_tools` for GPT; Tools page | `app/toolcatalog/` | `test_tool_catalog.py` |
| Discovery of GitHub and Drive links in requests; automatic inspection of named public repositories | `app/sources/` | `test_github_connection.py` |
| GitHub OAuth connection: state, PKCE, browser binding, Fernet-encrypted token, revocation | `app/repos/github_oauth.py` | same |
| GitHub writes: new repository, or a pull request on a new branch with only the run's changes, after GIT_PUSH approval | `app/repos/github_write.py`, `publish_repository_changes` | same, including a full run and its approval |
| Permission popup before a build, with automatic continuation after access is granted | `PermissionDialog.tsx` | Vitest |
| Gap analysis of existing code, checked against the real file list, and given to OpenCode | `submit_gap_analysis` | `test_gap_analysis.py` |
| GPT chooses the stack; a build with no verifiable stack is refused before spending | `api/runs.py`, Composer | `test_stack_choice.py`, Vitest |
| n8n: signed triggers and callbacks, health check, `run-events` workflow; **a waiting run is resumed by its callback** | `app/n8n/`, `wait_for_workflow`, `api/n8n.py` | `test_n8n.py`, `test_n8n_resume.py` |
| Real embeddings (OpenAI, dollar-bounded); persistent SQLite index isolated per project and grant | `app/rag/openai_embedder.py`, `sqlite_index.py` | `test_rag_production.py` |
| **GPT-written RAG answers**, every citation checked (retrieved source, verbatim quote, every marker cited) | `app/rag/grounded.py`, `POST /projects/{id}/knowledge/ask` | `test_rag_grounded.py` |
| Dashboard sessions: HttpOnly cookie, CSRF header, one-time sign-in link | `app/core/sessions.py` | `test_web_sessions.py`, Vitest |
| Run timeline for every new event | `lib/timeline.ts` | Vitest |
| Google OAuth consent with PKCE; read-only grants; revocation; **consent and encrypted refresh tokens persist across restarts** | `app/drive/`, `drive/persistent.py` | `test_drive_oauth.py`, `test_drive_tools.py`, `test_drive_persistence.py` |
| Knowledge bases restored from the persistent index after a restart; revocation deletes the grant's chunks from disk even then | `DriveRuntime.base_for`, `forget` | `test_drive_persistence.py` |
| Evaluation upgrade: preview → typed confirmation → **draft** pull request of a delivered, verified run (one new `devpilot/*` branch, one commit) | `app/evaluation/github_publish.py`, `api/evaluation.py`, `GitHubPublishCard.tsx` | `test_evaluation_github_publish.py`, Vitest |
| Evaluation upgrade: conservative source-to-requirement triage for GPT (`find_source_requirement_gaps`, never proof) and exact document-version diff | `app/evaluation/gaps.py`, `agent_tools.py` | `test_evaluation_gaps.py` |

## 3. Implemented, blocked here

| Feature | What blocks it | Who can unblock it |
|---|---|---|
| React + Vite, FastAPI, and React + FastAPI builds | `devpilot/verify-react:1` and `devpilot/verify-fastapi:1` are defined but not built (they pull base images and packages) | You approve the image builds; then their self-tests run |
| Static websites and browser games | `devpilot/verify-web:1` is built, but `devpilot setup --web` has not self-tested and approved it on this computer | You run `devpilot setup --web` (no download; it uses the local image) |
| Reading real PDF and DOCX documents | The `document-parser` image is not built | You approve building it |
| Private repositories, pull requests and new repositories | No GitHub OAuth App is configured | You register one and set its ID, its secret and `DEVPILOT_TOKEN_KEY` |
| Google Drive | No Google OAuth client is configured | You create one in Google Cloud |
| n8n | No n8n instance is running | You approve pulling `n8nio/n8n` (about 600 MB) and run `scripts/n8n-local.ps1` |
| Real embeddings | `RAG_EMBEDDER=openai` and `OPENAI_EMBEDDING_PRICE_PER_MTOK` are not set | You set them and approve the spend |
| Any build with the real model | Paid; no session is approved | You start `scripts/live_server.py --approve-session <limit>` |

## 4. Not built: requirements still missing

Found by reviewing the eight phases against the original request, most important first:

1. ~~Persistent Google credentials~~ and 2. ~~knowledge bases after a restart~~: **done** (2026-11-05, `drive/persistent.py`, `DriveRuntime.base_for`).
3. **No permission popup inside a run.** The popup works before a build starts. A run that hits a private repository in `import_repository`, or that needs Google consent mid-run, pauses and shows it in the timeline, but offers no popup button, and does not continue by itself after access is granted.
4. **Drive specifications are not part of the gap analysis.** The gap analysis compares *code* with requirements. Documents the request links to are indexed for RAG, but their content is not summarised into GPT's analysis.
5. **No dashboard panel for asking a knowledge base.** The grounded-answer API exists, with no UI yet.
6. **No automatic event forwarding.** n8n receives run events only when GPT calls `trigger_workflow` with `run-events`. Code does not forward them by itself.
7. **OpenCode through n8n.** Planned only. It is listed as planned, never as working.
8. **No GitHub App.** The OAuth App flow is built; a GitHub App with per-repository installation tokens is not.
9. **Audit items still open:** A12 (pagination), A16 (a durable queue that resumes runs after a restart).
10. **No systematic check that every visible button works.** The Vitest suites cover the main flows; no end-to-end browser test (Playwright) of the dashboard exists.

The live test plan for scenarios A–H is in [LIVE_TEST_PLAN.md](LIVE_TEST_PLAN.md).
