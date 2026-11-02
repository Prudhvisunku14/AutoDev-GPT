# DevPilot AI: competition demonstration

An offline, free and reproducible demonstration of the whole workflow. **No AI model is called, no key is read, no Docker container starts and no network is used.**

## Run it

From the repository root, in PowerShell:

```powershell
. .\scripts\dev-env.ps1
cd backend
& $env:DEVPILOT_PYTHON -m app.demo.competition           # every scenario; reports in data\competition-demo
& $env:DEVPILOT_PYTHON -m app.demo.competition --serve   # the same, then a local dashboard; open the link it prints
```

It takes about 10 seconds. Each scenario writes a structured report (`reports/<scenario>.json` and `.md`), and `competition-demo.json` summarises them. The command exits with 0 only if every scenario ended as expected.

## What is real and what is simulated

| Real, the same code a live run uses | Simulated, and named in every report's `provenance` |
|---|---|
| REST API, GPT's tool loop and its gates, the database | GPT's replies: FakeLLM plays a fixed script per scenario |
| Adaptive tool selection and its eligibility policy | Every coding provider: fixed files from `app/demo/fixtures.py` |
| The attempt lifecycle (a prompt is sent once; a restore only after a confirmed stop), the audit, the disposable copy | Test execution: recorded runs of those exact files (`ReplaySandbox`), not Docker |
| Verification, the frozen acceptance suite, failure classification, recovery, delivery, the report | |

OpenCode is present with its real profile, and the real eligibility policy refuses it offline: no `OPENAI_API_KEY` is configured and its network is not approved. Its launcher is never started.

A replayed result exists only for the exact files it was recorded on. Any other file, including anything a real model would write, gets "needs the Docker sandbox", never a pass. The recordings were made by running the sandbox's own unittest command on repository fixtures, and a unit test repeats that check on every test run.

## The scenarios

| Scenario | What it shows | Result |
|---|---|---|
| Demo 1: success | Plan, selection, code, the provider's tests, the evaluator's acceptance suite (5 of 5), delivery | COMPLETED |
| Demo 2: automatic repair | `subtract()` returns b minus a. The tests fail (`test_ac_002_subtraction`), the failure is classified TEST_FAILURE, a repair is requested and verified | COMPLETED |
| Demo 3: security | The attempt writes `opencode.json`. The audit refuses it (SECURITY_VIOLATION), the copy is restored, the run stops, the user's project is untouched | FAILED, nothing delivered |
| Demo 4a: alternative provider | OpenCode is refused by policy, the first simulated provider is down, the approved simulated backup is chosen | COMPLETED by `simulated_backup` |
| Demo 4b: safe refusal | OpenCode is refused and the only other provider would run on the host. Nothing starts | FAILED with `no_eligible_tool` |
| Acceptance check | The provider's code returns infinity for a division by zero and its own test agrees. The frozen suite fails AT-005 (4 of 5). The repair is told only the criterion, and the second attempt passes 5 of 5 | COMPLETED |

## Independent acceptance

The evaluator gives the acceptance suite with the requirement, before any code exists: criteria AT-001 to AT-005 and a unittest file. It is checked by parsing, never executed by the API, and frozen into each run with its SHA-256. The coding provider never receives it, and GPT learns only which criteria failed. It runs in the sandbox in a separate folder, without the provider's own test files. See `app/verification/acceptance.py`.

**It is not an adversarial security boundary.** The evaluator's tests import the candidate's code into the same Python process, so code written on purpose to subvert the test runner (by patching `unittest`, for example) could fake a pass. It catches honest mistakes and self-graded passes. Isolating each criterion in its own process would be needed against deliberate cheating.

## The dashboard

`--serve` starts a local, read-only dashboard at `http://127.0.0.1:8765/ui/`. The link it prints carries the API token in the URL fragment, which the browser never sends to the server, and the page removes it from the address bar. The page itself holds no data: every request for data needs the token. Its only controls are Refresh, the token form, the run list, the delivered files and "Show the full report".

Checked on 2026-11-02 by automated tests, and visually with the installed Chrome, headless, on loopback only: six runs listed, the selected run highlighted, the banner naming what was simulated, the "replayed, not a Docker execution" note, both acceptance runs (FAILED 4 of 5, then PASSED 5 of 5), and a delivered file opening. No browser was tested by hand.

## What is built, and how far it is verified

**Implemented and offline-tested**
- A natural-language requirement through the REST API, GPT's tool loop and its gates (driven by FakeLLM)
- Adaptive tool selection with its eligibility policy
- Failure classification and recovery
- Independent acceptance: a frozen, hidden evaluator suite with per-criterion evidence
- Safe workspace management: the disposable copy, the audit, guarded attempts, delivery only of verified files
- Final reports, the dashboard, the competition demonstration and the three-configuration evaluation

**Scripted or replayed in the demonstration**
- GPT's replies (FakeLLM), every coding provider (fixed files), and test execution (recorded runs of exact files, `ReplaySandbox`)
- The additional providers (`simulated_primary`, `simulated_backup`, `host_runner`) and the evaluation's tools and outcomes

**Previously verified on real Docker, before Stages 2 to 4 (historical evidence)**
- The sandbox's isolation controls (2026-10-30)
- The offline MVP on real Docker tests, 6 of 6 scenarios (2026-10-31)
- OpenCode 1.19.5 on real containers behind the API filter and the gateway, with a scripted model and no key (2026-10-31 to 2026-11-01)

**Awaiting Docker verification of the Stage 2 to 4 code**
- The 16 opt-in Docker tests under the current code
- The acceptance suite in the real sandbox
- Recovery on real containers: a retry, an unconfirmed stop, a test container that cannot be removed
- Adaptive selection with the real OpenCode launcher

**Real AI functionality not yet tested**
- One live GPT call has been verified (2026-10-29: an analysis, 919 tokens). A full run orchestrated by real GPT, code written by a real model through OpenCode, and the classifier on real failures have not. Each needs paid-call approval.

**Not built**
- Cloud deployment and a remote execution worker: see [DEPLOYMENT.md](DEPLOYMENT.md)
- GitHub-assisted development and Google Drive-to-RAG development: in the product backlog ([PROGRESS.md](PROGRESS.md), "Product backlog")
- n8n integration, stacks other than `python-cli`, and a corpus of real failure logs
