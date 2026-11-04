# Your GitHub repositories and websites

Status as of 2026-11-04. Each claim says how it was checked: **live** (real GitHub, real Docker, real npm registry, real
Chromium; never OpenAI), **offline** (automated tests with fakes), or **not done**.

## Capability status

| Capability | Status |
|---|---|
| Any GitHub URL: validation and normalisation (`/tree/<branch>`, `/commit/<sha>`, `.git`, SSH form; `--branch`, `--commit`) | **offline** tests, **live** on public repositories |
| Public repositories: metadata, commit resolution, file list, manifests, README, archive download | **live** |
| Private repositories with your token | **offline** only. The token handling is tested (header only to `api.github.com`, never on redirects, never in messages), but no private repository has been read: none was available |
| Inaccessible repository → authorization request, not a guess | **live**: `https://github.com/LTID-rep/ltid-ai-competition-website` answers 404 without a token; DevPilot reports "may be private … give a token", and assumes nothing about its contents |
| Framework, stack, pages, routes, components and assets detection | **offline** tests; **live** on a public React + Vite + TypeScript + Tailwind repository |
| Website verifier: static HTML/CSS/JS | **live**: self-test sites pass, a broken one fails at the right stage |
| Website verifier: React + Vite + TypeScript | **live**: self-test app passes, one with a type error fails at `typecheck`; a real public repository (`RoyRao2333/template-vite-react-ts-tailwind`, Vite 7) passed every stage |
| Dependency install through a registry-only proxy | **live**: npm reached only `registry.npmjs.org:443`; direct internet from the install network failed (no DNS, network unreachable); the proxy refused github.com, port 80, a link-local address and a plain GET, each audited |
| `devpilot build` for a **Python CLI starting from your repository** (import of the pinned commit, then the existing engine) | **offline** (scripted GPT, simulated coding engine). Not run against the real OpenAI API |
| `devpilot build` for a **website** (static, React + Vite) | **not available**: declined with the reason. See *What remains* |
| React + FastAPI full-stack | **not available**: detected and declined. Its React half can be checked with `verify-site --component <folder>` |
| Next.js, Vue, Angular, Svelte, Create React App, mobile apps | detected and declined by name |

The final acceptance test in the request ("Build the Northstar Studio website using …" accepted by `devpilot build` and a
verified website ZIP produced) **has not been met**. Two things stand in the way, and neither is hidden:

1. **The repository is not readable without your authorization.** GitHub answers 404 to an unauthenticated request. Run
   `.\devpilot.cmd setup --github-token` with a fine-grained token that can read it (Contents: Read-only), then
   `.\devpilot.cmd inspect https://github.com/LTID-rep/ltid-ai-competition-website` to see what it is.
2. **Website generation is not wired into the build engine** (below), so `devpilot build` declines website requests with
   that reason, before any approval or spending.

## Commands (none makes an OpenAI request)

```powershell
.\devpilot.cmd setup --github-token                      # private repositories (typed hidden; stored with Windows DPAPI)
.\devpilot.cmd inspect https://github.com/<owner>/<repo> [--branch <b> | --commit <sha>]
.\devpilot.cmd setup --web [--rebuild]                   # build devpilot/verify-web, self-test it, approve it on this computer
.\devpilot.cmd verify-site <GitHub URL or folder> [--component <folder>] [--output <dir>]
.\devpilot.cmd forget-github-token
```

`devpilot build "<request with a GitHub URL>"` reads and profiles the repository first (free), decides the stack from the
repository's files, and only asks for spending approval when that stack can be built and verified. An accepted build
imports exactly the resolved commit into the project, so the planner and OpenCode modify the existing files instead of
starting over. The planner is told the repository's URL, commit, file names and the intent (adapt, modify or rebuild);
never the repository's own text, which is untrusted.

## How a repository is read

- **URL rules** (`app/repos/github_url.py`): only `https://github.com/<owner>/<repo>` forms and `git@github.com:`; refused
  before any request: other hosts and look-alikes, `http`, ports, credentials in the URL (not echoed back), queries,
  fragments, percent-encoding, `..`, refs git would reject, two repositories in one request.
- **Authorization** (`app/repos/github_access.py`): a token is optional. It comes from `GITHUB_TOKEN`, `GH_TOKEN` or the
  DPAPI-encrypted store; it is sent only to `api.github.com` as a header. A 404 without a token is reported as "may be
  private"; with a token, as "not visible to this token". 401, 403 (permission), SAML SSO, rate limits, server errors and
  network failures each have their own message.
- **Download**: the archive of one pinned commit, redirected to `codeload.github.com` (now in the egress allowlist's
  `github` component). The signed redirect URL is followed without the token and never shown. The archive is unpacked in
  memory by the existing importer, which refuses links, `.git` paths, path escapes, files that could reconfigure the
  coding engine, and anything over the limits. Nothing in it is run.

## How a website is verified

`app/verification/web.py` drives three containers; the person's files are never mounted, only a disposable copy without
`node_modules`, build output, `.git`, `.npmrc` or `.yarnrc`.

1. **Install proxy**: `edge/install_proxy.py` in the approved `python:3.12-alpine` image, as `nobody`, read-only root. It
   accepts only `CONNECT <host>:443` for the hosts of the egress allowlist's `sandbox_install` component that npm needs
   (`registry.npmjs.org`), refuses names that resolve to non-public addresses, and audits every decision.
2. **Install** (React + Vite): `devpilot/verify-web` on an **internal** Docker network whose only peer is the proxy. A
   fixed script runs `npm ci` (with a lockfile) or `npm install --no-package-lock`, always `--ignore-scripts`, with
   DevPilot's own npmrc; dependencies from git, URLs or files are refused. No package code runs while a network exists.
3. **Verify**: the same image with `--network none`: tsc (when the project is TypeScript), `vite build` into DevPilot's
   own folder, DevPilot's static server (with single-page fallback), every page and referenced asset over HTTP, then
   Chromium: visible content, no uncaught errors, no failed same-origin requests, no console errors, same-origin links
   followed, visible buttons clicked, and an optional list of required texts, selectors and click results. Requests to
   other sites are blocked and listed, not counted as failures (there is no network).

Every container drops all capabilities, cannot gain privileges, has a read-only root, memory and process limits, and is
removed afterwards with its networks. A project's own scripts (`npm run build`, `preview`, `test`) are never run. The
verdict is PASSED only when every stage passed **and** the browser really ran; a missing report is NOT_RUN.

**Image approval**: `devpilot setup --web` builds `devpilot/verify-web` from `backend/sandbox_images/verify-web`
(Microsoft's `mcr.microsoft.com/playwright:v1.49.1-noble`, Node 22.12, plus `playwright-core@1.49.1`), then runs four
fixture projects: two must pass and two must fail at the expected stage. Only then is the exact image ID recorded in
`approved-web-runtime.json`; each later use re-reads the tag's ID and refuses on any difference.

## What remains before `devpilot build` can generate websites

The verifier is real, but the build engine does not yet use it. The engine verifies each component with **one**
no-network container job (`verification.recipes`), and the website path needs the install-then-verify sequence above.
Until the following are done and a real paid build has passed, website stacks stay declined
(`verification.recipes.WEB_ENGINE_WIRED = False`):

1. Run `app.verification.web` as the VERIFYING step for `static-html` and `react-vite` components (the job builder,
   the run's evidence and the recovery classifier all assume one container today).
2. Turn the plan's acceptance criteria for pages into the verifier's `expect` checks (routes, required texts, selectors,
   button results), and an independent acceptance suite for websites, so a generated site is not judged only by checks
   the generator chose.
3. Let OpenCode claim the website stacks (`selection/catalog.py` lists it for `python-cli` only, where it has been shown).
4. One real, user-approved paid build of a website, start to finish, with its ZIP and verification report kept as
   evidence.

## Requirements added

- About 2.5 GB more disk for the verifier image (Docker's storage), and 2 GB of memory for its containers.
- Internet during `setup --web` (Microsoft's container registry and the npm registry) and during `verify-site` for
  projects with dependencies (the npm registry only, through the proxy).
