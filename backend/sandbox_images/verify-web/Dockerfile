# devpilot/verify-web: the website verification runtime (app/verification/web.py).
#
# Base: Microsoft's Playwright image, pinned to one release; it carries Node.js and the matching headless Chromium.
# The only package added is playwright-core at exactly the same version, installed with lifecycle scripts disabled.
# The image is built by `devpilot setup --web`, then recorded as approved for this installation only after its
# self-test passes (a static site and a React + Vite site pass; a broken site fails). Each run re-checks the image ID.
#
# At run time the image never has a direct network:
#   install  npm ci/install --ignore-scripts, on an internal Docker network whose only way out is DevPilot's install
#            proxy (edge/install_proxy.py), which tunnels to registry.npmjs.org:443 and nothing else.
#   verify   --network none: type check, vite build, DevPilot's own static server, HTTP and asset checks, Chromium.
FROM mcr.microsoft.com/playwright:v1.49.1-noble

WORKDIR /opt/verify
RUN printf '{"name":"devpilot-verify-web","private":true,"dependencies":{"playwright-core":"1.49.1"}}\n' > package.json \
 && npm install --ignore-scripts --no-audit --no-fund --omit=dev \
 && npm cache clean --force
COPY verify-web.mjs install-deps.mjs npmrc /opt/verify/

# pwuser (uid 1000) is the image's own unprivileged user; root is never used at run time.
USER pwuser
WORKDIR /work
