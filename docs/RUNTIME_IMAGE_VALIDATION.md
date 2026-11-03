# Runtime image validation: why `sha256:a7753fe4…` is not present (2026-11-04)

**Status: validated; the request below is superseded (2026-11-04) by a per-installation mechanism, so the built-in pin was never changed.**
`APPROVED_RUNTIME_IMAGES`, the hash verification and every check are exactly as they were. The old terminal scripts
(`live_demo.py`, `live_build.py`, `live_server.py`) still compare against that constant, and on a machine whose image was
rebuilt they still report the mismatch described here. The `devpilot` command does not: see *Superseded* at the end.

## The failure

`scripts/live_demo.py` preflight: `FAIL image present: sha256:a7753fe443915eed28c577c1d` (22 of 23 passed). The pin is
`APPROVED_RUNTIME_IMAGES` in `backend/app/coding/opencode_container.py`; the tag `devpilot/opencode-runtime:1.19.5` now
resolves to `sha256:c1e2f26e24a3c11d52789a366fb7e1c9d77931742eb657a4c0860def8236e5cd`.

## Root cause

The approved image no longer exists on this machine. Its tag was re-pointed to a rebuild:

| Fact | Evidence |
|---|---|
| The tag was re-created on **2026-11-03 21:05 UTC** | `docker image inspect`: `Metadata.LastTagTime` and `Identity.Build.CreatedAt` |
| The engine uses the **containerd image store** (Docker 29.9.2, Docker Desktop) | `docker info`: `driver-type io.containerd.snapshotter.v1` |
| So `Id` is the digest of an **OCI index**, not of the image config | `Descriptor.mediaType = application/vnd.oci.image.index.v1+json`, `Descriptor.digest == Id` |
| The index holds the image manifest **plus a BuildKit provenance attestation** that records the build's timestamps and invocation ID | index entries: image `sha256:875a28f7…`, attestation `sha256:9a7f1789…` (`slsa.dev/provenance/v1`, started 2026-11-03T21:05:28Z, invocation `fn09a6x67053…`) |
| A rebuild therefore changes the `Id` **even when every layer is identical**; the `COPY rg` layer was a BuildKit cache hit, so the history still reads 2026-10-31 | history: `COPY rg … 2026-10-31T16:15:08+05:30`; layers below |

The pin is a hash of the *build record*, not only of the content. Any rebuild, however faithful, breaks it. That is the
gate working as designed: a changed identity needs a person's approval.

The original image (`a7753fe4…`) is gone: it is not in `docker image ls`, and its index digest cannot be reproduced,
because it embeds the original build's attestation. So it cannot be restored. Only the content can be verified.

## Validation of the replacement (offline: `docker save`, tar members read, nothing run)

| Check | Result |
|---|---|
| Base image `devpilot/opencode-check:1.19.5` is the one recorded earlier | ID `sha256:c380fdb5…` matches `docs/PROGRESS.md`, `ARCHITECTURE_OPTIMIZATION.md` and `DOCKER_READINESS.md` |
| The first 5 layers of the replacement are the base image's | `diff_ids` identical, in order |
| Exactly **one** layer was added | `sha256:73f235d8…`; its `diff_id` equals the SHA-256 of the uncompressed layer |
| That layer contains only `usr/`, `usr/local/`, `usr/local/bin/`, `usr/local/bin/rg` | owner root, modes `0755` (rg is not setuid, not writable) |
| `rg` is the verified ripgrep 15.1.0 | SHA-256 `ebeaf56f8a25e102e9419933423738b3a2a613a444fd749d695e15eba53f71f2`, 5,445,512 bytes: equals `rg_sha256` in the `ripgrep-15.1.0\manifest.json` kept with the verified OpenCode download (archive SHA-256 `1c9297be…91599`, as recorded in `PROGRESS.md`) |
| Runs as non-root | `User 65534:65534`, identical to the base |
| Sandbox-relevant configuration unchanged | `Env`, `Cmd`, `Entrypoint` (none), `WorkingDir`, `Labels`, `ExposedPorts` (none), `Volumes` (none), architecture and OS: **identical to the base image** |
| Built with no network | provenance `args: force-network-mode=none`; the added step is a `COPY`, which cannot use the network anyway |
| Build recipe is the documented one | `FROM devpilot/opencode-check:1.19.5` / `COPY rg /usr/local/bin/rg` (`…\ripgrep-15.1.0\context\Dockerfile`; `PROGRESS.md` §Runtime image) |
| OpenCode runtime dependencies | the base image's `libstdc++` and `libgcc` layer is layer 4, unchanged; ripgrep is now present, which is what stops OpenCode downloading it |

The OpenCode binary is not in the image: it is mounted and checked separately against `PINNED_BINARY_SHA256`, which is
untouched. The container's hardening (read-only root, dropped capabilities, no network, limits) is applied at start by
`opencode_container.production_problems`, not by the image, and is untouched.

**Limit of this validation:** the original image's config digest was never recorded, so "identical to the original" cannot
be proven byte for byte. What is proven is that the replacement equals the *documented recipe*: the recorded base image
plus one layer holding only the hash-verified ripgrep.

## Requested approval

Replace the approved identity `sha256:a7753fe4…` with `sha256:c1e2f26e24a3c11d52789a366fb7e1c9d77931742eb657a4c0860def8236e5cd`.
Hash verification and every other check stay on. Files that would change:

1. `backend/app/coding/opencode_container.py:42`: `APPROVED_RUNTIME_IMAGES` (and its comment: say the pin is an index digest, and record the old ID).
2. `backend/scripts/live_demo.py:55`: `RUNTIME_IMAGE`.
3. `backend/tests/unit/test_launcher.py:226`: the test that pins the constant.
4. Documentation of the identity: `docs/PROGRESS.md:1161`, `docs/FIRST_REAL_BUILD.md:62`, `docs/DOCKER_READINESS.md:43,258`, `frontend/README.md:54`.

Consequence to know: because the pin is an index digest, **every future rebuild, even an identical one, will need a new
approval.** If you would rather pin something rebuild-stable (the image config digest, or the added layer's `diff_id`),
that is a code change to how `ContainerImage.image_id` is read, and needs its own review; it is not part of this request.

After approval: change the files above, run the focused tests and the full suite, then rerun the offline preflight
(`scripts/live_demo.py` and `scripts/live_server.py` without an approval flag). That preflight reads `.env` for the key's
presence, so it is also yours to approve or run.

## Superseded: a portable, audited image instead of a fixed ID

The two observations above, that the ID is a build-record digest and that a locally built image's layers differ per
machine, mean no constant can be right on any other computer. The `devpilot` command (docs/INSTALL_WINDOWS.md) therefore
verifies *contents* instead of trusting an ID:

- `devpilot setup` builds the image from `backend/app/cli/Dockerfile.runtime` under a **new tag**
  (`devpilot/opencode-runtime:portable-1.19.5`; the tag above is never re-pointed), exports it with `docker save`, and audits it
  offline with `backend/app/cli/image_audit.py`: non-root `65534:65534`, no entrypoint/ports/volumes, every layer matching its
  recorded hash, a final layer that holds only `usr/local/bin/rg` (root-owned, 0755, not setuid, SHA-256 equal to the pinned
  ripgrep's), and the C++ libraries beneath it. The unit tests cover every failure mode on synthetic archives in both Docker
  layouts. Separately, the audit was run by hand on this machine's existing `devpilot/opencode-runtime:1.19.5` (it passed, and
  rejected a wrong pinned hash), and by `setup` on the image it built.
- Only an image that passes is recorded as approved *for this installation*, by exact ID, in `approved-runtime.json`.
  `opencode_container.approve_locally` makes the launcher accept exactly that ID in the current process. Each start re-reads the
  tag's ID and refuses on any difference, so a rebuilt or replaced image has no approval until `setup` audits it again.
- The built-in `APPROVED_RUNTIME_IMAGES` is unchanged and still accepted. Hash verification of OpenCode and ripgrep is unchanged.

Result on a clean checkout (2026-11-04): `setup.cmd` built and audited `sha256:f806988f…` and `devpilot doctor` passed 17 of 17.
