# First real build: readiness and procedure

**Status (2026-11-03): the first real build ran once and FAILED; §10 says why and what changed. The sections below were written before it ran** and describe the intended procedure. The fixes in §10 are offline-tested only. No second paid run has been made, and none is authorized.

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
| Per OpenCode attempt | $0.30, 15 requests, 200,000 tokens, 4,000 output tokens a request. The token figure was 80,000 in the first run; see §10 |
| GPT | 12 turns, 12 tool calls, 3,000 output tokens a turn. The run's token limit is 300,000 (it was 150,000), above what the dollars can buy |
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

## 10. The first real run (2026-11-03): why it failed, and what changed

Run `dcb942e8…`, evidence in `D:\DevPilotCache\live-demo\20261103T073422Z`. Real GPT, real OpenCode, real Docker. It ended `FAILED` (`coding_failed`), nothing was delivered, the acceptance suite was `NOT_RUN`, no container or network was left behind. The run's cost was counted at $0.3798 (ledger settled; $0.12 of the $0.50 remains counted as unspent, and the ledger was not touched).

### What the evidence shows

| | |
|---|---|
| GPT | 7 turns, $0.082 (25,390 input and 3,103 output tokens): analysis, plan (2 tasks), 14 criteria, 4 `generate_code` calls |
| OpenCode | 4 attempts, **16 requests, every one answered 200 by OpenAI**, $0.298 at the configured prices (86,354 input tokens, 59,049 of them cached, and 12,529 output) |
| Attempts | Task 1: attempt 1 failed, attempt 2 **kept** (`attendance.py`, 1,596 bytes). Task 2: attempts 3 and 4 both failed. The limit of 2 attempts a task then ended the run |
| Rolled back | Attempts 1, 3 and 4, each verified restored (`restore_verified`). Each had written its files before it failed. They were $0.110, $0.098 and $0.066: **$0.274 of the $0.298 OpenCode spend produced nothing** |

### Confirmed cause: DevPilot's own gateway, not OpenAI

All 18 refusals are in the gateways' audits as `forwarded: false`, `status: 429`, `reason: "the token budget is used up"`: they never left the machine. Nothing in the evidence shows OpenAI answering a 429 at all.

The chain:
1. The gateway reserves each request at its **size in bytes** as tokens (a sound upper bound). Real requests were 4,200 to 9,100 tokens (5,400 on average); their reservations were 25,000 to 49,500 (32,000 on average), about six times more.
2. The per-attempt token limit was 80,000 and the run's 150,000. Both were my choices in `live_build.py`, tighter than the 100,000 of the earlier proposal, whose own text said an attempt "gets about 6 to 12 requests". The last attempt began with about 44,000 left.
3. After 3 to 6 requests `settled tokens + the next reservation` passed the limit, and the gateway refused, as designed. Only $0.10 to $0.11 of the $0.30 attempt allowance had been spent. The dollars never bound.
4. The gateway answered **429**, with the generic text "Too Many Requests" (the reason was only in its audit). OpenCode treats a 429 as temporary and retried six times, about 2, 5, 8, 20 and 33 seconds apart (roughly 70 s idle each), then reported `APIError: Too Many Requests`.
5. Recovery classified that as `TOOL_UNAVAILABLE` and chose RETRY. The retry met the same limit.

Not rate limiting by OpenAI (no 429 from OpenAI), not exhausted credit, not an account or organization limit. The OpenAI usage page was not consulted, so what OpenAI billed is not known; the ledgers are DevPilot's own figures.

### Why the tokens were so high

- **Four attempts for two tasks.** 90% of the input tokens (77,300 of 86,354) were in the three rolled-back attempts. Only attempt 2 (9,000) was kept.
- **The finished work was thrown away at the last step.** In each failed attempt the files were written, and the refusal hit a later request. The rollback is correct, since unverified output is never kept, but it made the whole attempt waste.
- **Every request resends the whole conversation.** The gateway forbids `previous_response_id`, so a request carries OpenCode's system prompt and tool schemas (about 4,200 tokens) plus everything so far. 68% of it was served from OpenAI's prompt cache. The prices here charge cached tokens at the full $2 per million, so the estimate is higher than the bill should be: the gateway's $0.298 would be about $0.186 with cached input at $0.10. That is an estimate, not a bill.
- **Tool bookkeeping costs requests.** Each round of OpenCode tool calls is one request. Attempt 1 made seven calls in six requests (three `todowrite`, two `apply_patch`, and two `read` together). The `todowrite` calls only keep OpenCode's own checklist.
- **Two tasks for a one-file program.** The plan split implementation from tests, so the second session paid the base prompt again and read the first session's file.
- **GPT** was small: $0.082, 7 turns.

### What changed (all offline-tested; none of it has run against OpenAI or Docker)

| Change | Where |
|---|---|
| A used-up gateway budget answers **402** with an OpenAI-shaped error, code `devpilot_budget_used_up`, and the reason in the message. The pinned engine does not retry it (below) | `edge/gateway_policy.py` (`budget_refusal`), `edge/filter_policy.py`, `edge/filter_server.py` |
| The attempt's audit is read after it ends. A failed attempt with budget refusals, or with OpenAI 429s, becomes `LIMIT` (resources), whose recovery options never include starting the same tool again: the run stops with the facts, and a person decides | `app/coding/launcher.py` (`read_refusals`), `app/tools/adapters/coding.py` |
| An attempt is not started with room for fewer than 4 requests (72,808 tokens at 4,000 output): it would spend money to be rolled back. The fourth attempt of the run would have been refused up front | `app/selection/catalog.py` |
| The experiment's token limits are no tighter than its dollars: 200,000 an attempt and 300,000 a run | `scripts/live_build.py` |
| OpenAI's 429 for the coding engine: the gateway **does not retry by itself** (the engine already does, and two layers would multiply). A rate limit is passed on, with OpenAI's `Retry-After` forwarded when it is 20 s or less, so the engine waits exactly that. A hard quota (`insufficient_quota`, `billing_hard_limit_reached`, `billing_not_active`, `account_deactivated`, `access_terminated`), or a pause longer than 20 s, ends the attempt with a 402 whose body DevPilot wrote | `edge/gateway_policy.py` (`answer_to_429`), `edge/gateway_server.py` |
| A rejected 429 is charged zero (it was never processed), and still counts as a request. Everything else that fails stays charged in full, as before | `edge/gateway_server.py` |
| Every non-200 answer from OpenAI is recorded: status, error code and type, request ID, `Retry-After`, the `x-ratelimit-*` headers (an allow-list: never an authorization or cookie header, never the error message) | `edge/gateway_server.py` |
| GPT's own client: a 429 with code `rate_limit_exceeded` is waited out, at most 2 retries and 40 s, honouring `Retry-After` (one over 20 s pauses the run instead); hard-quota codes are never retried; the SDK's own retries stay off. The facts reach the run's `llm.error` event and the report | `app/orchestrator/llm_client.py`, `app/orchestrator/loop.py` |
| Fewer requests: the experiment's request asks for one coding task; every brief says that each step is a paid request (write each file whole, read only what is needed, a todo list only for a long task) | `scripts/live_build.py`, `app/coding/brief.py` |

### The pinned engine's retry behaviour: read from its binary, ported and tested

The OpenCode 1.19.5 binary was read as bytes (never run) at the offsets the tests name. An HTTP error is retried when the AI SDK marks it retryable (status 408, 409, 429 or 5xx), **or** when its message **or its response body** matches one of seven patterns (they include `429|500|502|503|504|524`, `rate limit`, `too many requests`, `resource exhausted`, `terminated`). It is retried up to **5 times**, waiting 2 s, doubling, with 25% jitter, or exactly what `Retry-After` says, **with no upper limit**.

- **This reproduces the first run exactly:** 1 try and 5 retries an attempt, 2.2, 5.0, 8.1, 19.7 and 32.6 s apart, each inside the ported formula's range.
- **What follows from it:**
  - A 402 is not retried, provided its body contains none of the patterns. DevPilot's three stop bodies are checked against them.
  - Passing on OpenAI's own body for a hard quota could restart the retries: `access_terminated` matches `terminated`. So the gateway writes its own body, and keeps OpenAI's code in its audit.
  - A forwarded `Retry-After` would be honoured without limit, so only pauses of 20 s or less are forwarded. Without one, the engine's five waits total at most about 78 s, inside an attempt's 300 s. Each retry is a new request, counted against the attempt's 15.
- **Tests:** `test_rate_limits.py` ports the rule (`opencode_retries`, `opencode_delay_ms`). `test_the_port_matches_the_pinned_binary` checks the copied text against the binary itself, and it ran here (it skips where the binary is absent). The same rule is then applied to every answer the gateway gives.
- **Residual risk:** this is a port of the decision, not a run of the engine. Error paths other than an HTTP error (an error inside a stream, say) use a message check that includes the word "exhausted". No DevPilot answer contains it.

### Token accounting, reviewed

- **Settlement is the real usage.** A request's reservation (its body in bytes plus 512, plus the output cap) is a hold while it runs, and is replaced by OpenAI's reported usage when the answer arrives. Replaying the first attempt's six requests settles to 39,391 tokens and $0.1095, the figures in its real ledger, not the 209,000 tokens reserved.
- **Unknown usage stays charged at the whole reservation,** in tokens and dollars: a lost answer, a gateway that dies (its reservation is on disk before anything is forwarded), an unreadable ledger (the whole allowance).
- **Bytes are used only where a guarantee needs them,** that is, to decide whether a request could pass a limit before it is sent. A token never covers less than one byte, so that bound cannot be beaten, and it is about six times the real figure. A tighter, "realistic" bound would no longer be a bound, and the dollar cap relies on it, so it was not changed.
- **The dollar cap is never bypassed:** GPT and OpenCode draw on one `run.cost_usd`, each attempt gets the smaller of $0.30 and what is left, every request is admitted only if its worst case fits, a retry is a new admission, a rejected 429 is zero only because it was never processed, and recovery's own decisions start nothing that skips admission.
- **Cached tokens** are reported inside `input_tokens` and charged at the full configured $2 per million. The repository's 2026-10-31 reading of the price page says cached input is $0.10; that could not be checked offline and no cached price is configured, so the figure stays an overestimate (about $0.298 against $0.186 for the first run's OpenCode part), never an underestimate.
- **Can 200,000 and 300,000 still refuse a legitimate request too early?** Modelled on the first run's growth (each request about 4,300 bytes longer, real input a sixth of the bytes): at $0.30 an attempt, **the dollars refuse first, after 8 requests**; without the dollar limit, 200,000 tokens would allow 14. The attempts that got anywhere needed 2 to 7. At the first run's 80,000, the tokens refused first, after 6, while the dollars had room. So the token limits no longer bind before the money, and no limit was raised beyond what the dollars imply. A task needing more than about 8 requests in one attempt would stop at its $0.30; that is the spending limit doing its job, not a token limit misfiring.

### Request reduction, evaluated

- **One task for a small program: done,** in this experiment's request only. The first run's plan split module and tests, so the second session paid OpenCode's base prompt again (about 4,200 tokens) and reread the first file, and each task could fail and be retried separately. The frozen suite and the plan gate are unchanged. GPT may still split it: it is a request, not a rule.
- **Hiding `todowrite`: not done.** DevPilot can hide a tool, as it hides `bash`. But one of the binary's system prompts tells the model to use the TodoWrite tools "VERY frequently" regardless, so a hidden tool would likely become `invalid` calls, each still a request. That cannot be checked offline. The brief's new line asks for a todo list only on long tasks instead. That removes no tool, so nothing can fail because of it.
- **Repeated context: left as it is.** Each request resends the conversation because the gateway refuses `previous_response_id` (it reads state stored at OpenAI, outside DevPilot's control). OpenAI's prompt cache served 68% of it. The brief (about 8,300 characters) holds the task, its requirements and criteria and the rules. All of it is needed, so it was not trimmed.

### Assumptions and what is still unknown

- A 429 that OpenAI rejects is not billed, so it is charged zero. This is OpenAI's documented behaviour as I know it, and it has not been confirmed on this account. Anything else that fails is still charged at its whole reservation.
- The engine's retry decision is ported from its binary and tested, but no real engine has been given these answers.
- Neither the gateway's nor the GPT client's 429 handling has met a real OpenAI 429.
- Whether GPT follows the one-task request, and whether the model follows the brief's line about requests, can only be seen in a real run.

### Not done, and why

- **Task 1's code cannot be reviewed.** Attempt 2's `attendance.py` was kept only in the run's disposable copy, which was removed when the run failed. The database holds its path and size (1,596 bytes), never its content. The OpenCode session is not stored. Nothing on disk can be reviewed, and even if it could, it was never tested or accepted, so it must not be reused as delivered code.
- **Not built:** counting cached tokens at a cached price (it needs a verified rate); keeping a failed attempt's files in the evidence folder for review (they would never be delivered, but it changes the restore path and deserves its own review).
