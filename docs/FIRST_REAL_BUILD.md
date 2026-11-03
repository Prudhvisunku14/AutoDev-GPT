# First real build: readiness and procedure

**Status (2026-11-03): prepared and offline-tested. Nothing in this document has run for real.** No Docker command, container, OpenCode request or paid build call was made. The only real model use so far is the two planning requests of `app/plan_cli.py` (about $0.039).

## 1. What "real" means here

A run is a genuine success only if all of these hold. `scripts/live_build.py` checks each one and prints "Not a success" otherwise:
- real GPT orchestrates (the report's `gpt` is the OpenAI Responses API, not FakeLLM);
- real OpenCode wrote the files, through the gateway, with at least one real model request in its ledger;
- the tests ran in real Docker (`DockerSandbox`, not the replay sandbox);
- the evaluator's frozen acceptance suite passed. The model never sees it;
- the report's `provenance.simulated` is empty;
- the cost is within the cap, and no container or network was left behind.

A run that stops at the spending cap, or on any failure, is reported as exactly that.

## 2. The build path that already exists

Request, then `submit_analysis`, `submit_plan` and `submit_criteria` (GPT, gated). Then `generate_code`: Python picks the provider, OpenCode writes into a disposable copy through the filter and gateway containers, and the audit refuses writes outside the task. Then `run_sandbox_command` and `submit_implementation`: Docker runs the tests, and the acceptance suite runs on its own. Failures go through classification and recovery. `finish` writes the real project only after a pass.

Every piece was exercised with a scripted model on real containers (2026-10-30 to 2026-11-01). **No real model has ever driven OpenCode, the gateway or the filter.** Their first real use is the run below.

## 3. The connections that were missing, and what was done

| Gap | Resolution |
|---|---|
| The only live script was fixed to the calculator and a $1.50 cap, and it never attached an acceptance suite | `scripts/live_demo.py` now takes an `Experiment` (request, limits, suite, extra checks, a confirmation, a judge). Its defaults are the calculator, unchanged. The suite is sent when the project is created, and a suite the API refuses stops the run |
| No way to run a $0.50 build with a person's separate go-ahead | `scripts/live_build.py`: $0.50 limits, then a typed `yes` after the checks and before anything is started |
| Nobody had written an independent suite for a small Python application | `live_build.py` holds eight criteria (AT-001 to AT-008). They were checked against a correct tracker (all pass) and eight deliberate breaks (each caught by the criterion it targets). The request names the interface the tests use, and none of the tests |
| The laptop's Docker targets were only a document | `live_build.py` checks C: free, available RAM and commit before anything starts |
| The cap was per run, so running the script twice could have spent $1.00 | The spending ledger above, found in the pre-commit review |

Not changed: the sandbox, the launcher, the gateway, the filter, the egress allowlist and the held-back line. There is still no second coding agent.

## 4. The spending cap ($0.50)

The cap is `RUN_BUDGET_USD=0.50`, shared by GPT and OpenCode through `run.cost_usd`, and it is strict:
- before each GPT request the most it could cost is saved, and a request that could pass the budget is not sent (`orchestrator/budget.py`);
- an OpenCode attempt is given the smaller of `GATEWAY_MAX_USD_PER_TASK` ($0.30) and what the run has left. The gateway refuses a request whose worst case passes it, and the selector refuses OpenCode when less than its floor remains ($0.0744 at these limits);
- the SDK makes no retries, and a run that pauses is cancelled, so nothing carries on unattended;
- a call whose answer never arrives is charged at its worst case. For OpenCode that is the gateway's own rule: a request's reservation is written to its ledger before anything is forwarded, an answer whose usage cannot be read keeps the whole reservation, a gateway that dies leaves the reservation charged, and an attempt whose ledger cannot be read is charged its whole allowance (`attempt_cost`);
- the $0.50 is a **total**, not a per-run figure. A ledger (`D:\DevPilotCache\live-demo\build-ledger.jsonl`) counts a run at its whole cap from the moment it starts until it is settled, so a crash or a closed terminal cannot hide it, and a run with an unknown cost counts at the cap. A second paid run is refused once the first has used anything. A run that stopped before any model call (the key cannot see the model, the dry launch failed) settles at zero and may be repeated.

Worst case of one request: GPT $0.040 (3,000 output tokens), OpenCode $0.0744 (about 17,000 input tokens and 4,000 output tokens).

| Limit | Value |
|---|---|
| Total | $0.50 |
| Per OpenCode attempt | $0.30, 15 requests, 80,000 tokens, 4,000 output tokens a request |
| GPT | 12 turns, 12 tool calls, 3,000 output tokens a turn, 150,000 tokens |
| Recovery | 1 repair, 2 attempts a task (the dollars bind first) |
| Time | 1,200 s |

**Billing uncertainty:** costs are computed at $2 in / $10 out per million tokens (the Standard tier, read 2026-10-31). OpenAI's usage page is the authority. The script prints the gateway's ledger so the two can be compared.

**Will $0.50 be enough?** Not known. The planning requests cost $0.013 and $0.026. The GPT part of a build is about 7 turns, each re-billing the earlier context, so roughly $0.12 to $0.25. An OpenCode request has not been measured with a real model; at the measured size it is about $0.02 to $0.04, and a small task needs perhaps 5 to 10. So the total is plausibly $0.25 to $0.50. If it stops at the cap, the run is reported as stopped, the real project is untouched, and the money is spent. A first attempt that spends its $0.30 leaves too little for a repair.

## 5. Readiness, item by item

| Item | State |
|---|---|
| Approved runtime image | `devpilot/opencode-runtime:1.19.5`, `sha256:a7753fe4…`, in `APPROVED_RUNTIME_IMAGES`. Built 2026-10-31. **Whether it is still present is unknown until Docker runs** |
| OpenCode binary | `D:\DevPilotCache\opencode\1.19.5\`, verified against its manifest by the probe on 2026-11-03 |
| Test runtime | `python:3.12-alpine`, approved. `python-cli` is the only stack with an approved verification runtime |
| Provider eligibility | OpenCode claims `python-cli` only. At the $0.50 limits the selector chooses it while at least $0.0744 remains |
| Launcher | Refuses until `CODING_LIVE_APPROVED` is true. Only `--approve-spend 0.50` sets it. With the live approval, a fake key and the working-tree allowlist, it has no objection |
| Model gateway | Holds the key, one fixed destination (`api.openai.com:443`, verified TLS). Never run against OpenAI |
| Network | Four Docker networks, created per attempt and removed afterwards (`docs/PROGRESS.md`, "First real demonstration"). The allowlist has `opencode: api.openai.com`. The backend-to-server line `127.0.0.1:4096` is in the working tree, not in a commit, and without it the preflight fails |
| Workspace | OpenCode writes only into a disposable copy under `D:\DevPilotCache\work`. The audit refuses changes outside the task. The real project is written only by `finish`, after a pass |
| `.env` | Needs only `OPENAI_API_KEY`. `live_build.py` sets the model, prices and every limit itself, whatever `.env` says |
| Approvals | The calling script gives the one approval the launcher needs. There is no per-tool approval inside a build |

## 6. Docker preflight (read-only, 2026-11-03)

| Target | Measured | Result |
|---|---|---|
| C: free at least 10 GB | 11.6 GB | Pass |
| RAM available at least 4 GB | 4.36 GB, then 3.70 GB a few minutes later | **Fail at the last reading** (15.65 GB installed) |
| Commit at most 70% | 60%, then 63% | Pass |
| AC power | On AC, battery 86% | Pass |
| Docker | Installed at `D:\DockerDesktop`, not running. WSL's `docker-desktop` distro stopped | Not started |
| Docker data | `D:\DockerData\disk\docker_data.vhdx`, 8.42 GB, last written 2026-11-02. D: has 128.7 GB free | On D: |
| `.wslconfig` | None, so Docker's VM may grow to about half the RAM | Risk |
| Images | Cannot be listed while the engine is off. Last recorded: `python:3.12-alpine`, `devpilot/opencode-check:1.19.5`, `devpilot/opencode-runtime:1.19.5`. No `devpilot/verify-*` image | Unverified |

RAM and commit move with whatever is open. Close browsers and spare editor windows, and re-check just before starting. C: has been as low as 2.2 GB earlier today, so a single reading proves little.

## 7. Procedure: the first real Python application

Each step needs your go-ahead. Stop on the first failure.

1. **Free memory, then check.** Close heavy apps. `live_build.py` repeats these checks, but check first:
   ```powershell
   Get-PSDrive C,D | Select Name,@{n='FreeGB';e={[math]::Round($_.Free/1GB,2)}}
   (Get-Counter '\Memory\% Committed Bytes In Use','\Memory\Available MBytes').CounterSamples | Select Path,CookedValue
   ```
   Proceed only with C: at least 10 GB, RAM at least 4 GB, commit at most 70% and AC power. Optionally cap Docker's VM first (your decision; a Windows setting I will not change): a `.wslconfig` with `[wsl2]` and `memory=4GB`.
2. **Start Docker Desktop** and run the health check, `docs/DOCKER_READINESS.md` §7 steps 2 to 5. Needs no network from DevPilot. Confirm the three images are present, and that nothing was pulled.
3. **Run the 16 opt-in Docker tests** (9 in `test_sandbox_docker.py`, 7 in `test_mvp_end_to_end.py`), from `backend`:
   ```powershell
   $env:RUN_DOCKER_TESTS = 'true'
   python -m pytest tests/integration/test_sandbox_docker.py tests/integration/test_mvp_end_to_end.py -q -p no:cacheprovider
   Remove-Item Env:RUN_DOCKER_TESTS
   ```
   Expect 16 passed. They create containers from `python:3.12-alpine` only, with `--pull never`.
4. **Preflight of the build** (loads the key into the process; contacts nothing external, spends nothing):
   ```powershell
   python scripts\live_build.py
   ```
   Every line must read `ok`, including that the images exist and that no DevPilot container or network is left. The dry launch of the real containers happens only in the paid step, before the first model call. Run it from `backend` with the venv active.
5. **The paid run**: `python scripts\live_build.py --approve-spend 0.50`. It shows the exact limits and waits for a typed `yes`. Then: keep-awake and the guard, the free model-access check, a dry launch of the real containers, and the run. Don't close the terminal.
6. **After it:**
   - The script prints the tasks, the provider's tests, the independent acceptance verdict, recovery steps and the delivered files, then the verdict. Evidence is under `D:\DevPilotCache\live-demo\<time>`.
   - Compare the gateway's ledger and the run's cost with OpenAI's usage page.
   - Check that no container or network remains: `docker ps -a --filter label=devpilot.sandbox=true`.
   - Run the delivered tests yourself in Docker if you want a second look.
7. **Stop Docker** (`docs/DOCKER_READINESS.md` §7 step 6), and record C:, RAM and commit again.

**If it stops short:** read the outcome (`budget_limit`, `verification_unavailable`, an acceptance failure). Nothing is retried automatically. Raising the cap is your decision, and the script accepts only the figure compiled into it.

## 8. Procedure: the full FastAPI portal

Stop after each step if it fails.

1. **The Python tracker above must have succeeded first.** It proves the real model, OpenCode, the gateway and Docker together.
2. **Decide the image's contents** (see §9: the plan needs packages the image lacks).
3. **Build `devpilot/verify-fastapi:1`.** This downloads about 15 to 30 MB of wheels and needs your approval for the network. Resolve the hashes, build, confirm the musllinux wheels exist (pydantic_core is the risk; the fallback is `python:3.12-slim`), and record the image ID.
4. **Run the recipe on fixtures with passing and failing tests** (`docs/DOCKER_READINESS.md` §8 steps 6 to 9): the parser, a component-level run, acceptance, a failing test, a missing dependency and a timeout. Only then approve the image's ID in code, in a reviewed commit.
5. **Write the portal's independent acceptance suite** before the run, from the accepted requirements. Authentication, entity management, attendance and the percentage arithmetic can all be tested through FastAPI's TestClient.
6. **Prove OpenCode on `python-fastapi` with a small task first** (the weather back end, `app/demo/weather.py`, $0.75 cap: `docs/DOCKER_READINESS.md` §9). Only a real success may add the stack to OpenCode's profile.
7. **Then the portal**, with its own approved budget. Nine tasks and 32 requirements will cost far more than $0.50.
8. **React stays off** until `devpilot/verify-react:1` is built and verified. The accepted plan is server-rendered and does not need it.

## 9. The college attendance portal: readiness

**Can the existing planning report be reused?** No, and a new build run is required.
- That run was closed (`CANCELLED`) on purpose, and a closed run cannot be resumed.
- Its plan lives in that run. Nothing imports a saved plan into a new run, and feeding one back as if GPT had said it would be a scripted response, which this work must not count as real.
- The report and plan are good as a reference for what GPT proposed. A new run would re-spend the planning, about $0.04, plus acceptance criteria.
- The independent suite must exist before that run is created, because it is frozen at project creation and cannot be added later.

**What the 9-task plan needs, and where it stands** (`python-fastapi`, one component, 32 requirements):

| Needed | Status |
|---|---|
| FastAPI app, routes, tests with TestClient | The image provides FastAPI, Starlette, httpx and pytest |
| SQLite persistence with foreign keys and migrations | Standard library `sqlite3`: available |
| Authentication: password hashing, expiring sessions, CSRF, login throttling | No hashing or session package in the image. Possible with `hashlib.scrypt`, `hmac` and `secrets`, but GPT must be told, because it will otherwise reach for bcrypt, passlib or itsdangerous |
| Server-rendered responsive pages | **Jinja2 and `python-multipart` are not in the image.** FastAPI's `Form(...)` needs python-multipart, and templates usually need Jinja2 |
| Students, faculty, departments, courses, attendance, reports | Plain Python and SQL |
| Unit and acceptance tests | pytest in the image, and the frozen suite (unittest) |
| Browser checks (desktop and mobile) | **Cannot run.** The plan itself says not to claim them. They stay a manual checklist |

**Blocker to resolve first:** the dependency gate refuses a project that declares anything the image does not provide, and that includes a missing `requirements.txt`. Nothing tells GPT or OpenCode which packages exist, so a plan that uses Jinja2 or python-multipart would stop at `verification_unavailable`. Either put those packages in the image (more wheels, reviewed), or tell the planner the package list and require standard-library templating and form parsing. This is a design decision for you.

**Scale:** nine tasks, each its own OpenCode attempt, with repairs. At the measured sizes it is likely several dollars, not cents. It needs its own budget and approval after the tracker succeeds.
