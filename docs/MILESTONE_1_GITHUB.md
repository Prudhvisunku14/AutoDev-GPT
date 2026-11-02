# Real-World Milestone 1: GitHub-assisted development

**Status (2026-11-02): public repository discovery, evaluation and safe import, multi-stack planning and framework verification recipes are implemented and tested offline. No live GitHub request, no real model, no Docker run of this milestone's code, and no React or FastAPI test has ever been executed. The weather application was not generated.**

| | Status |
|---|---|
| A. GitHub discovery and evaluation | Implemented, offline-tested with fixtures (invented repositories, labelled as such) |
| B. Live GitHub access | Pending. `GitHubSource` is wired into `python -m app` behind `REPOSITORY_SOURCE=github` (off by default), and tested only with GitHub-shaped answers served in process; it has never reached GitHub |
| Multi-stack planning | Implemented, offline-tested: components with their own stacks and folders, tasks per component, dependencies across them; one-stack plans unchanged |
| Framework verification recipes | Defined and offline-tested (commands, folders, runtimes, dependency checks, parsers on fixture output). **Never executed:** the FastAPI and React runtime images are not built, so their projects are blocked with a precise status |
| C. Real OpenCode generation | Pending real model authorization (OpenCode ran on real containers with a scripted model, 2026-10-31 to 2026-11-01) |
| D. Real Docker execution | Verified before Stages 2 to 4 (historical). Pending for the Stage 2 to 4 and Milestone 1 code |
| E. Independent acceptance | Implemented, offline-tested with replayed results. Pending in the real sandbox. The weather suite is checked by parsing only |

Docker and real-execution readiness for these stacks (status by stage, the environment checks, the five sandbox changes, the image specifications, the first health check, the ordered approval-gated procedure, the real-model plan): [DOCKER_READINESS.md](DOCKER_READINESS.md).

## Full-stack coding interfaces (committed as `01ac42b`)

| | Status |
|---|---|
| GitHub milestone | Committed locally as `6ead6b314c3b2fc674b08e383c942c2569bc8772` |
| Full-stack coding interfaces | Implemented: component tasks, per-component recipes and evidence, component folder enforcement |
| Scripted full-stack behaviour | Tested offline: FakeLLM, scripted files, fixture repositories and fixture sandbox output |
| Real GitHub connectivity | Pending |
| FastAPI and React runtime images | Pending: not built, so both stacks stay blocked |
| Docker verification of this code | Pending |
| Real AI code generation | Pending |

- **Provider declarations:** the simulated provider declares `python-cli`, `python-fastapi` and `react-vite`. OpenCode keeps claiming only `python-cli`, where it has been shown working (a Stage 2 rule). Declaring a stack never makes it runnable: `generate_code` stops the run before any selection when that stack's recipe has no approved runtime.
- **Component work:** GPT asks for a task, and the task names its component. The provider sees the disposable copy, but the audit refuses any change outside that component's folder, which counts as a security violation, so recovery stops. A task waits for its dependencies. Reused code in a component's folder can be adapted, and a component without reused code is built from scratch.
- **Verification:** for a multi-stack plan, `submit_implementation` builds one sandbox job per component from its recipe (Python chooses the image and the argument list), checks the declared dependencies against the runtime first, and blocks the run with the missing names otherwise. Each component is judged by its own parser and recorded as `verification.component`. The project passes only when every component passes and every MUST criterion is proved by a passing test somewhere, and the evaluator's suite runs in the Python component's runtime. Nothing is delivered otherwise.
- **The contract test** (`tests/unit/test_fullstack_coding.py`) approves the two runtimes inside the test only, labelled as simulated, to check what happens after that point. The weather request is coded in four component tasks; the back end fails its tests once, is repaired and passes; the front end passes; acceptance passes 7 of 7; and the files are delivered. Without that simulated approval, the same request stops at the first coding step.

## What was reused

The tool registry and executor, the policy engine and approval queue (`EXTERNAL_NETWORK` always needs a person), the egress allowlist (its `github` component), the workspace jail, the audit's rules for paths that could reconfigure the coding engine, the event log, the report, the selector and recovery, and independent acceptance. Nothing existing was replaced. There was no GitHub or Git code before; `GITHUB_TOKEN` was only declared so the redactor hides it.

## How it works

```
GPT: submit_analysis ─> search_repositories ─(approval)─> Python evaluates every result and decides reuse or build
     ─> import_repository ─(approval)─> pinned archive into the project's workspace ─> submit_plan ─> generate_code ...
```

- **Search and evaluation** (`app/repos/evaluate.py`): relevance from the request's words; each required technology must be shown by the repository's own manifests (`package.json`, `requirements*.txt`, `pyproject.toml`), not by its README; tests present; recent activity; size limits.
- **Licences** (`app/repos/licenses.py`): MIT, Apache-2.0, BSD, ISC, 0BSD, Unlicense and Zlib are reused, with their condition recorded. Copyleft (GPL, AGPL, LGPL, MPL and others) is refused until a person decides. No licence, or one the host cannot identify, means no reuse. A permissive licence without its file is refused, because attribution could not be kept.
- **Untrusted text:** the README, description and manifests are data. Text addressed to AI agents, or a download piped into a shell, makes a repository ineligible. Install hooks are flagged. GPT receives Python's evaluation and a 200-character description marked untrusted, never the README.
- **Decision:** Python reuses only an eligible repository scoring at least `REPOSITORY_REUSE_THRESHOLD` (0.55); otherwise it decides to build from scratch, and says why.
- **Import** (`app/repos/importer.py`): an archive of the pinned commit, not a `git clone`, so there is no `.git` folder, hook, submodule or configuration. It is read in memory and refused as a whole for a link, a device, a path outside the folder, a `.git` path, a path that could reconfigure the coding engine, or a size or count over the limits. It goes through the workspace jail, never overwrites a user's file, and is undone on failure. `THIRD_PARTY_NOTICES.md` and `DEVPILOT_IMPORT.json` record the URL, commit and licence. **Nothing downloaded is ever run.**
- **Enforcement:** `import_repository` is refused unless Python decided "reuse" for exactly that repository and commit in this run, once per run, in PLANNING. Delivery is refused if the reused code's licence file or `THIRD_PARTY_NOTICES.md` is missing.
- **Selection:** reuse changes what the coding provider is asked to adapt, not who is chosen; adaptive selection and recovery apply unchanged.

Nothing is specific to weather: the same tools serve any request.

## Multi-stack plans

`submit_plan` takes an optional list of components, each with a name, a stack from the catalog and one folder (for example `backend` on `python-fastapi` and `frontend` on `react-vite`). Every task then names its component and its language must match that component's stack. Tasks may depend on tasks in any component, and `generate_code` refuses a task until its dependencies are done. The gate refuses unknown stacks, repeated names or folders, a main stack that is no component's, unknown components, dependency cycles or references to missing tasks, components without tasks, and any requirement no task covers, so front-end requirements cannot be dropped because a back-end repository was found. Criteria may use any test method one of the components' stacks supports. A plan without components is validated exactly as before. Repository search and import take the component too, so each component gets its own reuse-or-build decision, and an import goes into that component's folder.

## Verification recipes

`app/verification/recipes.py` holds one recipe per stack, chosen by Python from the plan, never by GPT or a README. Each names an exact argument list (no shell), the component folder it runs in (one plain level, checked), a runtime image with no network, the packages that image provides, memory, CPU, process and time limits, the output format and its parser, the failure conditions, and how the evaluator's acceptance suite runs.

| Stack | Recipe | Runtime | Status |
|---|---|---|---|
| `python-cli` | `python -I -B -m unittest discover` | `python:3.12-alpine`, standard library only | Approved; verified in Docker before Stages 2 to 4 |
| `python-fastapi` | `python -I -B -m pytest -v` in `/workspace/<folder>`; pytest output parsed | `devpilot/verify-fastapi:1` (FastAPI, Starlette, Pydantic, httpx, pytest) | Defined, not built, never run |
| `react-vite` | `node .../vitest.mjs run --reporter=json --environment=jsdom --root /workspace/<folder>`; JSON parsed | `devpilot/verify-react:1` (React, Vite, Vitest, jsdom, Testing Library) | Defined, not built, never run |

Nothing is installed when a recipe runs: a project declaring a package its runtime does not provide fails first, with the missing names. The recipe commands are not sandbox commands GPT can call. When any component's runtime is not approved, `generate_code` writes nothing and the run ends `STOPPED_BY_POLICY` with outcome `verification_unavailable`, naming each blocked component and its reason in the status and the report.

## The weather application

`app/demo/weather.py` holds the request, GPT's scripted analysis and plan, and the evaluator's frozen acceptance suite.

**Contract for the back end:** `backend/app/main.py` exposes the FastAPI `app`; `backend/app/weather.py` holds `fetch_current(city)`, the only code that calls the provider, raising `CityNotFound` or `ProviderError`, and `provider_base_url()` from `WEATHER_API_BASE_URL`. `GET /api/weather?city=` answers `city`, `temperature_c`, `humidity_pct`, `wind_speed_ms` and `description`.

**Frozen acceptance suite** (AT-001 to AT-007): the agreed shape; numbers for temperature, humidity and wind; 422 for an empty, overlong or malformed city without calling the provider; 404 for an unknown city; 502 with no internal detail for a provider failure; a configurable provider URL; and a static check that the front end calls `/api/weather` through `VITE_API_BASE_URL` (SHOULD). The provider is always replaced by fixed values: no weather service is called and no weather data is invented. It needs FastAPI in the sandbox, so it has been checked by parsing only.

**Still pending:** a responsive layout, the loading and error states (they need a Node test image for Vitest or Playwright), and parsing the provider's raw response (written once the provider is chosen).

**What happens today, offline** (`python -m app.demo.fullstack`, 13 checks, all passing): the back end's search reuses `fixture-labs/fastapi-weather-api` (MIT); the front end's search finds only unsuitable repositories (GPL, no licence, no TypeScript, instructions aimed at agents), so it is built from scratch; the first import is denied by the person and fetches nothing, the second is approved and goes into `backend/` with its licence; the two-component plan with four dependent tasks and the criteria for all eight requirements are accepted; and the first coding step stops the run: "verification is blocked: backend (python-fastapi): the fastapi-pytest recipe needs the devpilot/verify-fastapi:1 runtime, which is defined but not built or approved; frontend (react-vite): ...". No code is generated and nothing runs.

## Offline tests

60 new tests: evaluation and licences on fixtures, every archive attack (links, traversal, absolute paths, `.git` hooks, engine configuration, several top folders, devices, oversize files and archives, non-archives), import records and rollback, `GitHubSource` through an in-process transport (egress refusal before any request, no token, the `codeload.github.com` redirect refused unless allowlisted, size limits, name and commit checks), and the orchestrated flow (each call waits for approval; nothing is fetched before approval or after a denial; an import Python did not choose is refused; build-from-scratch fallbacks; no overwrite; licence and notice enforced at delivery; the weather flows above). The readiness round added 37 more: multi-stack plans and every way to make one invalid, cross-stack criteria methods, task dependencies at coding time, recipe readiness, fixed commands and folder checks, dependency checks, the pytest and Vitest parsers on fixture output, the blocked status, the GitHub client through the tools with in-process answers, and the rehearsal. Whole offline suite: 3,385 passed, 17 skipped, 0 failed.

## Blockers

**For live GitHub access**
1. `codeload.github.com` is not in the egress allowlist. GitHub serves archives there, so imports would be refused. The allowlist file is held back, and changing it is the user's decision.
2. `GitHubSource` is wired into `python -m app`, but off: `REPOSITORY_SOURCE=github` turns it on, and each search and import still needs approval in the run.
3. Unauthenticated GitHub allows about 60 requests an hour. Evaluating 8 repositories takes about 90 (tree, README, licence and manifests each). Either lower `REPOSITORY_SEARCH_LIMIT` to 4 or approve a read-only token; no token is sent today.

**For building and verifying the weather application**
1. Multi-stack plans: resolved offline.
2. The `devpilot/verify-fastapi:1` and `devpilot/verify-react:1` images must be built from pinned packages, run once in the sandbox, and approved; only then do their recipes run. Building them downloads packages, so it needs approval.
3. The per-component verification step in `submit_implementation` is implemented and offline-tested (`01ac42b`). The five sandbox changes that running a recipe for real needs (per-recipe limits, image approval by ID, FastAPI imports, the React runner, front-end acceptance) are implemented and offline-tested, not committed and not Docker-tested ([DOCKER_READINESS.md](DOCKER_READINESS.md) §4).
4. OpenCode's profile claims only `python-cli`. The simulated provider declares all three stacks, which makes none of them runnable.
5. Real code generation needs paid-call approval; Docker needs approval to start.

## Readiness (read-only checks, 2026-11-02)

| Item | Found |
|---|---|
| Storage (22:03) | C: 4.41 GB free; D: 128.63 GB free |
| Memory (22:03) | Commit 84% of the limit; not enough headroom to start Docker safely ([DOCKER_READINESS.md](DOCKER_READINESS.md) §2) |
| Docker | Docker Desktop 4.97.0 and CLI 29.9.2 at `D:\DockerDesktop`; not running; `com.docker.service` stopped (manual) |
| WSL | WSL service running, no VM running; `docker-desktop` distro and image disk under `D:\DockerData`; `D:\WSL` exists |
| OpenCode | Verified 1.19.5 binary cached with its manifest |
| Deferred tests | 16 opt-in Docker tests: `test_mvp_end_to_end.py` (7), `test_sandbox_docker.py` (9) |

The offline rehearsal, any time: `& $env:DEVPILOT_PYTHON -m app.demo.fullstack` (from `backend`, reports in `data\fullstack-rehearsal`).

Commands for the Docker verification, once approved, after the health check in [DOCKER_READINESS.md](DOCKER_READINESS.md) §7 (Docker Desktop started by the user first):

```powershell
. .\scripts\dev-env.ps1; cd backend
docker version; docker image ls python:3.12-alpine                      # read-only
$env:RUN_DOCKER_TESTS = 'true'
& $env:DEVPILOT_PYTHON -m pytest tests/integration/test_mvp_end_to_end.py tests/integration/test_sandbox_docker.py
Remove-Item Env:RUN_DOCKER_TESTS
& $env:DEVPILOT_PYTHON -m app.demo.mvp --out D:\DevPilotCache\mvp-demo   # the existing Docker demonstration
```

## Permissions needed for the first real-world run

1. **GitHub network:** add `codeload.github.com` to the `github` egress component, and approve each `search_repositories` and `import_repository` call in the run.
2. **GitHub rate limit:** approve a fine-grained, read-only token for public repositories (set by the user in `.env`), or accept a search limit of 4.
3. **Docker:** start Docker Desktop and run the 16 opt-in tests with `RUN_DOCKER_TESTS=true`.
4. **Sandbox images:** approve building `devpilot/verify-fastapi:1` and `devpilot/verify-react:1` from the specifications in [DOCKER_READINESS.md](DOCKER_READINESS.md) §5.
5. **Model:** approve paid calls with a budget, the OpenAI key set by the user, `CODING_LIVE_APPROVED`, and OpenCode enabled.
6. **Weather provider:** choose it, and approve its key if it needs one, and its egress entry for the running application.

## Next requested capability

Google Drive-to-RAG development, as recorded in the product backlog in [PROGRESS.md](PROGRESS.md). Not started.
