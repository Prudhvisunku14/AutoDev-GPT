# devpilot/verify-react:1: the runtime of the react-vitest recipe (app/verification/recipes.py).
# SPECIFICATION ONLY: never built, never run, and NOT YET PINNED.
#
# PENDING (needs network and approval): the Node LTS tag and digest (the host has Node 24.14.1, which proves nothing
# about images), and every npm version. No version is guessed here. package.json and package-lock.json are produced
# once, with approval, for exactly: react, react-dom, vite, vitest, jsdom, @vitejs/plugin-react, @testing-library/react,
# @testing-library/jest-dom, @testing-library/user-event, typescript. npm is the only package manager. The lockfile is
# generated inside the chosen Node Alpine image, so it records the linux-musl native packages (esbuild, rollup); one
# made on Windows may miss them. The placeholder below is deliberate: a build fails rather than pick a version.
FROM node:LTS-alpine-PENDING-DIGEST

WORKDIR /opt/verify
COPY package.json package-lock.json ./
# Lifecycle scripts are never run; the cache is removed so the image holds only the installed tree.
RUN npm ci --ignore-scripts --no-audit --no-fund && npm cache clean --force

# The fixed entry the recipe calls: copy /workspace/<component> into /tmp/app (the workspace is read-only and holds no
# node_modules), link /opt/verify/node_modules beside it, and run Vitest with the JSON reporter, offline, with Vite's
# cache in /tmp (node_modules is read-only) and, as recommended, a fixed config from the image (docs/DOCKER_READINESS.md
# §4.4). Native binaries stay under /opt/verify, because /tmp is noexec. To be written and tested on Docker; until then
# the recipe is not approved.
COPY run-vitest.mjs /opt/verify/run-vitest.mjs

USER 65534:65534
WORKDIR /workspace
