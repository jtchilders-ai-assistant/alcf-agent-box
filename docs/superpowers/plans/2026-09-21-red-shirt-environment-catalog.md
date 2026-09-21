# Red Shirt Environment Catalog Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Give Red Shirt a secret-safe, provenance-preserving SQLite catalog of Polaris modules, software paths, dependency edges, and attempt observations without adding a daemon or weakening scientific evidence requirements.

**Architecture:** A standard-library Python CLI builds an immutable site snapshot on the Polaris host and queries it inside the Red Shirt SIF. The schema uses normalized relational tables plus FTS5 and explicit directed relations, while a separate writable attempt overlay stores structured observations and evidence references. Collection parses only allowlisted structure, never stores raw environment dumps, and fails closed on secret-like keys or values.

**Tech Stack:** Python 3 standard library (`argparse`, `sqlite3`, `subprocess`, `hashlib`, `json`), SQLite/FTS5, Environment Modules/Lmod command output, pytest, Apptainer bind mounts, PBS shell launcher.

---

## File map

- Create `scripts/red_shirt_env_catalog.py`: CLI, schema migration, validation, collection, query, and overlay recording.
- Create `tests/test_red_shirt_env_catalog.py`: schema, collection, redaction, FTS, graph traversal, immutability, deterministic JSON, and overlay tests.
- Modify `Dockerfile.red-shirt-polaris`: ship the query CLI in the SIF.
- Modify `deploy/polaris/red-shirt-pepper-campaign.pbs`: validate a host-generated catalog, copy a checksum-verified immutable snapshot into the attempt, expose it read-only to Red Shirt, and advertise it in facts.
- Modify `tests/test_red_shirt_polaris_launcher.py`: static launcher and image integration contracts.
- Modify `scripts/red_shirt_task_context.py`: explain catalog epistemic limits and use in generated `AGENTS.md`.
- Modify `tests/test_red_shirt_task_context.py`: generated-context contract assertions.
- Modify `deploy/polaris/RED_SHIRT_README.md`: operator collection, refresh, validation, and campaign staging workflow.
- Create `docs/red-shirt-environment-catalog.md`: schema, provenance model, threat model, query examples, and limitations.

## Non-negotiable contracts

1. The site catalog is a snapshot, not compatibility proof. Requested, declared, detected, compiled/linked, runtime, and scientific evidence remain distinct.
2. Contradictory observations coexist; no upsert may erase prior evidence.
3. No token, password, credential, authorization header, private key, connection-string credential, secret-file content, or raw environment dump is stored.
4. Site snapshots are finalized atomically, mode `0444`, integrity-checked, and opened read-only inside Red Shirt. Attempt overlays are separate mode-`0600` databases.
5. The catalog accelerates discovery; Red Shirt still chooses, installs, configures, builds, tests, runs, and analyzes the application.
6. Collection is bounded by explicit command timeouts and module limits. Partial collection is labeled incomplete rather than silently represented as complete.
7. Query output is deterministic JSON by default so the agent does not need to scrape prose.
8. The implementation uses Python’s bundled SQLite and no network daemon or third-party database package.

### Task 1: Schema, atomic lifecycle, and secret firewall

**Files:**
- Create: `scripts/red_shirt_env_catalog.py`
- Create: `tests/test_red_shirt_env_catalog.py`

- [ ] **Step 1: Write failing tests for database creation**

Create tests that call `init --output <tmp>/catalog.sqlite --system polaris --source-id fixture`, then assert schema version `1`, required tables (`snapshots`, `entities`, `relations`, `observations`, `entity_fts`, `metadata`), foreign keys, a single open snapshot, mode `0600` before finalization, and no temporary file remains.

- [ ] **Step 2: Run the schema tests and verify RED**

Run: `python3 -m pytest -q tests/test_red_shirt_env_catalog.py -k 'init or schema'`
Expected: FAIL because the CLI does not exist.

- [ ] **Step 3: Implement schema version 1 and atomic creation**

Use a temporary database in the destination directory, `PRAGMA foreign_keys=ON`, WAL disabled for the portable artifact, explicit transactions, and `os.replace`. Store UTC timestamps, system, source ID, collection status, schema version, and tool version. Do not store process environment.

- [ ] **Step 4: Write failing secret-firewall tests**

Cover keys containing token/password/secret/credential/api-key/private-key/authorization, bearer strings, URL userinfo, PEM private-key headers, JWT-shaped strings, and known canary values nested in JSON. Assert the transaction rolls back, the canary is absent from every SQLite text/blob cell and file bytes, and stderr names only the rejected field—not its value.

- [ ] **Step 5: Implement recursive validation and rollback**

Validate all external strings before insertion. Reject secret-like values rather than persisting redacted source material. Permit explicitly non-secret identifiers such as module names and SHA-256 digests. Keep error messages value-free.

- [ ] **Step 6: Implement `finalize` and `verify`**

`finalize` must run `foreign_key_check`, FTS integrity, `quick_check`, mark the snapshot complete, atomically write `<db>.sha256`, and chmod database/sidecar `0444`. `verify` checks the sidecar, schema, integrity, and complete status without making the DB writable.

- [ ] **Step 7: Run focused tests and commit**

Run: `python3 -m pytest -q tests/test_red_shirt_env_catalog.py -k 'init or schema or secret or finalize or verify'`
Expected: PASS.

Commit: `feat(red-shirt): add secure environment catalog schema`

### Task 2: Bounded host collection and provenance

**Files:**
- Modify: `scripts/red_shirt_env_catalog.py`
- Modify: `tests/test_red_shirt_env_catalog.py`

- [ ] **Step 1: Add fixture-driven failing collection tests**

Use a fake command runner fixture for `module --terse avail`, `module show`, `module list`, `which`, compiler `--version`, and `readelf -d`. Assert collection records module/version entities, active state, allowlisted path changes, declared prerequisite/load/conflict edges, executable provenance, ELF `NEEDED`/RPATH/RUNPATH edges, command SHA-256, exit status, timestamps, and source kind—but never raw stdout or the full environment.

- [ ] **Step 2: Add bounded-failure tests**

Assert positive timeout and module-limit validation, deterministic ordering, timeout labeling, nonzero-command observations, and `collection_status=incomplete` when a bounded probe fails. A failed probe must not be upgraded to an absent dependency.

- [ ] **Step 3: Implement `collect`**

Support `--output`, `--system`, `--source-id`, repeatable `--module`, `--discover-modules`, `--module-limit`, and `--command-timeout`. Parse only module names, known module directives, normalized absolute paths, executable identities, and ELF dynamic tags. Record command executable/arguments as structured provenance after secret validation; never invoke `shell=True` and never retain raw streams.

- [ ] **Step 4: Add active-profile collection**

Record active module names from `LOADEDMODULES` only after secret validation, plus explicitly requested executable and library paths. Do not enumerate or persist arbitrary environment variables.

- [ ] **Step 5: Run focused tests and commit**

Run: `python3 -m pytest -q tests/test_red_shirt_env_catalog.py -k 'collect or module or elf or bound or provenance'`
Expected: PASS.

Commit: `feat(red-shirt): collect bounded Polaris environment metadata`

### Task 3: Search, graph traversal, and attempt overlays

**Files:**
- Modify: `scripts/red_shirt_env_catalog.py`
- Modify: `tests/test_red_shirt_env_catalog.py`

- [ ] **Step 1: Write failing query tests**

Create a finalized fixture catalog and assert `search`, `show`, `dependencies`, `reverse-dependencies`, and `status` return stable JSON with schema version, snapshot ID, provenance, evidence level, and completeness. Dependency traversal must be cycle-safe and bounded by `--max-depth` and `--limit`.

- [ ] **Step 2: Implement immutable query commands**

Open site databases with URI `mode=ro&immutable=1`, set `PRAGMA query_only=ON`, validate positive limits, use parameterized SQL, use FTS5 for search, and sort results deterministically. Never interpolate user expressions into SQL.

- [ ] **Step 3: Write failing overlay tests**

Test `observe --overlay <attempt.sqlite> --site <site.sqlite> --input <structured.json>`. Require observation kind, claim/evidence level, subject, outcome, evidence path, evidence SHA-256, command exit code, and environment profile ID. Assert contradictory success/failure observations both remain queryable and that site bytes/checksum do not change.

- [ ] **Step 4: Implement overlay creation and merged reads**

Create overlays atomically at mode `0600`; validate evidence paths are absolute and under an explicitly supplied attempt root; record references and digests, not evidence file contents. `search --overlay` merges result sets while retaining source database and snapshot identity.

- [ ] **Step 5: Run focused tests and commit**

Run: `python3 -m pytest -q tests/test_red_shirt_env_catalog.py -k 'search or show or dependencies or overlay or observe or immutable'`
Expected: PASS.

Commit: `feat(red-shirt): query catalog graphs and record attempt evidence`

### Task 4: Red Shirt image and campaign integration

**Files:**
- Modify: `Dockerfile.red-shirt-polaris`
- Modify: `deploy/polaris/red-shirt-pepper-campaign.pbs`
- Modify: `tests/test_red_shirt_polaris_launcher.py`

- [ ] **Step 1: Add failing image and launcher contract tests**

Assert the image copies the catalog CLI executable. Assert the launcher requires `RED_SHIRT_ENV_CATALOG`, requires its `.sha256`, invokes `verify`, copies it into the attempt using mode `0444`, re-verifies the copy, binds the attempt catalog read-only, creates a separate overlay path, exports both paths, and includes catalog checksum/snapshot metadata in `facts.json`.

- [ ] **Step 2: Implement image packaging**

Copy `/opt/red-shirt-polaris/red_shirt_env_catalog.py`, chmod `0555`, and compile it during image build using the pinned Hermes interpreter.

- [ ] **Step 3: Implement campaign staging**

Before watcher startup, verify the operator-provided host catalog and sidecar. Copy both into `$ATTEMPT/environment/`, remove write bits, verify again, and expose the snapshot to the SIF at `/environment/site.sqlite:ro`. The overlay remains under the attempt and is writable only through the attempt bind.

- [ ] **Step 4: Advertise catalog paths without claiming compatibility**

Add `environment_catalog` facts containing site path, checksum, collection status, query CLI, overlay path, and the explicit statement `discovery_only_not_compatibility_proof`. Do not add credentials or arbitrary environment values.

- [ ] **Step 5: Run integration tests and commit**

Run: `python3 -m pytest -q tests/test_red_shirt_polaris_launcher.py tests/test_red_shirt_env_catalog.py`
Expected: PASS.

Commit: `feat(red-shirt): mount environment catalog into campaigns`

### Task 5: Resident contract and operator documentation

**Files:**
- Modify: `scripts/red_shirt_task_context.py`
- Modify: `tests/test_red_shirt_task_context.py`
- Modify: `deploy/polaris/RED_SHIRT_README.md`
- Create: `docs/red-shirt-environment-catalog.md`

- [ ] **Step 1: Add failing generated-context tests**

Assert `AGENTS.md` tells Red Shirt to use the catalog for discovery, inspect provenance/freshness/completeness, preserve contradictory records, record structured attempt observations, and still prove compatibility through compile/link/runtime probes.

- [ ] **Step 2: Update the resident contract**

Add concise catalog instructions without replacing the existing evidence hierarchy or application-ownership language.

- [ ] **Step 3: Document collection and deployment**

Provide exact host commands to collect a bounded broad snapshot, deepen selected modules, finalize/verify, stage campaign tools, set `RED_SHIRT_ENV_CATALOG`, submit PBS, inspect status, refresh stale snapshots, and query from inside Red Shirt. Document that there is no service/container and no network dependency.

- [ ] **Step 4: Document schema and threat model**

Describe entity/relation/observation semantics, evidence levels, incompleteness, immutable site versus writable overlay, prohibited data, parser limitations, freshness, and why static records cannot establish Pepper success.

- [ ] **Step 5: Run documentation/integration tests and commit**

Run: `python3 -m pytest -q tests/test_red_shirt_task_context.py tests/test_red_shirt_polaris_launcher.py tests/test_red_shirt_env_catalog.py`
Expected: PASS.

Commit: `docs(red-shirt): document environment catalog workflow`

### Task 6: Full verification and release review

**Files:**
- Review all files changed since `main`.

- [ ] **Step 1: Run the complete repository suite**

Run: `python3 -m pytest -q`
Expected: all tests pass.

- [ ] **Step 2: Run static and security verification**

Run Python compilation for every changed Python file, `bash -n` on changed shell/PBS files, `git diff --check`, SQLite `quick_check`/`foreign_key_check`/FTS integrity against a real fixture catalog, and a canary scan over the resulting database and JSON output.

- [ ] **Step 3: Exercise the exact CLI workflow**

Build a fixture site snapshot, finalize it, verify it, query FTS and dependency closure, record conflicting overlay observations, and confirm the immutable site checksum is unchanged.

- [ ] **Step 4: Run independent two-stage review**

First request specification compliance review. After it passes, request security/database/code-quality review emphasizing SQL injection, secret retention, partial-collection truthfulness, immutable snapshot handling, graph cycles, path containment, and scientific overclaiming. Resolve every Blocker or Important finding and re-run review.

- [ ] **Step 5: Push a PR and require green CI**

Push via authenticated HTTPS if SSH selects the wrong account. Open a PR, verify checks, merge only after review and green CI, then verify local `main`, `origin/main`, and GitHub `main` are the same SHA and the published Red Shirt image is multi-architecture with matching revision provenance.
