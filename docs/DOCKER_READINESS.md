# Docker and real-execution readiness for full-stack projects

**Status (2026-11-02, rechecked at 22:03): a plan, not a run.** No Docker command, image build, download, model call or GitHub request was made for this document. Committed so far: GitHub milestone `6ead6b3`, full-stack support `01ac42b`. Offline suite on `01ac42b`: 3,389 passed, 17 skipped, 0 failed.

## 1. Where each part stands

The furthest state each part has reached:
- **Designed:** written down only.
- **Implemented:** the code exists.
- **Offline-tested:** tests pass with fakes, fixtures or replayed results.
- **Docker-tested:** it ran in real containers.

**Approved** means a person has allowed it to run for real.

| Part | Furthest state | Approved | Evidence |
|---|---|---|---|
| Sandbox isolation with `python:3.12-alpine` (the flags in §3) | Docker-tested | Yes (`python-cli`) | 9 real-container tests passed on 2026-10-30. Not re-run since Stages 2 to 4 |
| The MVP workflow on Docker | Docker-tested | Yes (`python-cli`) | 7 real-container tests passed on 2026-10-31. Not re-run on the current code |
| OpenCode in real containers, scripted model, no key | Docker-tested | Not for a real model | 2026-10-31 to 2026-11-01 |
| Multi-stack plans, component tasks, the folder audit, per-component verification, the dependency gate | Offline-tested | Not for FastAPI or React | `01ac42b`: `test_fullstack_coding.py` and the recipe tests |
| Recipes `fastapi-pytest` and `react-vitest` | Offline-tested: commands, folders, and parsers on fixture output | Not yet approved | Their runtimes do not exist |
| Independent acceptance in the sandbox | Offline-tested with replayed results | Python suites only | Never run in a real container |
| Image `devpilot/verify-fastapi:1` | Designed: a Dockerfile and 24 exact pins, with the dependency closure checked offline | Not yet approved | Never built |
| Image `devpilot/verify-react:1` | Designed, incomplete: no versions, no lockfile, no entry script | Not yet approved | Never built |
| The five sandbox changes (§4) | Designed | Not yet approved | None made |
| Front-end acceptance (a frozen Vitest file) | Designed | Not yet approved | None made |
| OpenCode on FastAPI and React with a real model | Designed (§9) | Not yet approved: paid calls | |
| Live GitHub search and import | Offline-tested with GitHub-shaped answers served in process | Not yet approved: network | |
| Live App Preview, Google Drive-to-RAG | Not designed | | Roadmap |

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

`build_create_args` (`backend/app/sandbox/docker_cli.py:73-102`) creates every job with these settings:
- **Images:** `--pull never`.
- **Network and privileges:** `--network none`, `--cap-drop ALL`, `--security-opt no-new-privileges`.
- **Filesystem:** `--read-only`, with `--tmpfs /tmp:rw,noexec,nosuid,nodev,size=64m`.
- **Process:** `--user 65534:65534`, `--ulimit nofile=256:256`, `--init`, `--workdir /workspace`.
- **Environment:** `HOME=/tmp` is the only variable.
- **Mounts:** the workspace, bind-mounted read-only, is the only mount.
- **Resources:** memory (with no swap beyond it), CPU and process limits come from the settings. The defaults are 1g, 1 CPU, 256 processes and 300 s.
- **Never present:** a Docker socket, privileged mode or a host namespace.

`DockerSandbox._check_spec` (`runner.py:91-99`) refuses two things before Docker is called: any image not in `APPROVED_IMAGES`, and any folder outside the workspace roots.

The only approved sandbox image is `python:3.12-alpine`, approved by tag. `--pull never` stops a silent change, but an explicit `docker pull` would re-point the tag, so pinning it by ID is a follow-up.

None of this changes.

## 4. The five sandbox changes: implementation checklist

None of these changes has been made. Each lands with its tests: offline first, then on Docker (§8, steps 6 to 9), before any recipe is approved. Tests that guard a boundary today must stay green and unchanged, except where noted.

### 4.1 Per-recipe resource limits

**Today**
- `ExecSpec` (`spec.py:26-34`) carries no limits, and `build_create_args` uses the settings (`docker_cli.py:91-94`).
- Component jobs are built directly at `coding.py:448`, and their timeout is the recipe's, uncapped. The one-stack path caps it (`policy.py:113-115`).
- That bypasses the rule in `spec.py:27` that only `build_spec` builds an `ExecSpec`.

**Change**
1. Add `build_recipe_spec(recipe, folder, root, settings)` to `sandbox/policy.py`. Use it at `coding.py:448`, and for the acceptance job instead of `replace(spec, image=...)` at `coding.py:629`.
2. Give `ExecSpec` optional `memory`, `cpus` and `pids` fields. Each takes the smaller of the recipe's value and the settings' value, with memory compared in bytes. The timeout works the same way.
3. In `build_create_args`, use the spec's values when set and the settings otherwise. Keep `--memory-swap` equal to `--memory`.

**Decision needed:** React's recipe asks for 2g, 2 CPUs and 512 processes, above the defaults of 1g, 1 CPU and 256. It gets the defaults unless the settings are raised. Measure on Docker before deciding.

**Guards today**
- `test_sandbox_docker_args.py`: `test_resource_limits_come_from_settings`.
- `test_sandbox_policy.py`: `test_a_command_can_only_lower_the_timeout`.
- `test_verification_recipes.py`: `test_every_recipe_declares_its_whole_contract`.
- On Docker, in `test_sandbox_docker.py`: `test_the_memory_limit_is_enforced` and `test_the_time_limit_kills_and_removes_the_container`.

**New tests**
- A recipe asking more than the settings gets the settings; one asking less gets its own values.
- The component timeout is capped.
- Memory units compare correctly.
- A spec without limits produces exactly today's arguments.

**On Docker**
- `docker inspect` shows a component container's `Memory`, `NanoCpus` and `PidsLimit`.
- An OOM kill is reported as `MEMORY_LIMIT`.

### 4.2 Approved image registration

**Today**
- `APPROVED_IMAGES` (`policy.py:52-58`) holds only `python-runner`.
- `Runtime.approved` (`recipes.py:36`) gates readiness.
- `_check_spec` would refuse the verify images even if a recipe were approved.
- The OpenCode runtime is already pinned by image ID (`opencode_container.py:42`).

**Change**
1. After §8 passes, add `verify-fastapi` and `verify-react` to `APPROVED_IMAGES`. Pin them by full image ID (`sha256:` followed by 64 hex characters), as `APPROVED_RUNTIME_IMAGES` does, because a locally built image has no registry digest.
2. Accept that form explicitly in `_REFERENCE` (`policy.py:21`). Today it would pass only by accident, read as an image named `sha256`.
3. Set each `Runtime.image` to the same ID, with `approved=True`, in one reviewed commit.
4. Add a consistency test: every approved recipe's image is in `APPROVED_IMAGES`.

**Guards today**
- `test_sandbox_policy.py`: `test_images_must_name_a_version` and `test_unpinned_or_unusual_image_references_are_refused`.
- `test_sandbox_docker_args.py`: `test_an_unapproved_image_is_refused_before_docker_is_called` and `test_images_are_never_pulled_implicitly`.
- `test_verification_recipes.py`: `test_gpt_cannot_reach_a_recipe_command_through_the_sandbox_tool`.
- `test_fullstack_coding.py`: `test_without_approved_runtimes_nothing_is_coded`.
- `test_verification_recipes.py`: `test_each_stack_has_its_recipe_and_only_the_built_runtime_is_approved`. This test changes on purpose, in the approval commit.

**On Docker**
- `docker image inspect --format '{{.Id}}'` equals the pinned ID.
- A job that names an absent ID fails without a pull.

### 4.3 FastAPI import handling

**Today:** `recipes.py:66` runs `python -I -B -m pytest -v -p no:cacheprovider --color=no /workspace/<folder>` from `/workspace`. That fails for a normal project layout:
- Under `-I`, neither the working directory nor `PYTHONPATH` is on `sys.path`.
- pytest's default import mode adds only the test file's own folder, such as `/workspace/backend/tests`.
- So `from app.main import app` fails.

**Change:** `command_for` adds three fixed arguments, built from the folder it has already checked:
- `--rootdir=/workspace/<folder>`;
- `-o pythonpath=/workspace/<folder>`;
- `-o addopts=`, so the project's own `addopts` cannot change the output the parser reads.

No variable is added, and the working directory stays the same.

**Limit:** the candidate's `conftest.py` still runs, and it can change pytest's reporting. So this run is evidence only for the candidate's own tests. The frozen acceptance suite is the independent check, with its same-process limit (`acceptance.py:15-18`).

**Guards today**, all in `test_verification_recipes.py`:
- `test_commands_are_fixed_lists_with_a_checked_folder_and_no_shell`;
- `test_a_folder_that_is_not_one_plain_level_is_refused`;
- `test_pytest_output_is_parsed_and_mapped_to_criteria`;
- `test_output_that_cannot_be_read_is_never_a_pass`.

**On Docker**
- A fixture whose `backend/tests` imports `app.main` passes.
- A `pytest.ini` with `addopts = -q` does not change the parsed result.
- Nothing is written to `/workspace`.

### 4.4 React verification script and working directory

**Today**
- `recipes.py:71` calls Vitest directly, with `--root /workspace/<folder>`.
- Modules would be looked up in `/workspace/<folder>/node_modules`, which does not exist.
- Vite writes its cache under `node_modules/.vite`, which is read-only.
- `run-vitest.mjs`, `package.json` and `package-lock.json` do not exist yet.

**Change:** the recipe becomes `node /opt/verify/run-vitest.mjs <folder>`. The script:
1. checks the folder name again;
2. copies the component, without any `node_modules`, into `/tmp/app`, with a size cap below the 64 MB tmpfs;
3. links `/tmp/app/node_modules` to `/opt/verify/node_modules`;
4. runs `vitest run --reporter=json`, with Vite's cache in `/tmp`;
5. prints only the JSON on stdout and its logs on stderr, and exits with Vitest's exit code.

`/tmp` is `noexec`, so native binaries (esbuild, rollup) stay under `/opt/verify`, where they can run; Node only reads the copied files. The script needs no new environment variable.

**Decision needed:** use a fixed Vitest config from the image, or the project's own.
- **Fixed config (recommended):** deterministic, and a project's config cannot redirect reporters or includes. A project that needs more fails with readable output, never a pass.
- **The project's config:** needed for features such as import aliases.

**Lockfile:** generate it inside the chosen Node Alpine image, so npm records the linux-musl native packages. A lockfile made on Windows may miss them.

**Guards today**
- `test_verification_recipes.py`: the fixed-list and folder tests, `test_vitest_json_is_parsed_and_its_names_map_to_criteria` and `test_output_that_cannot_be_read_is_never_a_pass`.
- `test_sandbox_docker_args.py`: `test_temporary_space_is_a_non_executable_tmpfs`, `test_only_one_environment_variable_is_passed` and `test_the_only_mount_is_the_workspace_and_it_is_read_only`.

**On Docker**
- Passing and failing fixtures give the right counts.
- A component over the size cap fails cleanly.
- Nothing is written to `/workspace`, and nothing tries the network.

### 4.5 Frozen front-end acceptance

**Today:** a suite can only be Python unittest files:
- `FILE_NAME` accepts only `test_<name>.py`, parsed with `ast` (`acceptance.py:38`, `81-93`).
- `_DEVELOPER_TEST` strips only Python tests (`:40`).
- Files are staged at the top level (`:108-111`).
- `_begin_components_submit` plans only a `python-unittest` run (`coding.py:451-452`).
- `judge_acceptance` reads unittest output (`:181`).

**Change**
1. Allow front-end suite files tagged with their component, such as `frontend/__acceptance__/at_007.test.tsx`. They are validated without being run: a strict pattern requires titles like `test("AT-007 …")` or `it("AT-007 …")`, within the size limits.
2. Strip the candidate's own `*.test.*` and `*.spec.*` files from that component, and stage the evaluator's files inside it.
3. Run them with the same script in the React image, including only `__acceptance__`, and judge them with `parse_vitest_json`.
4. Deliver only when both the Python and the front-end suites pass.

Freezing and hashing (`freeze` and `thaw`, `:96-105`) do not change. The same-process limit applies in Node too.

**Guards today**
- `test_acceptance.py`: `test_a_frozen_suite_comes_back_unchanged_and_a_changed_copy_is_refused`, `test_checking_a_suite_never_runs_it` and `test_a_suite_that_cannot_prove_its_criteria_is_refused`.
- `test_acceptance_flow.py`: `test_the_coding_provider_and_gpt_never_see_the_suite`, `test_code_that_fails_acceptance_is_never_delivered`, `test_a_changed_copy_of_the_suite_stops_the_run_and_keeps_the_evidence` and `test_an_acceptance_run_with_no_answer_delivers_nothing`.
- `test_api_acceptance.py`: `test_there_is_no_way_to_change_a_suite_after_creation`.

**New tests:** the same five properties for front-end files, plus the stripping of the candidate's front-end tests.

The component boundary itself stays guarded by two tests in `test_fullstack_coding.py`: `test_a_component_task_cannot_change_another_components_files` and `test_a_dependency_the_runtime_does_not_provide_blocks_verification`.

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
| Resource limits | The settings today. After §4.1, the recipe's 1g, 1 CPU, 256 processes and 180 s | The settings today. The recipe asks for 2g, 2 CPUs, 512 processes and 300 s (the decision in §4.1) |
| Scripts | No entry point; the recipe gives the argument list. No install or build step at test time | Only the fixed `run-vitest.mjs`, **not yet written**. Lifecycle scripts never run |

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
5. **Confirm that nothing was pulled.** `docker events --since 15m --until 1s --filter type=image` should print nothing. Every sandbox job is created with `--pull never` and `--network none` (`docker_cli.py:82-85`), and step 2 of §8 needs only `python:3.12-alpine`.
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
| 4 | Choose the Node image, generate the lockfile inside it, write `run-vitest.mjs`, and build `devpilot/verify-react:1` | the Node image (about 50 MB compressed) and npm packages (about 150 to 250 MB unpacked); both estimates | about 300 to 400 MB of image, on D: |
| 5 | Implement §4.1 to §4.5 offline, with their tests. Record the images' IDs, but leave `approved` false; the opt-in Docker tests approve them inside the test only, as `approve_runtimes` does | none | |
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
5. Implement the five sandbox changes (§4); that is code work, reviewed.
6. Run the recipe checks, the component-level run, and the acceptance and recovery checks (§8, steps 6 to 9).
7. Separately, and later: paid model calls ($1.50 cap), the key set by you in `.env`, live coding approval, and OpenCode enabled.
8. Separately: GitHub network access, which needs `codeload.github.com` in the egress allowlist and either a read-only token or a search limit of 4.
