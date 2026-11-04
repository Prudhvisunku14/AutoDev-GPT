# DevPilot AI dashboard

A React client for the FastAPI backend in `../backend`. It reads projects, runs, events, files and reports; creates
projects and starts runs only after the user explicitly approves the backend-enforced spending limit. It never calls
a model or stores API or model credentials. The API token stays in this tab's `sessionStorage`.

Stack: React 19, Vite 7, TypeScript, Tailwind CSS 4, Lucide icons, React Router 7, TanStack Query 5. Tests use Vitest
and Testing Library.

## Run it locally

1. Start the backend (from `backend`, in its own terminal):

   ```powershell
   & .\.venv\Scripts\python.exe -m app
   ```

   It listens on `http://127.0.0.1:8000` unless `DEVPILOT_HOST`/`DEVPILOT_PORT` say otherwise. Starting it does not
   call OpenAI. A build may call paid services only after the user approves the enforced spending cap and the backend
   confirms that its price and build capabilities are available.

2. Start the dashboard (from `frontend`):

   ```powershell
   npm ci
   npm run dev
   ```

3. Open **http://localhost:5173** and paste the backend's `DEVPILOT_API_TOKEN` into the connect card. The token is
   checked against `/api/v1/status` first, and kept only in this tab's `sessionStorage`. "Forget the API token" in the
   account menu removes it.

The dev server proxies `/api` to the backend, so the browser only talks to `localhost:5173` and the backend's CORS
list does not change. If the backend runs somewhere else, copy `.env.example` to `.env.local` and set
`DEVPILOT_BACKEND_URL`. No token or key belongs in any env file here: Vite can put `VITE_`-prefixed values into the
bundle.

### Real builds: the coding engine

`python -m app` connects the OpenCode coding engine only when `OPENCODE_ENABLED=true`, and the `.env.example` template
ships it as `false`. That backend therefore shows **"Setup required — Coding engine is not configured"** (the missing
settings are listed under *Advanced details*), and **Approve and start build** stays disabled however often the box is
ticked: the backend refuses, and so does the button. Everything else works: projects, run history, approvals, and
downloading verified archives.
Setting the flag alone does not make live coding safe. The launcher also needs an approved runtime image, the verified
OpenCode binary, an `openai/<model>` name, prices and two request limits, and real builds need the guard and preflight
that the terminal builds use.

One-time setup (Docker Desktop running): the coding runtime must be a local image with ripgrep, or OpenCode would try
to download it. The verified `rg` binary and its Dockerfile are already in `D:\DevPilotCache\opencode\ripgrep-15.1.0`.
Nothing here uses the network.

```powershell
docker image inspect sha256:a7753fe443915eed28c577c1d1b3f0e27ab1c5bd16b7debc7de9579d2aaefe99 --format "{{.Id}}"   # present? then skip the build
docker build --network none --pull=false -t devpilot/opencode-runtime:1.19.5 D:\DevPilotCache\opencode\ripgrep-15.1.0\context
```

The build only works if the base image `devpilot/opencode-check:1.19.5` exists locally. To build from the dashboard,
stop the normal backend and serve with the configuration that delivered the verified attendance build instead (from
`backend`, Docker Desktop running, after `. ..\scripts\dev-env.ps1`):

```powershell
& $env:DEVPILOT_PYTHON scripts\live_server.py                          # preflight only: no key sent, nothing external
& $env:DEVPILOT_PYTHON scripts\live_server.py --approve-session 1.00   # then type "serve live"
```

It needs `OPENAI_API_KEY` and `DEVPILOT_API_TOKEN` in `.env`, the approved OpenCode runtime image and the python runner
image in Docker, the verified binary at `D:\DevPilotCache\opencode\1.19.5`, mains power, 5 GB free on D:, and the
machine floors of `live_build.py` (10 GB free on C:, 4 GB of memory available, commit charge under 70%). It
checks the key can see the model (not billed), dry-launches the containers, starts the guard and keep-awake, and then
serves on port 8000. Each build is limited to $0.50 and still needs its own approval in the dashboard, the session's
builds together stay within the `--approve-session` total, and the session ends after `--hours` (default 2).

Only OpenAI models are supported. Gemini is not: the OpenCode policy bundles only the OpenAI provider, the model
gateway forwards only to `api.openai.com`, and GPT is the only orchestrator by design.

### Console warnings from browser extensions

`MaxListenersExceededWarning` (with `[ObjectMultiplex]`) and `ObjectMultiplex - orphaned data` come from a wallet
extension's injected script (MetaMask's `obj-multiplex` stream), not from this app. The bundle contains no event emitter,
and a browser with no extensions logs neither. Disable the extension for `localhost` to hide them.

For a temporary Cloudflare demo, start the dev server as above, then use a second terminal:

```powershell
cloudflared tunnel --url http://127.0.0.1:5173 --http-host-header localhost:5173
```

The generated URL is public. Share it only with intended viewers, keep the backend token strong and private, and use
Cloudflare Access when available. The API remains authenticated; the tunnel does not make backend routes public.
This tunnel setup is a development/demo path, not a production hosting configuration.

## Checks

```powershell
npm run typecheck   # tsc
npm run lint        # ESLint
npm test            # Vitest: unit and component tests
npm run build       # type-check, then the production bundle in dist/
```

## What each page shows

- **Home**: enter a requirement and press **Build Application**. A dialog shows the application name and the
  backend-enforced maximum spending; the build starts only after the approval box is ticked **and** the backend reports
  it is ready. One status line says **Ready to build** or **Setup required — …**; the full diagnostics sit under
  *Advanced details*. Builds use the Python command-line stack, the only one verified end to end. A request for another
  kind (React, FastAPI, a website…) is declined with one sentence rather than quietly built as something else.
- **Projects** and **project details**: each saved requirement, its runs and its delivered files.
- **Runs**: every run, newest first, with provider, cost, tests and outcome. Provider and tests come from a finished
  run's report.
- **Run details**: what the run is doing or why it ended, the plan, a grouped event timeline, the five stages, the
  verification results, the project's files and the final report. "Application generated successfully" appears only
  when the run is COMPLETED and the independent acceptance suite PASSED in a run with nothing simulated. A project
  ZIP is enabled only when the backend confirms a verified artifact: a COMPLETED run with nothing simulated whose
  independent acceptance suite passed, or, for a freeform request that has no such suite, whose own tests passed in
  the Docker sandbox. The second kind is labelled "Verified by the generated tests only".
- **Templates**: starting prompts. Choosing one only fills in the composer.

Stage status, progress and timings come from the run's real state and recorded events (`src/lib/stages.ts`). Activity
and run state are authenticated by the API token and polled; SSE is not used because browser `EventSource` cannot
send the required authorization header. A stage with no evidence stays pending, Retrieve shows "Skipped" when the run
used no retrieval, and progress counts finished stages: there are no estimated percentages.

The Runs page includes backend runs and read-only historical records from `scripts/live_build.py`/`live_demo.py`.
Historical entries are explicitly labeled and are never restarted. They appear only when the configured history
directory contains the script's summary and workspace evidence.

## Supported scope and safeguards

- The backend capability response is authoritative. Python CLI is the first generation target; FastAPI and React/Vite
  builds remain unavailable until their verification runtimes are implemented and pass their tests. SQLite is
  available only within a stack the backend reports as buildable. Static HTML is listed but has no approved
  verification recipe, and React + FastAPI is not yet a supported combined stack. Do not treat a Python-only build as
  proof of full-stack support.
- The backend enforces the approved per-run limit and existing policy checks. Exact future cost is not promised; a
  build is blocked if pricing or required build capabilities are unavailable.
- Pending approval decisions are sent to the backend's existing approval API. They may resume work and incur spend,
  so the user must make the decision explicitly.
- Artifact ZIPs contain selected project files and verification evidence. Environment files, credential-like files,
  configured secrets, caches, tool data and local databases are excluded; historical artifacts are labeled as such.
- The token is sent in the authorization header, never in URLs. Do not put backend tokens or provider keys in
  `VITE_` variables or frontend files.
