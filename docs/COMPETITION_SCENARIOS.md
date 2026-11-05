# Competition scenarios: what is ready, what has really run, and what each still needs

The step-by-step plan for running them live is [LIVE_TEST_PLAN.md](LIVE_TEST_PLAN.md). Feature by feature status: [FEATURE_STATUS.md](FEATURE_STATUS.md).

Status as of 2026-11-05, branch `feature/general-purpose-platform`.

**None of the eight scenarios below has been run live in this branch.** Each needs at least one of the following, and none of them has been given:
- a paid OpenAI run you approve;
- an account you connect (GitHub OAuth App, Google OAuth client);
- a container you approve pulling (n8n, the FastAPI and React verifiers).

Everything listed as "built" is tested offline with scripted GPT and in-process fakes. "Verified" means a real service or model ran.

## How to run any scenario

1. Start the live server, then type `serve live` when it asks (it spends only after that, within the session limit):

   ```powershell
   cd D:\DOWNLOAND_NEW_D\AI\AI\backend
   .\.venv\Scripts\python.exe scripts\live_server.py --approve-session 1.00
   ```

2. Open the one-time sign-in link it prints. There is no token to paste.
3. Type the request on the home page, approve the run's limit, and approve anything the run asks for in the popups.
4. Afterwards, record the cost from the run's report and from OpenAI's usage page.

## The scenarios

| | Scenario | Built (offline) | Verified live | Still needed |
|---|---|---|---|---|
| A | Homepage from a prompt | GPT picks `static-html`; frozen browser checks; Chromium verification in Docker; ZIP | Scripted pipeline on real Docker and Chromium only (2026-11-04) | `devpilot setup --web` self-test on this computer; one approved paid run |
| B | Weather forecast app | `python-cli` path is verifiable; the provider is replaceable and faked in tests | No | One approved paid run. As a React + FastAPI app it also needs both verifier images built and approved |
| C | Simple playable game | As a static site (HTML, CSS, JS) under A's checks | No | As A |
| D | Search GitHub, compare with a requirement | Search, licence-aware evaluation, automatic inspection of named repositories, code-checked gap analysis given to GPT | No live GitHub request yet | `REPOSITORY_SOURCE=github`; a GitHub connection for private repositories; one approved paid run |
| E | Modify a repository, open an approved PR | Import at a pinned commit, gap analysis, verification, `publish_repository_changes` (GIT_PUSH approval, new branch, only the run's changes) | No | A GitHub OAuth App (`GITHUB_OAUTH_CLIENT_ID`, `GITHUB_OAUTH_CLIENT_SECRET`), `DEVPILOT_TOKEN_KEY`, a connection from the Tools page, a test repository, one approved paid run |
| F | Google Drive RAG application | Consent with PKCE and encrypted refresh tokens, scoped grants, OpenAI embeddings with a dollar bound, persistent SQLite index isolated per project and grant, GPT-written answers whose every citation is checked in code (`POST /projects/{id}/knowledge/ask`) | No | A Google OAuth client; the `document-parser` image built and approved; `RAG_EMBEDDER=openai` with `OPENAI_EMBEDDING_PRICE_PER_MTOK`; FastAPI and React verifiers for the generated app |
| G | Compare OpenCode, n8n and another tool | Tools page and `compare_tools` answer from the catalog with recorded evidence; unknown tools (Cloudflare, Zapier) are reported as unknown | Answers are local and free; the evidence they cite is as recorded | Nothing to compare *live*; n8n's evidence stays "offline tests" until the live n8n test passes |
| H | Download the ZIP and run it in a clean environment | ZIP only for verified runs, with the same packaging rules as `devpilot build`; manifest and report included | No | A delivered run, then unzipping it on a clean machine and running its tests by hand |

## What must not be claimed

- That OpenCode has built a project end to end with a real model. Its one live build, on 2026-11-03, failed (docs/FIRST_REAL_BUILD.md).
- That n8n, GitHub OAuth, Google OAuth or real embeddings have run against the real services. They are tested against fakes.
- That OpenCode runs through n8n. It is planned, not built.
- That React, FastAPI or React + FastAPI projects can be built here. Their verifier images are defined, not built or approved.
