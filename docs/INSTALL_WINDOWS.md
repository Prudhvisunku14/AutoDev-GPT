# Installing and using DevPilot AI on Windows

```powershell
git clone https://github.com/Prudhvisunku14/AutoDev-GPT.git
cd AutoDev-GPT
.\setup.cmd
.\devpilot.cmd build
```

## Before you start

| You need | Why | If it is missing |
|---|---|---|
| Windows 10 or 11 | the installer and the encrypted key store are Windows-only | not supported elsewhere yet |
| **Python 3.12** (exactly; 3.13 is not supported) | DevPilot's engine | https://www.python.org/downloads/release/python-3129/, tick "Add python.exe to PATH", or `winget install Python.Python.3.12` |
| **Docker Desktop**, installed and **running** | every build runs the code and its tests in containers | https://www.docker.com/products/docker-desktop/ (WSL2 backend), start it, wait for "running" |
| Git | only to clone and update | https://git-scm.com/download/win |
| **Your own OpenAI API key**, with credit | DevPilot spends *your* credit, only up to the limit you approve | https://platform.openai.com/api-keys |
| Internet, once, for setup | PyPI, the npm registry (OpenCode), GitHub (ripgrep), Docker Hub and the Alpine mirrors (the image build) | the containers a build runs have **no** network |
| About 2 GB free | Python packages, OpenCode (~80 MB), the Docker image (~90 MB), workspaces | free space, or set `DEVPILOT_HOME` to a roomier drive |

`setup.cmd` checks all of this and stops with the fix. It never installs system software for you.

## What `setup.cmd` does

1. Finds Python 3.12 (`py -3.12`, `python3.12` or `python`), checks Git and Docker.
2. Creates `.venv` in the repository folder and installs the **pinned** dependencies (`backend\requirements.lock`) and DevPilot itself.
3. Runs `devpilot setup`, which:
   - asks for your **OpenAI key** (hidden input) and stores it **encrypted with Windows DPAPI**: only your Windows account on this computer can decrypt it. If `OPENAI_API_KEY` is set in the environment it is used instead and nothing is stored;
   - asks for the **model** and your **prices** (USD per 1M input and output tokens, from your own pricing page). They are how DevPilot caps your spending: a build cannot start without them, and prices that are too low make the cap too loose;
   - downloads **ripgrep 15.1.0** and checks the archive and the binary against pinned SHA-256 hashes;
   - downloads **OpenCode 1.19.5** and checks it against the pinned SHA-512 from the npm registry (the downloader is `backend\scripts\fetch_opencode.py`, unchanged);
   - builds the Docker **runtime image** and **audits it** (below), then records it as approved for this computer;
   - runs `devpilot doctor`.

It makes **no OpenAI request**. Run it again any time; finished steps are skipped (`--rebuild` rebuilds and re-audits the image).

## Using it

```powershell
.\devpilot.cmd build
```

You are asked for the application, shown the stack in use, and shown the spending approval:

```
Spending approval
  Model: gpt-6.1-sol  ($2.0 in / $10.0 out per 1M tokens, your own prices)
  This build may spend at most $0.50, GPT planning and OpenCode coding together.
  Its real cost cannot be known beforehand: it depends on the plan and on how many repairs the tests need.
  What is certain is the limit: before every model request the most it could cost is worked out, and a request that could
  take the build past $0.50 is not sent. ...
Type "approve 0.50" to start the build, or anything else to cancel:
```

Only that typed phrase (or `--approve-spend 0.50`, with the exact figure) starts anything. Then DevPilot:

1. checks your key can see the model (a free request, no tokens) and test-starts the real containers, then stops them;
2. starts a guard process that stops the containers if DevPilot is killed, and asks Windows not to sleep;
3. plans with GPT, writes the code with OpenCode, tests it in the sandbox, repairs failures within strict limits, and verifies the result;
4. if the generated tests pass in Docker, saves `devpilot-output\<name>-<id>\` and `devpilot-output\<name>-<id>.zip`.

GPT may ask you a question mid-build; answer it in the terminal (Enter alone stops the build). **Ctrl-C** cancels the run and stops the containers.

### What the ZIP holds

The source, the tests, `DEVPILOT-HOW-TO-RUN.txt`, and `devpilot-verification\` (the report, and `MANIFEST.txt` listing what was included and what was left out and why). **Never included:** `.env` files, keys and certificates, version-control and tool folders, caches, local databases, logs, and any file containing your key or a credential. A build whose tests did not pass gets **no ZIP**.

"Verified" means the tests the project wrote for itself passed in the no-network Docker sandbox; the ZIP's manifest says so. DevPilot cannot add an independent acceptance suite to a free-form request. It is not a guarantee that the program does what you meant.

## Where things live

| What | Where (default) |
|---|---|
| Your settings (model, prices, limit) | `%LOCALAPPDATA%\DevPilot\config.toml` (no secrets) |
| Your key, encrypted | `%LOCALAPPDATA%\DevPilot\credentials.bin` |
| OpenCode and ripgrep downloads | `%LOCALAPPDATA%\DevPilot\tools\` |
| The image approval record | `%LOCALAPPDATA%\DevPilot\approved-runtime.json` |
| Database, workspaces, run copies, guard state | `%LOCALAPPDATA%\DevPilot\{data,workspaces,work,guard}` |
| Results | `.\devpilot-output\` in the folder you ran the command from (`--output` changes it) |

Set `DEVPILOT_HOME` (or pass `--home`) to keep all of this elsewhere, for example on a roomier drive. Nothing is written to the repository except `.venv` and, if you choose, `devpilot-output`; all of them are in `.gitignore`.

## How the Docker image is verified (and why it is not a fixed ID)

OpenCode runs in a container from the **runtime image**: `python:3.12-alpine`, the two C++ libraries the OpenCode binary needs, and ripgrep as the last layer, running as the unprivileged user `65534:65534`. Containers started from it have no network, a read-only root filesystem, no capabilities and strict limits; those are applied at start by the engine, not by the image.

**Why a fixed image ID cannot be the check.** DevPilot used to pin the image's ID in the code (`APPROVED_RUNTIME_IMAGES`). That cannot work on anyone else's computer, and it already failed on the original one:

- Under Docker Desktop's *containerd image store* the ID is the digest of an OCI **index** that includes a BuildKit *provenance record* (build time, invocation ID). Rebuilding **identical** content gives a **new** ID.
- A locally built image's layers also differ between computers, because `apk add` installs whatever package versions are current.

**What is checked instead.** `devpilot setup` builds the image from `backend\app\cli\Dockerfile.runtime` and `docker save`s it, then audits the *contents* offline (`backend\app\cli\image_audit.py`; nothing is run):

- the image runs as `65534:65534` and sets no entrypoint, ports or volumes; it is linux/amd64;
- every layer matches the hash its config records;
- the **last layer holds exactly one file**, `usr/local/bin/rg`, root-owned, mode 0755, not setuid, and its SHA-256 is the pinned ripgrep's, so OpenCode never tries to download one;
- the layers beneath provide `libstdc++` and `libgcc_s`.

Only an image that passes is recorded as approved, by its **exact ID**. Each start still requires the image's ID to equal the recorded one: an image that is rebuilt or replaced has a different ID and is refused until `setup` audits it again. The OpenCode binary is mounted into the container read-only after its hash is re-checked against the pinned value, and the built-in pin in the code is unchanged and still accepted.

## `devpilot doctor`

Checks, locally and without contacting OpenAI: Python 3.12, dependencies, Git, Docker installed and running, the sandbox image, the audited runtime image, the OpenCode binary, your key (present, plausible; never printed), model and prices, the network policy (OpenAI and the local filter only), the engine's own readiness check, leftover containers, an unconfirmed container stop, disk space, mains power and port 4096. Each failure says how to fix it.

## Troubleshooting

| Message | Fix |
|---|---|
| `Python 3.12 was not found` | install 3.12 (table above), open a **new** terminal |
| `Docker is installed but not running` | start Docker Desktop and wait for it to say it is running |
| `docker build failed … apk fetch failed` | setup needs the internet once; check proxy/VPN, run `.\setup.cmd` again |
| `… does not match its pinned SHA-256, so it was discarded` | the download was altered or corrupted; run setup again, and if it repeats, do not proceed |
| `the image … is not the one that was audited` | the image was rebuilt or replaced; run `.\setup.cmd --rebuild` |
| `OpenAI did not accept the API key` | `.\devpilot.cmd forget-key`, then `.\setup.cmd` with a valid key |
| `the key cannot see the model` | the model name or your account's access; edit `model` in `config.toml` or rerun setup with `--model` |
| `a crashed build left …` (warning) | `docker rm -f` the listed containers and `docker network rm` the `devpilot-*` networks |
| `unconfirmed container stop` | an earlier build could not confirm a container stopped: check `docker ps -a`, then delete the marker folder named |
| Windows asks about running scripts | `setup.cmd` runs `setup.ps1` with `-ExecutionPolicy Bypass` for that one process only; nothing is changed system-wide |

## Limits

- **Python command-line applications only.** React, websites, FastAPI and full-stack generation are declined: their verification runtimes have not passed a real end-to-end test, and a Python-only success is not evidence for them.
- The first generation from a fresh install is not yet proven on every machine; what is proven is listed in `docs/PROGRESS.md`.
- Costs are computed from the prices *you* entered; OpenAI's invoice is the authority.
- Windows only for now (the encrypted key store, the guard process and the installer).
