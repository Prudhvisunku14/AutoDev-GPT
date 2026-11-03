# Milestone 6: Google Drive and an autonomous RAG chatbot builder

**Status (2026-11-03):**
- The offline milestone is committed as `829b9da`.
- The security hardening (§14) is implemented and offline-tested, and **not committed**.
- No Google server was contacted, no real OAuth sign-in happened, and no paid embedding or model call was made.
- No Docker container ran, and no package was installed.
- **There is no working live Google Drive RAG chatbot:** none was generated or executed. Generation stops safely, naming its blockers.

The request this milestone prepares: "Build a RAG chatbot using documents from my Google Drive folder. Use a React frontend, FastAPI backend, document processing, embeddings and a vector index. The chatbot must answer questions with accurate source citations." Nothing is specific to one folder or one collection. `app/demo/rag.py` is data for one request.

| | Status |
|---|---|
| Drive connector, folder restriction, ingestion, chunking, index, retrieval, citation checks, the seven tools, blueprint and plan check, approval details, the OAuth state, callback, refresh and revocation contracts, the credentials endpoint | **Implemented and offline-tested** |
| Google Drive itself (an in-process fixture serving Drive v3-shaped answers), Google consent in the tool flow (the approval stands for it), the OAuth provider in tests, embeddings (a hashing test double), GPT (FakeLLM) | **Simulated only** |
| The isolated parser interface and its host-side checks; the parser image is a specification | **Security interface implemented but not container-verified** |
| Consent, code exchange, refresh and revocation against Google itself (`GoogleOAuthProvider` has only met an in-process transport) | **OAuth flow not tested against Google** |
| Refresh-token encryption: Fernet is the approved mechanism, and `cryptography` is not installed, so consent fails closed | **Pending an approved install** |
| The `document-parser` image: built, run on Docker, approved by ID | **Parser container pending** |
| A PDF parser in that image | **PDF parsing pending** |
| An embedding provider (the OpenAI API or a local model): neither is implemented | **Real semantic embeddings pending** |
| Code for the chatbot's three components, the FastAPI and React runtimes, and the acceptance suite in the sandbox | **Real chatbot generation pending** |

## 1. What was reused

These were reused rather than duplicated:
- **The approval queue and PolicyEngine:** every Drive call is `EXTERNAL_NETWORK`, so a person approves each one.
- **The egress allowlist:** its existing `google_drive` component (`accounts.google.com`, `oauth2.googleapis.com`, `www.googleapis.com`). The file is unchanged.
- **The GitHub client's pattern:** the allowlist checked on every hop, redirects included; size caps; tests through an in-process transport.
- **The redactor:** its existing Google token patterns.
- **`Settings.google_oauth_client_secret`:** declared but unused.
- **The repository scanner's untrusted-instruction patterns:** now exposed as `instruction_findings`.
- **Multi-stack plans and the plan gate, per-component verification recipes, the blocked-outcome path (`verification_unavailable`), and component-bound frozen acceptance.**
- **The adaptive selector, which chooses the embedding provider, and the recovery engine, which decides when none can run.**
- **The run report, and FakeLLM with the demonstration pattern of `app/demo/fullstack.py`.**

No OAuth, Google or vector code existed before. Nothing was found to duplicate.

## 2. Google Drive connector (`backend/app/drive/`)

- **Feature flag:** `DRIVE_SOURCE` is `off` by default, and then no Drive tool exists.
- **`google` mode:** wires the Drive v3 client, the OAuth flow and the isolated parser. With no approved cipher, consent fails closed; with no approved parser image, every document is refused. So such a run stops safely (§14).
- **Fixtures:** tests and the demonstration pass a fixture runtime to `create_app`. No setting selects fixtures or the test double.
- **`GoogleDriveSource`** implements `files.list` (parents query, pages), `files.get`, `alt=media` and `files.export` (text/plain):
  - every URL passes the `google_drive` allowlist, redirects included; a redirect to another host is refused before it is followed;
  - answers have byte caps;
  - 429, 5xx and rate-limit 403s are retried twice, with backoff, and then reported;
  - a 401 means the authorization is gone;
  - IDs are checked before they reach a URL or a query, so no quote can reach `q`;
  - the token comes from the authorizer per call, and no error message holds it.

  It has never reached Google.
- **`FixtureDrive`** is a simulated Drive with invented documents. Each account sees only its own items. It can rate-limit, fail, forget a token, and change, move or bin a file.
- **Not implemented:** shared drives (`supportsAllDrives`), Drive change notifications, and downloads redirected to `googleusercontent.com`. That host is not allowlisted, so such a download is refused.

## 3. OAuth and access control

- **Scope:** only `drive.readonly` (`check_scopes`). Write, delete, share and full scopes are refused.
- **Folders, enforced by DevPilot:** that scope lets Google answer for every file the account can read, so DevPilot enforces folders itself (`ScopedDrive`):
  - discovery walks down from the approved folders, and a file counts only when it was listed under a reached folder and names it as a parent;
  - before every download, the file must have been discovered, and must still be under a reached folder, not binned, supported and within the size limit;
  - an invented ID, a file in another folder, or a file moved out since discovery is refused.
- **Grants:**
  - each grant belongs to one project, and another project's run is refused;
  - states are `PENDING_CONSENT`, `GRANTED` and `REVOKED`;
  - the token lives only inside the authorizer; events, tool results, reports and GPT get the grant ID.
- **Explicit authorization:** `request_drive_authorization` waits for a person, and only the folders in the approved call are granted.
- **Real flow (`GoogleOAuthAuthorizer`, app/drive/oauth.py):** the consent URL, the callback, the code exchange, encrypted refresh-token storage, refresh and revocation are implemented and tested against a simulated provider only (§14). It has never run against Google, and storage fails closed until `cryptography` is installed.
- **Revocation (`revoke_drive_authorization`, no approval needed):**
  - the grant is marked revoked and the token forgotten;
  - every chunk and cached document read under it is removed;
  - later reads are refused.

## 4. Document ingestion (`backend/app/rag/extract.py`, `parsing.py`, `parsers.py`)

Real documents are parsed only by the isolated parser, which refuses them while no approved parser image exists (§14). The in-process path is for the simulated fixture documents only.

| Format | How | Location kept |
|---|---|---|
| TXT | UTF-8 only; NUL bytes refused | paragraph N |
| Google Docs | exported as text/plain | paragraph N |
| DOCX | the standard library (zipfile, ElementTree), with limits on entries and uncompressed size; any DTD or entity refused | section 'heading', or paragraph N |
| PDF | **no parser is installed.** `MissingPdfParser` refuses with that reason; a parser plugs in behind `PdfTextExtractor` | page N |

- **Kept for every document:** the Drive ID, untrusted title, modification time, the grant it was read under, the SHA-256 of the bytes, the parser, and located sections.
- **Limits:** files per call, bytes per file and per call, characters and sections per document.
- **Refused safely:** unsupported, malformed, empty and oversized documents, each with a reason that quotes none of the document.
- **Untrusted instructions:** text addressed to AI agents is flagged and stays data. Document text never reaches GPT, which sees metadata only. Nothing in a document can change instructions, tool permissions, approvals or network policy.

## 5. The knowledge base (`backend/app/rag/`)

- **`chunking.py`:** overlapping windows per section. Each chunk carries owner, grant, file ID, title, location, document hash and modification time.
- **`embeddings.py`:** the `Embedder` interface. `HashingEmbedder` is a **TEST DOUBLE**: deterministic feature hashing of words. It is lexical, not semantic (a test shows "car" and "automobile" are not close). **It does not provide production-quality retrieval.**
- **`index.py`:** the `VectorIndex` interface. `InMemoryVectorIndex` scores exactly, and only chunks inside the `AccessScope` (owner and active grants) are scored at all.
- **`knowledge.py`:**
  - `add` indexes a document, replaces it when its hash changed, and skips it when unchanged;
  - `remove`, `sync` (drops what a fresh listing no longer holds) and `revoke`;
  - an embedding budget, estimated before anything is embedded;
  - `answer` is an extractive test double that quotes the best-matching retrieved sentences, or declines with no citation;
  - `check_citations` proves each citation names a retrieved chunk that still exists in scope, has the current document hash, and contains the quote. **That shows a citation is traceable, not that the answer is true.**
- **`selection.py`:** the adaptive selector chooses the embedder.
  - The test double is chosen only when nothing real is eligible, and any real provider outranks it.
  - The OpenAI API and a local model are planned, so they can never win.
  - With no provider, the recovery engine stops the run (`embedding_unavailable`).

## 6. Orchestration (`backend/app/tools/adapters/drive.py`)

| Tool | Risk | Approval | Notes |
|---|---|---|---|
| `request_drive_authorization` | EXTERNAL_NETWORK | yes | folder IDs or links only; read-only |
| `discover_drive_documents` | EXTERNAL_NETWORK | yes | IDs, untrusted titles, types, sizes, dates |
| `ingest_drive_documents` | EXTERNAL_NETWORK | yes | discovered files only; per-file status |
| `build_rag_index` | WORKSPACE_WRITE | no | embedder chosen by the selector; budget check |
| `configure_retrieval` | READ | no | citations shown, never text or quotes |
| `prepare_rag_application` | WORKSPACE_WRITE | no | blueprint; writes `RAG_BLUEPRINT.json` (no document text) |
| `revoke_drive_authorization` | WORKSPACE_WRITE | no | taking access away is always allowed |

- **States:** all are in PLANNING; revocation is available in any state. GPT stays the orchestrator.
- **Python enforces:** authorization, eligibility, folder and file boundaries, network policy, limits and budgets, and the stops.
- **In memory only:** discovered documents, ingested documents and the index live in the backend process. A restart loses them, and discovery must be repeated, with approval.

## 7. Planning and generation

`prepare_rag_application` returns the blueprint (`backend/app/rag/blueprint.py`).

**Components**
- `knowledge` (python-fastapi): ingestion, chunking, embeddings, index.
- `backend` (python-fastapi): `POST /api/ask`, `GET /api/documents`, per-user access.
- `frontend` (react-vite): chat, citations, loading and error states.

Dependencies run knowledge → backend → frontend.

The blueprint also holds:
- the API and citation format;
- the rules every part must keep;
- the blockers;
- a test seam: `create_app(documents=, grants=, embedder=)`.

`submit_plan` then refuses a plan that drops a component, changes its stack, or breaks a dependency.

Generation is attempted, and stops before any provider runs (`STOPPED_BY_POLICY`, `verification_unavailable`). The exact blockers:
1. The FastAPI and React verification runtimes are not built or approved, so knowledge, backend and frontend are all blocked.
2. No real coding provider claims those stacks; OpenCode claims only `python-cli`.
3. The knowledge component needs a PDF parser, an embedding client and a vector store, which no approved runtime provides.
4. Drive access and embeddings were simulated.

## 8. Frozen acceptance criteria (`app/demo/rag.py`, checked by parsing only)

| Code | Criterion |
|---|---|
| AT-001 | a user lists only the documents that user is authorized to see |
| AT-002 | every authorized document is parsed into chunks |
| AT-003 | each citation maps back to a located chunk of an authorized source |
| AT-004 | each citation quotes text its source holds, **and the answer states the supported fact** |
| AT-005 | no citation names a document that does not exist |
| AT-006 | a question the documents cannot answer is declined, with no citation |
| AT-007 | another user's document is never retrieved; an unknown user is refused |
| AT-008 | a changed document is re-indexed; a deleted one is no longer listed or cited |
| AT-009 | instructions inside a document change neither the answer's sources nor what it reveals |
| AT-010 | a missing embedding provider is a 503 with no invented answer |
| AT-011 | the chat shows each citation's title and location (bound to `frontend`, a Vitest file) |

AT-004 checks the stated fact, because a citation's presence alone is not proof. The suite needs the generated application and both images, and has never run.

## 9. Offline demonstration

Run `python -m app.demo.rag` from `backend`, with temp folders on D:. It passes 24 of 24 checks. It goes through the real API, orchestrator, approvals, tools, Drive client and knowledge base:
1. The request becomes ten requirements.
2. Simulated authorization, approved by a person, gives read-only access to one folder.
3. Discovery stays inside that folder and its subfolder, and is never asked about the other folders.
4. A request for a file outside the folder is refused, even after approval.
5. Ingestion:
   - three Google Docs, TXT and DOCX documents are parsed;
   - the PDF waits for a parser;
   - a broken DOCX and an image are refused;
   - the injected instructions are flagged.
6. Chunks with full metadata go into the test-double index.
7. Retrieval cites real chunks and declines "What is the population of Lisbon?".
8. The blueprint is produced, the three-component plan is accepted, and the criteria are set.
9. Generation stops, naming every blocked component.
10. On the run's own runtime, afterwards:
    - a changed document is re-indexed by hash;
    - a deleted one is removed on sync;
    - another project's scope retrieves nothing;
    - revocation removes every chunk and stops the token.

GPT never saw document text or a token. The report names every simulation.

## 10. Tests

**New test files:**
- `test_drive_access.py` (22): folder links, scopes, grant states, the consent URL, the folder restriction, pagination and limits, other accounts, files that move or are binned, size caps, retries, 401, redirects, unsafe IDs.
- `test_rag_ingestion.py` (19): each format, refusals, archive and size limits, the missing PDF parser, flagged instructions, chunk metadata and overlap.
- `test_rag_knowledge.py` (13): the lexical test double, scope isolation, updates, deletions, revocation, citations and their forgery, the missing embedder, the budget, provider selection.
- `test_drive_tools.py` (9): the feature off by default, the rehearsal, the suite's binding, injection through the orchestrator, the recovery stop with no embedder, revocation through the tool, grants across projects, the plan gate with the blueprint, pending real consent.

## 11. Security findings

The first three findings from the offline milestone were addressed in the hardening round (§14).

1. **Parsing in the backend: closed by design, but not container-verified.**
   - A runtime that is not simulated refuses an in-process parser.
   - Every real document goes to `IsolatedParser`, which refuses it while no approved parser image exists.
   - The image has never been built, so the isolation itself is unproven.
2. **SECURITY.md §7: the contracts are implemented.** The state check, its expiry and single use, the browser binding, strict redirect URI validation, the callback, safe refresh, the credentials endpoint and separate revocation reporting are all in place. Still open:
   - **Encrypted storage cannot run:** `cryptography` is not installed, so consent fails closed.
   - **Credentials live in memory only:** a restart loses them, so consent must be repeated.
   - **The flow has never met Google.**
3. **Approvals now show names, types, locations, the operation and the reason**, as SECURITY.md §7 asks.

**Still open**
4. **Format detection** checks PDF magic, ZIP structure and UTF-8 text, but takes Drive's MIME type as the format.
5. **Shared documents:** files shared with the account inside an approved folder are readable, as Google allows. Other DevPilot projects cannot use the grant.
6. **The extractive answerer and the lexical embedder are test doubles.** A real chatbot needs a model and real embeddings, and their own evaluation.
7. **The parser's output channel is stdout**, capped by `SANDBOX_MAX_OUTPUT_BYTES` (64 KB by default, at most 4 MB). A large document is refused, not cut short. SECURITY.md §8 describes a separate output mount, which the sandbox does not support yet.
8. **Python cannot reliably erase secrets from memory.** Decrypted refresh tokens are short-lived variables, and are never logged or stored.

## 12. What real Google Drive access needs

1. `cryptography` installed with approval, and a Fernet key set as `DEVPILOT_TOKEN_KEY` by the user.
2. Persistent storage for the encrypted refresh tokens; today they are kept in memory only.
3. A Google OAuth client, with its ID configured and its secret set by the user in `.env`.
4. The `document-parser` image built, verified on Docker, and approved by ID; and, with approval, a larger output channel for big documents.
5. A PDF parser added to that image, with approval.
6. A dashboard button that starts consent (the endpoint exists).
7. `DRIVE_SOURCE=google`.
8. A person's consent in the browser, and a first test against Google.

## 13. What a genuinely generated and tested chatbot needs

1. The FastAPI and React images, built, verified and approved: [DOCKER_READINESS.md](DOCKER_READINESS.md).
2. The knowledge component's packages (PDF parser, embedding client, vector store) added to an approved image.
3. A real coding provider shown working on `python-fastapi` and `react-vite`, with paid calls approved.
4. A real embedding provider, with its cost approved.
5. The frozen acceptance suite run in the sandbox.
6. Docker, which needs the laptop's memory and C: headroom first.

## 14. Security hardening (2026-11-03, not committed)

### OAuth (`backend/app/drive/oauth.py`, `backend/app/api/integrations.py`)

**State**
- The value is random, single-use, bound to the project, and expires after 10 minutes.
- It is removed on the first callback, whatever follows, so a refused or replayed answer cannot be retried.
- Its browser binding is an HttpOnly cookie, SameSite=Lax, scoped to the callback path, and compared in constant time.

**PKCE (S256):** the verifier lives only in the pending record, goes only to the code exchange, and never appears in a URL. A code issued for another verifier is refused.

**Redirect URI:** validated when it is configured. It must be https, or http to a loopback address with a port, and the path must be exactly `/oauth/google/callback`. A user, query, fragment or wildcard is refused. Only that URI is ever used.

**The callback refuses each of these, and none of them grants:**
- a forged, expired or replayed state;
- another browser;
- a Google error;
- a malformed code;
- a scope beyond read-only;
- a missing refresh token.

Tokens that will not be kept are revoked at once.

**Storage:** refresh tokens go through a `TokenCipher`.
- The only product cipher is Fernet, keyed by `DEVPILOT_TOKEN_KEY`.
- `cryptography` is not installed, so completing consent **fails closed before any code is exchanged** (503).
- Nothing else stands in for Fernet; tests use a cipher double that lives only in the test file.
- Access tokens stay in memory until they expire.

**Refresh:** one refresh at a time per grant.
- A rotated refresh token is stored encrypted again.
- An outage changes nothing.
- `invalid_grant` deletes the local credential, revokes the grant, and removes everything read under it.

**Revocation:** local deletion comes first, and remote revocation is reported separately: `revoked`, `already_invalid`, `failed` (with what to do), `not_attempted` or `simulated`.

**Routes**
- `POST /api/v1/integrations/google/consent` and `DELETE /api/v1/integrations/google/credentials?project_id=` require the API token.
- `GET /oauth/google/callback` is reached by the browser.
- All of them answer 404 while Drive is off.
- Deleting credentials affects only the named project.

### Parser isolation (`backend/app/rag/parsing.py`, `backend/app/rag/parsers.py`, `backend/sandbox_images/document-parser/`)

**The shared code:** the parsing code is standard library only. It is the same file in the backend's fixture path and in the container.

**`IsolatedParser`**
- It stages the bytes in the parser's own folder, read-only to the container.
- It runs `document-parse` through the sandbox with the approved image: no network, all capabilities dropped, read-only root, user 65534, and 512 MB, 1 CPU, 32 processes and 60 s, capped again by the settings.
- It removes the staged copy afterwards.
- It accepts only `{"sections": [...]}` with valid locations and text, within the limits, or `{"refused": reason}`.
- It checks the result again on the host, and computes the hash on the host.

**Fail closed:** with no approved image, no sandbox or no settings, every document is `parser_unavailable`. PDF is always unavailable.

**`FixtureParser`** is in-process, and is accepted only by a simulated runtime.

**The entry point** `parse_main.py` is tested on the host over trusted fixtures. **The image has never been built or run.**

### Approval details

Each Drive approval now records what a person needs to decide:
- the operation;
- the reason (`discover` and `ingest` take one; an older call says "No reason was given.");
- the folders, with their IDs and names (unknown before access is granted, because nothing is fetched before approval);
- for ingestion, each file's sanitized name and its flags, type, folder path, size and date.

**Names stay labels:**
- control, format and bidirectional characters are removed, and the name is shortened;
- text addressed to AI agents is flagged;
- access is decided by ID and folder, checked again at execution.

Tests show that a rename, a deletion, a move out of the folders, or a revocation between approval and execution cannot bypass these checks.

### Tests

- `test_drive_oauth.py` (28).
- `test_drive_parsers.py` (19).
- Five new tests in `test_drive_tools.py`, plus checks added to the revocation test.
- One existing test was updated for the new consent API.

**Focused regression:** 583 passed, 0 failed.

**Whole offline suite** (run once memory allowed it, under the fenced runner): 3,585 passed, 17 skipped (opt-in Docker and live tests), 0 failed, in 169 s. Peak memory was 248 MB. Commit headroom never fell below 2.4 GB, against the 2 GB floor. The count is the 3,533 of `829b9da` plus the 52 new tests.
