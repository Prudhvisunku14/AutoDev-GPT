# devpilot/verify-fastapi:1: the runtime of the fastapi-pytest recipe (app/verification/recipes.py).
# SPECIFICATION ONLY: never built, never run. Building it downloads wheels and needs explicit approval.
#
# Base: python:3.12-alpine, the image the sandbox already approves for python-cli. Pin it by image ID at approval.
# PENDING validation (needs network): that every pinned wheel exists for musllinux/CPython 3.12, pydantic_core above
# all. If not, the base becomes python:3.12-slim, pinned by image ID, and nothing else changes.
FROM python:3.12-alpine

ENV PYTHONDONTWRITEBYTECODE=1 PIP_DISABLE_PIP_VERSION_CHECK=1 PIP_NO_CACHE_DIR=1
COPY requirements.txt /opt/verify/requirements.txt
# Wheels only, so no package's setup code runs, and only the pinned set (--no-deps: the list is closed), then checked.
# Hashes (--require-hashes) are added when the wheels are first resolved.
RUN python -m pip install --no-deps --only-binary=:all: --no-compile -r /opt/verify/requirements.txt \
 && python -m pip check
# DevPilot's own pytest configuration: the recipe passes it with --config-file, so a project's pytest settings are never read.
COPY pytest.ini /opt/verify/pytest.ini

# The sandbox runs every container as 65534:65534 with no network, a read-only root, all capabilities dropped and
# no new privileges; the image itself needs no root at test time.
USER 65534:65534
WORKDIR /workspace
