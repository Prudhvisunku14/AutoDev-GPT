# Runtime for the OpenCode contract check (Phase 4C). NOT BUILT YET: building needs the user's approval,
# because `apk add` downloads two packages (about 1.5 MB) from the official Alpine mirror.
#
# Why: the official opencode-linux-x64-musl 1.19.5 binary needs libstdc++.so.6 (read from its ELF
# headers), and python:3.12-alpine (Alpine 3.24.3) has neither libstdc++ nor libgcc. OpenCode's own
# Dockerfile installs the same two libraries on Alpine.
#
# - The base is the python:3.12-alpine image already on D:, pinned by digest (SECURITY §11).
# - apk verifies the packages' signatures with the Alpine keys that ship in the base image
#   (no --allow-untrusted), and --no-scripts runs no package install scripts.
# - The build script refuses the image if apk installs or upgrades anything else.
# - The OpenCode binary is NOT copied in. It is mounted read-only from the integrity-verified npm
#   download, so this image holds no OpenCode code.
# - The build context is this folder, which holds only this file.
FROM python:3.12-alpine@sha256:e6dae589c1c05976188fd3bd019afd091445fc533ed2ea29001beddc7368a9c8
RUN apk add --no-cache --no-scripts libstdc++ libgcc
LABEL devpilot.purpose="opencode-contract-check"
USER 65534:65534
