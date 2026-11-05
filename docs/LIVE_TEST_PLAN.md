# Live test plan: scenarios A–H

**Nothing here has been run. Every paid step and every download needs your approval at that step.**

The plan goes from free to paid, and from local to external. **Stop rule:** after any unexpected result, stop, record it, and diagnose offline before the next paid step.

Current feature status: [FEATURE_STATUS.md](FEATURE_STATUS.md).

## Stage 0: preparation (free; nothing is contacted)

1. **Check out the branch.** Everything below runs from `D:\DOWNLOAND_NEW_D\AI\AI`, branch `feature/general-purpose-platform`.
2. **Ports.** The old copy in `C:\Users\prudh\AutoDev-GPT` still runs on 8000 and 5173. Either stop it, or use 8001 and 5174 as below.
   - In `.env`: `DEVPILOT_PORT=8001` and `DASHBOARD_URL=http://localhost:5174`.
   - Every OAuth callback below must then use port 8001.
3. **`.env` settings.** `OPENAI_PRICE_INPUT_PER_MTOK` and `OPENAI_PRICE_OUTPUT_PER_MTOK` from OpenAI's pricing page, `OPENAI_MAX_OUTPUT_TOKENS=4000`, `OPENAI_MAX_RETRIES=0`, `REQUIRE_SPEND_APPROVAL=true`.
4. **Website verifier.** `.\devpilot.cmd setup --web` self-tests the local `devpilot/verify-web:1` image. There is no download. It unlocks scenarios A and C.
5. **Preflight.** `cd backend; .\.venv\Scripts\python.exe scripts\live_server.py` reads `.env`, sends no key, and lists anything missing.
6. **Dashboard.** `cd frontend; npm run dev -- --port 5174 --strictPort`.

**Record for every scenario:** date, commit, run ID, final state, DevPilot's cost and OpenAI's usage page figure, turns, repairs, every verification verdict, the delivered files, and any links.

## Stage 1: free

### G. Compare OpenCode, n8n and another tool

1. Open the Tools page (no spend).
2. Ask: "Compare OpenCode, n8n and Cloudflare", then "Which tool is best for a website?".
3. **Pass if:**
   - OpenCode and n8n are shown with their real readiness and their strongest *recorded* evidence;
   - Cloudflare is reported as not a DevPilot tool, with nothing invented;
   - nothing claims live evidence that does not exist.

## Stage 2: paid builds with no external accounts

Start the live server with a session limit you approve, then type `serve live` when it asks:

```powershell
cd D:\DOWNLOAND_NEW_D\AI\AI\backend
.\.venv\Scripts\python.exe scripts\live_server.py --approve-session 1.00
```

Open the one-time sign-in link it prints. The server enforces $0.50 per run and the session limit across runs.

### A. Homepage

1. **Prompt:** "Create a homepage for a bakery with opening hours, a menu section and a contact form that validates the email address."
2. **Pass if:**
   - GPT chooses `static-html`;
   - the browser checks are frozen before coding;
   - Chromium verification passes, or a failure is repaired and then passes;
   - the run is COMPLETED, and "Download ZIP" is offered.
3. **Fail is also a result:** record the state, the failing check and the cost.

### H. ZIP in a clean environment (with A's ZIP, then B's)

1. Download the ZIP and unzip it into a new empty folder.
2. A static site: serve it with `python -m http.server 8090` from that folder, open `http://127.0.0.1:8090`, and repeat the acceptance criteria by hand.
3. A Python program: `py -3.12 -m venv .v; .\.v\Scripts\python -m unittest -v`.
4. **Pass if:** it runs and its tests pass with nothing but the ZIP's contents.

### C. Simple playable game

1. **Prompt:** "Create a browser game where the player catches falling stars with the arrow keys, with a score, a timer and a restart button."
2. **Pass if:** it behaves as A does, and the frozen checks cover the score, the restart and keyboard input.

### B. Weather forecast application

1. **Prompt:** "Create a Python weather forecast application that reports temperature, humidity and wind for a city, with a replaceable data provider, clear errors for unknown cities, and unit tests that use a fake provider."
2. **Pass if:** GPT chooses `python-cli`, the tests pass in the sandbox, the run is delivered, and H passes on its ZIP.
3. A React + FastAPI weather app is not possible until the two verifier images are built and approved. GPT should plan it and stop before coding, with that reason.

## Stage 3: GitHub (needs a GitHub OAuth App)

1. **Register the app.** GitHub → Settings → Developer settings → OAuth Apps → New:
   - homepage `http://localhost:5174`;
   - callback `http://127.0.0.1:8001/oauth/github/callback`.
2. **Configure `.env`:**
   - `GITHUB_OAUTH_CLIENT_ID`, `GITHUB_OAUTH_CLIENT_SECRET`, and `GITHUB_OAUTH_REDIRECT_URI` set to that callback;
   - `REPOSITORY_SOURCE=github`;
   - `DEVPILOT_TOKEN_KEY`. Generate it with `backend\.venv\Scripts\python.exe -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"`.
3. **Connect.** Restart the live server. On the Tools page, choose "Connect GitHub" and approve on GitHub. Pass if it shows "Connected as <you>".
4. **Make a throwaway repository** you own, for example `devpilot-sandbox`, with a small Python program, a README and a LICENSE.

### D. Similar projects and gap analysis

1. **Prompt:** "Improve https://github.com/<you>/devpilot-sandbox: add a command that exports results to CSV, with tests."
2. **Pass if:**
   - the repository is read before the run starts, with its visibility, commit and licence;
   - GPT records a gap analysis, which shows in the timeline as "Existing code compared…";
   - the plan follows that analysis.
3. A private repository should open the "Permission needed" popup first.

### E. Modify the repository and open an approved pull request

1. Continue D, or ask "…and open a pull request with the change".
2. **Pass if:**
   - the run asks for GIT_PUSH approval, showing the account, the branch and every file;
   - after approval, a pull request exists on a **new branch** with only the changed files;
   - `main` is untouched;
   - the timeline shows "Pull request opened".
3. Denying the approval must send nothing.

## Stage 4: n8n (needs your approval to pull `n8nio/n8n`, about 600 MB)

1. **Start n8n.** Set `N8N_WEBHOOK_SECRET` in `.env`, then run `.\scripts\n8n-local.ps1`. In `http://127.0.0.1:5678`, create the owner account and an API key, and activate all three workflows.
2. **Configure `.env`:** `N8N_ENABLED=true`, `N8N_API_KEY`, and `N8N_CALLBACK_URL=http://host.docker.internal:8001/integrations/n8n/callback`.
3. **Live test (free, local only):**

   ```powershell
   $env:RUN_LIVE_N8N_TESTS='true'; $env:N8N_WEBHOOK_SECRET='<same>'; $env:N8N_API_KEY='<key>'
   cd backend; .\.venv\Scripts\python.exe -m pytest tests/live/test_n8n_live.py -s
   ```

   **Pass if:** every workflow is found and active, and each signed trigger ends in a successful n8n execution.
4. **In a paid run**, ask a build to "notify me through n8n when it is delivered".
   - **Pass if:** an approval is requested (or skipped for `notify` with `N8N_AUTO_APPROVE=true`), n8n shows the execution, and the Tools page shows it.
   - With `echo` and `wait_for_workflow`, the run pauses, and n8n's callback resumes it. Leave `REQUIRE_SPEND_APPROVAL` off for this one test.

## Stage 5: Google Drive RAG (needs a Google OAuth client and the parser image)

1. **Create the OAuth client.** In Google Cloud, create an OAuth client of type *Web application* with redirect `http://127.0.0.1:8001/oauth/google/callback`. Add yourself as a test user, and enable the Drive API.
2. **Configure `.env`:** `DRIVE_SOURCE=google`, `GOOGLE_OAUTH_CLIENT_ID`, `GOOGLE_OAUTH_CLIENT_SECRET`, `GOOGLE_OAUTH_REDIRECT_URI`, `RAG_EMBEDDER=openai`, `OPENAI_EMBEDDING_PRICE_PER_MTOK`, and `RAG_EMBEDDING_MAX_USD=0.05`.
3. **Approve building `document-parser`.** Without it, every PDF and DOCX is refused.
4. **Prepare documents.** Put two or three non-sensitive documents in a test folder.
5. **Do not restart the backend between consent and asking.** Drive consent is kept in memory for now (FEATURE_STATUS.md, item 1).

### F. Google Drive-backed RAG application

1. **Prompt:** "Build a chatbot that answers questions from the documents in <folder link>, citing its sources."
2. **Pass if:**
   - the Google permission popup appears, and consent grants only that folder;
   - ingestion and indexing record the real embedder;
   - `POST /api/v1/projects/<id>/knowledge/ask` with `approved_max_usd: 0.02` returns a grounded answer whose citations quote the documents;
   - a question the documents do not answer returns the not-found answer;
   - revoking access removes the chunks.
3. Generating the chatbot application itself needs the FastAPI and React verifier images. Until they are approved, the run stops before coding with that reason, which is the correct result.

## After each stage

1. Compare DevPilot's recorded cost with OpenAI's usage page.
2. Add a dated entry to `docs/PROGRESS.md` with the record above.
3. Only then move a feature to "tested live" in FEATURE_STATUS.md.
