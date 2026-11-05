# Adding a kind of application (a stack)

DevPilot does not decide what to build from keywords. GPT reads the request and chooses a stack from the catalog while planning. DevPilot's code then refuses to code or deliver any stack it cannot verify.

So a new framework is added as data plus a verified toolchain, never as an example prompt. These are the four parts, and all four are needed:

| Part | Where | What it says |
|---|---|---|
| 1. Catalog entry | `backend/app/stacks/catalog.yaml` | id, name, language, frameworks, description, test methods. GPT sees this list, with "can be built and tested now: yes/no" for each entry (`StackCatalog.describe`). |
| 2. Verification recipe | `backend/app/verification/recipes.py` (`RECIPES`) | The exact test command, its folder, its output parser, the packages its image provides, its limits, and how the evaluator's frozen acceptance suite runs. GPT and repository READMEs never supply a command. |
| 3. Runtime image | `backend/sandbox_images/<name>/Dockerfile` | An image with no network at run time, holding exactly the packages the recipe lists. Nothing is installed when a recipe runs. |
| 4. Approval | `Runtime(..., approved=True)` in the recipe, and the image on the sandbox's approved list (`app/sandbox/policy.py`) | Set only by a person, after the image is built and has run its own self-test in the sandbox. |

**Readiness.** A stack is verifiable when all four are in place (`verification.recipes.readiness`). The dashboard and `GET /api/v1/capabilities` show that readiness, and so do the Tools page and GPT's planning prompt.

**What happens without the parts:**
- With no verifiable stack at all, a full build is refused before anything is spent (`POST /runs/{id}/start` answers 409 with every stack's reason).
- A plan may still choose an unverifiable stack. The run then stops before coding, with the reason, and is never reported as built.

## Status on this computer (2026-11-05)

| Stack | Recipe | Image | Verifiable |
|---|---|---|---|
| `python-cli` | `python-unittest` | `python:3.12-alpine` (approved) | yes |
| `static-html` | `app.verification.web` (Chromium) | `devpilot/verify-web:1` (built here; `devpilot setup --web` self-tests and approves it) | once self-tested on this computer |
| `python-fastapi` | `fastapi-pytest` | `devpilot/verify-fastapi:1`: Dockerfile ready, **not built** | no |
| `react-vite` | `react-vitest` | `devpilot/verify-react:1`: Dockerfile ready, **not built** | no |

Building the FastAPI and React images pulls their base images from Docker Hub and their packages from PyPI and npm. That needs the person's approval. Then, after each image's self-test passes, the recipe's `approved` flag and the image digest are set:

```powershell
docker build -t devpilot/verify-fastapi:1 backend\sandbox_images\verify-fastapi
docker build -t devpilot/verify-react:1 backend\sandbox_images\verify-react
```

React + FastAPI projects are planned as two components, each verified by its own recipe. They become buildable when both images are approved.
