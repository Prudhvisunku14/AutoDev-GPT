# Docker and real-execution readiness for full-stack projects

**Status (2026-11-02): the five sandbox changes are implemented and offline-tested, not committed and not Docker-tested.** No Docker command, image build, download, model call or GitHub request was made. Committed: GitHub milestone `6ead6b3`, full-stack support `01ac42b`, this document's first version `f2ec196`.

## 1. Where each part stands

- **Implemented:** the code exists.
- **Offline-tested:** tests pass with fakes, fixtures, replayed results, or the host's own pytest and Node over trusted fixtures.
- **Pending Docker verification:** it must still run in real containers.
- **Pending package download:** it needs packages or images from the internet first.
- **Pending approval:** a person must allow it to run for real.

| Part | Implemented | Offline-tested | Pending Docker verification | Pending package download | Pending approval |
|---|---|---|---|---|---|
| Sandbox isolation with `python:3.12-alpine` (§3) | Yes | Yes | A re-run only: 9 real-container tests passed on 2026-10-30 | No | No: approved for `python-cli` |
| The MVP workflow on Docker | Yes | Yes | A re-run only: 7 real-container tests passed on 2026-10-31 | No | No |
| Multi-stack plans, component tasks, the folder audit, per-component verification, the dependency gate | Yes (`01ac42b`) | Yes | Yes | No | Yes, per runtime |
| 4.1 One job builder; recipe limits capped by the settings | Yes, uncommitted | Yes | Yes | No | No |
| 4.2 Image approval by exact reference or full image ID | Yes, uncommitted | Yes, with a fake Docker CLI | Yes | No | Yes: the two images, by ID |
| 4.3 FastAPI pytest options and DevPilot's pytest config | Yes, uncommitted | Yes: the host's pytest 9.2.0 with the image's config file | Yes | Yes: the wheels | Yes |
| 4.4 React runner and DevPilot's Vitest config | Written, uncommitted | The copy and its checks only, with the host's Node 24; **Vitest has never run** | Yes | Yes: the Node image, npm packages, lockfile | Yes |
| 4.5 Front-end acceptance bound to its component | Yes, uncommitted | Yes, with simulated runtimes | Yes | Yes: needs the React image | Yes |
| Image `devpilot/verify-fastapi:1` | A specification | The pin closure only | Yes | Yes: wheels, hashes | Yes |
| Image `devpilot/verify-react:1` | A specification; versions pending | The runner only | Yes | Yes: everything | Yes |
| OpenCode in real containers, scripted model, no key | Yes | Yes | A re-run only: ran 2026-10-31 to 2026-11-01 | No | A real model: paid calls |
| OpenCode on FastAPI and React with a real model | No: designed (§9) | No | Yes | Yes: the images | Yes: paid calls |
| Live GitHub search and import | Yes | Yes, with GitHub-shaped answers in process | No | No | Yes: network |
| Live App Preview, Google Drive-to-RAG | No | No | | | Roadmap |

## 2. Environment (read-only checks, 2026-11-02 22:03)

| Item | Found |
|---|---|
| Storage | C: 4.41 GB free of 183 GB. D: 128.63 GB free |
| Memory | 15.65 GB of RAM, 1.26 GB available. Commit charge: 22.10 GB of a 26.24 GB limit (84%) |
| Page file | Only `C:\pagefile.sys` is in use: system-managed size, 10.6 GB allocated, 2.1 GB used (peak 3.3 GB since the 10:25 boot). A D: entry is configured but unused |
| Largest memory users | One WebView2 process, 2.5 GB. VS Code, about 3.5 GB. Chrome, at least 2.4 GB. `mysqld`, 0.6 GB. HP `SysInfoCap` uses 0.2 GB now; it reached 16.3 GB earlier today |
| Docker Desktop | Version 4.97.0 at `D:\DockerDesktop`. CLI 29.9.2 (`D:\DockerDesktop\resources\bin\docker.exe`, on `PATH`). Not running. `com.docker.service` is stopped (manual start). Start at login is off |
| Docker data | All on D:. The WSL distro `docker-desktop` is at `D:\DockerData\main` (0.1 GB). Images and volumes are in `D:\DockerData\disk\docker_data.vhdx` (8.42 GB). On C:, only logs (0.03 GB) |
| WSL | Service running, no VM running. There is no `.wslconfig`, so Docker's VM may grow to half the RAM (about 7.8 GB) |
| Pending restart | None from Windows Update or servicing. 62 pending file renames (Office, Edge, fonts, setup logs), none for Docker or WSL; they clear at the next restart |
| Power | On AC, 100% |
| Images, as last recorded (not re-read, because the engine is off) | `python:3.12-alpine` (`sha256:e6dae589…`, pulled 2026-10-30). `devpilot/opencode-check:1.19.5` (`sha256:c380fdb5…`). `devpilot/opencode-runtime:1.19.5` (`sha256:a7753fe4…`). SafePassage's images, which are not DevPilot's. No `devpilot/verify-*` image exists |

**Headroom: not enough for a safe start today.**
- Windows grows the page file once commit passes about 90% of the limit. That is 23.6 GB, only 1.5 GB above the current commit.
- Docker's VM uses 2 to 4 GB in normal use (an estimate), and up to 7.8 GB.
- So starting Docker now would very likely grow `C:\pagefile.sys` into the 4.41 GB left on C:.
- On 2026-11-01, C: reached 0 MB and Docker Desktop then failed to restart.

Start Docker only when all of these hold:
- commit is at or below 70% of the limit;
- at least 4 GB of RAM is available;
- at least 10 GB is free on C:;
- the laptop is on AC power;
- no heavy job is running, such as a SafePassage install or build.

Closing browser tabs and spare VS Code windows lowers commit fastest. Lowering commit matters more than freeing disk, because page-file growth can use up space freed on C: again.

Ways to free space on C:. Nothing was deleted; each is your decision:
1. Windows Update's download cache, 9.69 GB in `C:\Windows\SoftwareDistribution\Download`. Use Disk Cleanup, "Clean up system files", Windows Update Cleanup. No update restart is pending.
2. The Recycle Bin, 4.59 GB across all drives. Review it, then empty it.
3. Temporary files, 0.85 GB in your Temp folder. Use Settings, System, Storage, Temporary files.
4. Smaller items: the npm cache (0.33 GB), browser caches (about 0.5 GB), VS Code caches (0.27 GB).
5. A Windows setting, so a bigger decision. `powercfg /h /type reduced` shrinks `hiberfil.sys` (6.3 GB earlier today). `powercfg /h off` removes it, and hibernation with it.
6. A WSL setting. A `.wslconfig` containing `[wsl2]` and `memory=4GB` would cap Docker's VM, and with it how far commit can rise.

Moving the page file to D: does not work on this machine. A D: page file set up on 2026-11-02 was not used after a restart.

## 3. The sandbox today (read from the code)

`build_create_args` (`backend/app/sandbox/docker_cli.py:73-106`) creates every job with these settings:
- **Images:** `--pull never`.
- **Network and privileges:** `--network none`, `--cap-drop ALL`, `--security-opt no-new-privileges`.
- **Filesystem:** `--read-only`, with `--tmpfs /tmp:rw,noexec,nosuid,nodev,size=64m`.
- **Process:** `--user 65534:65534`, `--ulimit nofile=256:256`, `--init`, `--workdir /workspace`.
- **Environment:** `HOME=/tmp` is the only variable.
- **Mounts:** the workspace, bind-mounted read-only, is the only mount.
- **Resources:** memory (with no swap beyond it), CPU and process limits come from the settings, or from the job's own limits, which can only be lower (§4.1). The defaults are 1g, 1 CPU, 256 processes and 300 s.
- **Never present:** a Docker socket, privileged mode or a host namespace.

`DockerSandbox._check_spec` (`runner.py:90-104`) refuses, before Docker is called: any image not exactly in `APPROVED_IMAGES`, any job asking for more than the settings allow, and any folder outside the workspace roots.

The only approved sandbox image is `python:3.12-alpine`, approved by tag. `--pull never` stops a silent change, but an explicit `docker pull` would re-point the tag, so pinning it by ID is a follow-up.

None of this changes.

## 4. The five sandbox changes: implemented offline

All five are in the working tree, not committed, and none has run on Docker. Every test that guarded a boundary before still passes unchanged, and none was loosened:
- one test helper changed on purpose: the full-stack tests' simulated approval now also puts the images on the approved list, because a runtime now needs both;
- the FastAPI recipe uses pytest's long options, so the existing ban on `-c` in any recipe still holds.

### 4.1 Per-recipe resource limits: implemented, offline-tested

**Now**
- `sandbox/policy.py` adds three pieces:
  - `Limits`, which refuses an invalid value: memory not like `512m` or `1g`, CPUs or timeout not positive and finite, processes outside 16 to 4096;
  - `capped`, which takes the smaller of the job's value and the settings', comparing memory in bytes;
  - `build_job`, the one place a container job is made: an approved image, a fixed non-empty argument list, and capped limits.
- `build_spec` goes through `build_job`, and every approved command keeps the settings' memory, CPU and process limits, so a Python-only project gets exactly what it got before.
- Component jobs come from `recipes.recipe_spec`, and acceptance jobs from `recipes.acceptance_spec`. `coding.py` no longer builds an `ExecSpec` itself, or swaps an image afterwards.
- `ExecSpec` carries optional `memory`, `cpus` and `pids`, and `build_create_args` uses them, or the settings when they are absent.
- `DockerSandbox` refuses any job above the settings, however it was made.

**React (decision pending):** the recipe asks for 2g, 2 CPUs and 512 processes, so it gets the defaults: 1g, 1 CPU and 256. Nothing was raised. Decide after measuring on Docker.

**Offline tests**
- `test_sandbox_job_builder.py`: capping, the exact limits approved commands keep, the container flags, invalid limits, runtimes that cannot be defined, and the runner's refusal.
- `test_verification_recipes.py`: every recipe's capped limits, under the default settings and tighter ones.
- `test_fullstack_coding.py`: the React acceptance job is capped.

**Pending Docker:** `docker inspect` shows a recipe job's `Memory`, `NanoCpus` and `PidsLimit`, and an OOM kill under a recipe limit is reported as `MEMORY_LIMIT`.

### 4.2 Image approval: implemented, offline-tested

**Now**
- `valid_reference` accepts `name:tag` (never `latest`, with an optional `@sha256:` digest) or a full image ID: `sha256:` followed by 64 lower-case hex characters. Anything else shaped like a digest is refused: a short ID, upper case, `sha512:…`.
- `approved_image` returns the entry for exactly that reference, or refuses. `build_job` and `DockerSandbox` both use it.
- A runtime is ready only when its recipe is approved **and** its image is in `APPROVED_IMAGES`.
- `python-runner` (`python:3.12-alpine`) is unchanged and is the only entry. The FastAPI and React images are not approved.

**Offline tests:** `test_sandbox_job_builder.py`:
- valid and refused references;
- only an exact match runs, and the table is unchanged;
- an ID-pinned job is created with that ID and `--pull never`;
- an approved image missing from the disk is never pulled and is judged `NOT_RUN`.

Readiness with the flag alone is tested in `test_verification_recipes.py`, and as a full run in `test_fullstack_coding.py`.

**Pending:** the built images' IDs, then approval in one reviewed commit (§8).

### 4.3 FastAPI test execution: implemented, offline-tested

**Now:** the recipe runs:

`python -I -B -m pytest --config-file=/opt/verify/pytest.ini -p no:cacheprovider -v --color=no --rootdir=/workspace/<folder> --confcutdir=/workspace/<folder> --override-ini=pythonpath=/workspace/<folder> /workspace/<folder>`

- The config is the image's own (`backend/sandbox_images/verify-fastapi/pytest.ini`), so a project's `pytest.ini`, `pyproject.toml`, `tox.ini` or `setup.cfg` is never read.
- Every folder argument is the one checked folder.
- The acceptance suite still runs on its own, with `python -m unittest` in the same image.

**Offline tests:** `test_fastapi_recipe_options.py` runs the recipe's exact arguments with the host's pytest 9.2.0 and the image's config file. The fixture has a hostile `pytest.ini`, a hostile `pyproject.toml`, and a `conftest.py` outside the component.
- The component imports, and its one test passes.
- Nothing outside the component loads, and nothing is written.
- With the old arguments, the same fixture cannot be judged.

**Limit:** the candidate's own `conftest.py` inside the folder still runs. That run is evidence only for its own tests (§6).

**Pending:** the wheels and the image, then the same check inside the container.

### 4.4 React test execution: written, partly offline-tested, not Docker-tested

**Now:** the recipe is `node /opt/verify/run-vitest.mjs /workspace/<folder>`. The runner, `backend/sandbox_images/verify-react/run-vitest.mjs`:
1. checks the folder is one level under `/workspace`;
2. copies only approved file types into `/tmp/app`, leaving out dot files, `node_modules`, `dist`, `build` and `coverage`;
3. refuses links (symlinks and junctions), hard-linked files, special files, paths that resolve elsewhere, and trees over 2,000 files, 16 MB, 1 MB a file or 12 levels;
4. links the image's `node_modules`;
5. runs Vitest with DevPilot's config, `vitest.config.mjs`, and a fixed environment. The config sets jsdom, puts the cache in `/tmp/vite-cache`, and fails the run on `.only` or on no tests;
6. prints only the JSON report.

It never starts npm, and it imports no network module.

**Offline tests:** `test_react_runner.py`, with the host's Node 24.14.1, covers the copy, every refusal, Vitest's arguments and environment, the runner's imports and its single process, and bad arguments. **Vitest itself has never run, and the config is unchecked against a pinned Vitest.**

**Pending:**
- the Node image, npm packages and lockfile;
- then the runner inside the image: passing and failing fixtures, a tree over the limits, and no write to `/workspace`.

### 4.5 Front-end acceptance: implemented, offline-tested with simulated runtimes

**Now**
- **Binding:** a criterion may carry `component`, in the API and in the frozen suite. It is then proved only by files named `<component>/__acceptance__/<name>.test.{js,jsx,ts,tsx}`, whose plain `test("AT-…")` and `it("AT-…")` titles are read by pattern, never run.
- **Backward compatibility:** a suite without components freezes to exactly the same text and hash as before.
- **Staging:** the candidate's `*.test.*`, `*.spec.*` and `__acceptance__` files are stripped.
- **Running:** each front-end component's files run in that component's runtime, as `react-acceptance` (the same runner with `--acceptance`), after every component's own tests pass. Each run is reported as a separate part.
- **Never passed by existing:** a criterion whose run did not happen is `NOT_RUN`, never `PASSED`.
- **Blocking:** a criterion bound to a component the plan lacks (any one-stack plan included), or whose runtime cannot run front-end files, blocks verification with `verification_unavailable` before any test runs.
- **Who sees what:** GPT and recovery see the unmet criterion and its component, never the tests.

**Offline tests**
- `test_acceptance.py`: binding, refusals, freezing, tamper evidence, staging, judging, and separate parts.
- `test_api_acceptance.py`.
- `test_fullstack_coding.py`:
  - a full run hidden from the provider;
  - a failing front-end criterion blocks delivery;
  - a missing component blocks verification;
  - nothing runs without approved runtimes.

**Pending:** the React image, then a front-end acceptance run on Docker.

### What only Docker can show

1. The container flags with a recipe's own limits, and an OOM kill under them.
2. An image pinned by ID, created with `--pull never`, in the real engine.
3. pytest inside `devpilot/verify-fastapi:1`, from the read-only workspace, with its config at `/opt/verify/pytest.ini`.
4. The React runner and Vitest inside `devpilot/verify-react:1`:
   - the copy under the 64 MB `noexec` tmpfs, and the `node_modules` link;
   - Vite's cache in `/tmp`, and the JSON report;
   - the config against the pinned Vitest version.
5. Front-end acceptance, end to end, including a hostile component. A `vitest.workspace.*`, `vitest.config.*` or `vite.config.*` file in it must not change which files run. With `--config` given, Vitest should look for a workspace file only beside DevPilot's config, but that depends on the pinned version. Until it is shown, the React runtime stays unapproved.
6. Opt-in Docker tests for steps 6 to 9 of §8 do not exist yet; they are written with the images.

## 5. Image specifications (not built)

| Requirement | `devpilot/verify-fastapi:1` | `devpilot/verify-react:1` |
|---|---|---|
| Files | `backend/sandbox_images/verify-fastapi/` | `backend/sandbox_images/verify-react/` |
| Base image | `python:3.12-alpine`, already approved and recorded locally; pin by image ID. **Unverified:** that a musllinux wheel exists for every pin, `pydantic_core` above all. If not, the base becomes `python:3.12-slim` | Node LTS on Alpine. **Pending:** the tag and its ID. The placeholder reference makes a build fail rather than pick a version |
| Dependencies | 24 exact pins from `backend/requirements.lock`. Their closure was checked offline on 2026-11-02 against the venv's installed metadata, with Linux markers; that check added `annotated-doc` 0.0.5, `opentelemetry-api` 1.45.1 and `Pygments` 2.21.0 | React, React DOM, Vite, Vitest, jsdom, the React plugin, Testing Library and TypeScript. **Every version pending** |
| Reproducible install | `pip install --no-deps --only-binary=:all:`, then `pip check`: only the pinned wheels, and no package code runs. Hashes (`--require-hashes`): **pending** until first resolution | `npm ci --ignore-scripts` from a lockfile generated in the Node image, with integrity hashes: **pending** |
| User at test time | 65534, set by the image and forced by the sandbox | Same |
| Filesystem | Read-only root and workspace; `/tmp` is 64 MB and `noexec` | Same. Vite's cache must be in `/tmp` (§4.4) |
| Network at test time | None (`--network none`). The build needs the network, with approval | Same |
| Docker socket | Never mounted; a test asserts this | Same |
| Resource limits | The recipe's 1g, 1 CPU, 256 processes and 180 s, capped by the settings (§4.1) | The recipe asks for 2g, 2 CPUs, 512 processes and 300 s, and gets the settings' 1g, 1 CPU and 256 (§4.1: decision pending) |
| Scripts | No entry point; the recipe gives the argument list, with DevPilot's `pytest.ini`. No install or build step at test time | Only the fixed `run-vitest.mjs` and `vitest.config.mjs`: written, tested offline with the host's Node, never in the image. Lifecycle scripts never run |

A project's declared dependencies must all be among the image's packages. Otherwise verification stops before running and names the missing ones. Nothing is installed when tests run.

## 6. Component execution design

1. Each component's tests run from its own folder, by its recipe, in its own image, and are recorded separately (`verification.component`).
2. The candidate is mounted read-only. Only the coding attempt writes, and the audit refuses changes outside the task's component folder.
3. Dependencies are checked against the image before any container starts. A missing one blocks the run, with the names.
4. The evaluator's acceptance suite runs only after every component passes, in a separate folder, in the Python component's image.
5. Failures go through the existing classification and recovery. A security finding or an unproved state stops the run.
6. One report covers every component, every acceptance criterion, the reused code's licence and what was simulated.

## 7. First Docker health check (prepared, not run)

Each step needs your go-ahead. Stop at any failed check, and run nothing else in between. This check runs no container and needs no download.

1. **Check C: and memory (read-only).** Go on only if §2's thresholds hold.
   ```powershell
   Get-PSDrive C, D | Select-Object Name, @{n='FreeGB'; e={[math]::Round($_.Free/1GB, 2)}}
   (Get-Counter '\Memory\% Committed Bytes In Use', '\Memory\Available MBytes').CounterSamples | Select-Object Path, CookedValue
   Get-CimInstance Win32_PageFileUsage | Select-Object Name, AllocatedBaseSize, CurrentUsage
   ```
2. **Start Docker Desktop.** You start it from the Start menu. `docker desktop start` also works: the plugin is installed, but it hasn't been tried here.
   - Don't sign in, and don't install an update.
   - Docker Desktop's own update check and usage statistics are on by default, and its settings file overrides neither. So Docker Desktop itself may contact Docker's servers. Turning both off first is your choice.
   - DevPilot needs no network for this check.

   Wait until Docker Desktop reports that the engine is running.
3. **Confirm the engine is healthy (read-only).**
   ```powershell
   docker context show        # expect desktop-linux
   docker version --format '{{.Client.Version}} / {{.Server.Version}}'
   docker info --format '{{.ServerVersion}} | {{.OperatingSystem}} | {{.DockerRootDir}} | {{.MemTotal}} | {{.NCPU}}'
   docker system df
   ```
   Stop if there is no server version, or if the context is not `desktop-linux`.
4. **Check the local images (read-only; nothing is pulled).**
   ```powershell
   docker image ls --digests
   docker image inspect python:3.12-alpine --format '{{.Id}} {{json .RepoDigests}} {{.Os}}/{{.Architecture}}'
   docker image inspect devpilot/opencode-runtime:1.19.5 --format '{{.Id}}'   # expect sha256:a7753fe4...
   docker ps -a --format '{{.Names}}  {{.Status}}'                            # SafePassage's containers: leave them alone
   docker ps -aq --filter label=devpilot.sandbox=true                          # expect nothing
   ```
   Compare the results with §2. Report a missing image; never pull it.
5. **Confirm that nothing was pulled.** `docker events --since 15m --until 1s --filter type=image` should print nothing. Every sandbox job is created with `--pull never` and `--network none` (`docker_cli.py:86-89`), and step 2 of §8 needs only `python:3.12-alpine`.
6. **Stop Docker cleanly.** Quit Docker Desktop from its tray icon, or run `docker desktop stop`. Then confirm:
   ```powershell
   Get-Process | Where-Object { $_.Name -match 'docker|vmmem' }   # expect nothing
   $env:WSL_UTF8 = '1'; wsl -l -v                                  # docker-desktop: Stopped
   ```
   Record C:, commit and the page file again. Don't use `wsl --shutdown` unless the VM will not stop, and then only with your approval.

**Abort rule:** if C: falls below 2 GB free, or commit passes 90%, at any point, quit Docker Desktop at once.

The engine pauses itself after 5 idle minutes. A slow first answer after a pause is not a failure.

## 8. Real Docker verification: the ordered procedure

Each step is a separate approval. Stop on any failure and report it.

| # | Step | Downloads | Expected use |
|---|---|---|---|
| 1 | The health check (§7) | none | Docker's VM: 2 to 4 GB of memory (estimate) |
| 2 | The 16 opt-in tests, with `RUN_DOCKER_TESTS=true` | none (`python:3.12-alpine` is recorded as present) | about a minute (7 + 9 tests when they last ran, 2026-10-30 and 2026-10-31) |
| 3 | Resolve the wheel hashes and build `devpilot/verify-fastapi:1` | about 15 to 30 MB of wheels (estimate) | about 100 MB of image, on D: |
| 4 | Choose the Node image, generate the lockfile inside it, and build `devpilot/verify-react:1` with the runner and config | the Node image (about 50 MB compressed) and npm packages (about 150 to 250 MB unpacked); both estimates | about 300 to 400 MB of image, on D: |
| 5 | §4.1 to §4.5: **done offline, not committed**. Still to do: record the images' IDs, leave `approved` false, and write the opt-in Docker tests for steps 6 to 9; they approve the images inside the test only, as `approve_runtimes` does | none | |
| 6 | Run each recipe on a fixture project with passing and failing tests, and confirm the parsers | none | seconds each |
| 7 | One component-level run of the weather plan, with scripted code | none | under a minute |
| 8 | Independent acceptance on that run, Python and front end | none | seconds |
| 9 | Recovery and safe termination: a failing test, a missing dependency, a timeout | none | about a minute |

Only after step 9 may a person approve a runtime (§4.2), in code and reviewed.

**Rollback:** remove the images (`docker image rm devpilot/verify-fastapi:1 devpilot/verify-react:1`) and the build cache (`docker builder prune`), each approved separately. Until a runtime is approved in code, DevPilot's behaviour does not change: its stacks stay blocked. Docker's data lives on D:. The risk is C: (§2).

## 9. Real coding-provider readiness

**Why OpenCode cannot claim FastAPI or React yet:**
- It has been shown working only on `python-cli`, with a scripted model.
- Its profile claims only that stack, and must keep doing so until a real run proves another one.
- The verification runtimes for those stacks do not exist, so their output could not be checked anyway.

**A minimum real-model verification, once §8 passes and paid calls are approved:**

| | FastAPI task | React task |
|---|---|---|
| Task | The weather back end's contract (`app/demo/weather.py`) | The dashboard: city search, reading, loading and error states |
| Limits | At most 8,000 output tokens a request, 40 requests a task, 1 attempt and 1 repair | The same |
| Spend | **$0.75 cap**, enforced by the gateway (`GATEWAY_MAX_USD_PER_TASK`) and the run budget | **$0.75 cap**; **$1.50 in all** |
| Success | Its own tests pass in `devpilot/verify-fastapi:1`, and the frozen acceptance suite (AT-001 to AT-006) passes | Its own Vitest tests pass in `devpilot/verify-react:1`, and a frozen front-end acceptance file (§4.5) passes |
| Failure | Anything else, including a budget stop, is recorded as it is; there is no retry beyond the one repair | The same |

Only a success in a real run may add the stack to OpenCode's profile.

## 10. Approvals needed for the next real Docker test

1. The health check (§7). It needs §2's thresholds first, which may mean closing apps or freeing space on C:.
2. Run the 16 opt-in tests (`RUN_DOCKER_TESTS=true`).
3. Download the FastAPI wheels and build `devpilot/verify-fastapi:1`.
4. Choose the Node LTS image, download it and the npm packages, generate the lockfile, and build `devpilot/verify-react:1`.
5. Review and commit the five sandbox changes (§4), now implemented offline.
6. Run the recipe checks, the component-level run, and the acceptance and recovery checks (§8, steps 6 to 9).
7. Separately, and later: paid model calls ($1.50 cap), the key set by you in `.env`, live coding approval, and OpenCode enabled.
8. Separately: GitHub network access, which needs `codeload.github.com` in the egress allowlist and either a read-only token or a search limit of 4.
