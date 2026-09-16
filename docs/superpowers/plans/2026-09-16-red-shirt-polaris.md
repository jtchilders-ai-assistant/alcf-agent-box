# Red Shirt Polaris Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build, publish, deploy, and verify Red Shirt Polaris: a compute-resident Hermes agent using ALCF inference and authenticated standard A2A v1.0 over userspace Tailscale/Headscale.

**Architecture:** A dedicated multi-stage OCI image extends pinned Hermes `v2026.9.14`, adds pinned Tailscale and the already-tested selective CONNECT rewriter, and launches a non-root supervisor inside an Apptainer/PBS job. Hermes binds standard A2A to loopback; Tailscale Serve exposes only that port to the tailnet. Persistent Hermes/Globus state lives under the user's home filesystem while Tailscale identity and sockets remain job-local and disposable.

**Tech Stack:** Docker/BuildKit, GitHub Actions/GHCR, Apptainer 1.4.1, PBS Pro, Bash, Python 3, Hermes Agent A2A v1.0, Tailscale 1.88.3 userspace networking, Headscale 0.29.3, pytest, ALCF Inference Service.

**Design specification:** `docs/superpowers/specs/2026-09-16-red-shirt-polaris-design.md`

---

## File map

- `Dockerfile.red-shirt-polaris` — immutable compute image; no dashboard entrypoint.
- `.github/workflows/build.yml` — multi-arch `alcf-red-shirt-polaris` publish job.
- `config/red-shirt-polaris/SOUL.md` — compute identity and operating constraints.
- `config/red-shirt-polaris/config.template.yaml` — minimal ALCF+A2A Hermes config.
- `docs/polaris-snapshot/README.md` — source/provenance index.
- `docs/polaris-snapshot/official/*.md` — date-stamped official user-doc snapshots.
- `docs/polaris-snapshot/local/deployment-notes.md` — measured findings, visibly non-policy.
- `scripts/red_shirt_config.py` — fail-closed credential validation, live-model selection, config rendering.
- `scripts/red_shirt_entrypoint.sh` — ordered daemon lifecycle and cleanup.
- `scripts/red_shirt_probe.py` — machine-readable readiness/inference/A2A probes.
- `deploy/polaris/build-red-shirt-sif.sh` — immutable OCI→SIF conversion and checksum.
- `deploy/polaris/red-shirt-polaris.pbs` — resident job launcher.
- `deploy/polaris/RED_SHIRT_README.md` — credential staging, submit, monitor, test, teardown.
- `tests/test_red_shirt_content.py` — image, SOUL, docs, and CI invariants.
- `tests/test_red_shirt_config.py` — renderer and model-selection behavior.
- `tests/test_red_shirt_runtime.py` — executable lifecycle tests with fake daemons.
- `tests/test_red_shirt_polaris_launcher.py` — PBS/SIF launcher invariants.

---

### Task 1: Pin the compute image contract

**Files:**
- Create: `tests/test_red_shirt_content.py`
- Create: `Dockerfile.red-shirt-polaris`
- Modify: `.github/workflows/build.yml`

- [ ] **Step 1: Write failing image-contract tests**

Add tests that require a pinned Hermes base, pinned Tailscale source, non-root final user, dedicated entrypoint, required content copies, and a multi-arch CI target:

```python
from pathlib import Path

ROOT = Path(__file__).parents[1]


def test_compute_image_is_pinned_and_non_root():
    text = (ROOT / "Dockerfile.red-shirt-polaris").read_text()
    assert "nousresearch/hermes-agent:v2026.9.14@sha256:" in text
    assert "tailscale/tailscale:v1.88.3@sha256:" in text
    assert "USER hermes" in text
    assert 'ENTRYPOINT ["/opt/red-shirt-polaris/entrypoint.sh"]' in text


def test_ci_publishes_compute_image_for_both_architectures():
    text = (ROOT / ".github/workflows/build.yml").read_text()
    assert "alcf-red-shirt-polaris" in text
    assert "file: Dockerfile.red-shirt-polaris" in text
    assert "platforms: linux/amd64,linux/arm64" in text
```

- [ ] **Step 2: Run the tests and verify RED**

Run: `pytest -q tests/test_red_shirt_content.py`

Expected: failures because `Dockerfile.red-shirt-polaris` and the CI job do not exist.

- [ ] **Step 3: Resolve and record the immutable Hermes digest**

Query Docker Hub's OCI index and write the returned digest exactly into the Dockerfile base reference. Verify the index contains `linux/amd64` and `linux/arm64`. Do not use `latest` or infer the digest from a local cache.

- [ ] **Step 4: Add the minimal compute Dockerfile and CI job**

The Dockerfile must use this shape:

```dockerfile
ARG HERMES_BASE=nousresearch/hermes-agent:v2026.9.14@sha256:<verified-index-digest>
FROM tailscale/tailscale:v1.88.3@sha256:b2a19f6b6402adc26a2aa8cb90da66afe3061e718ac67ed3f21ec3d4b366439f AS tailscale-src
FROM ${HERMES_BASE}
USER root
COPY --from=tailscale-src /usr/local/bin/tailscale /usr/local/bin/tailscale
COPY --from=tailscale-src /usr/local/bin/tailscaled /usr/local/bin/tailscaled
COPY scripts/connect_proxy.py /opt/red-shirt-polaris/connect_proxy.py
COPY scripts/red_shirt_config.py /opt/red-shirt-polaris/red_shirt_config.py
COPY scripts/red_shirt_probe.py /opt/red-shirt-polaris/red_shirt_probe.py
COPY scripts/red_shirt_entrypoint.sh /opt/red-shirt-polaris/entrypoint.sh
COPY config/red-shirt-polaris/ /opt/red-shirt-polaris/config/
COPY docs/polaris-snapshot/ /opt/red-shirt-polaris/docs/
COPY skills/ /opt/red-shirt-polaris/skills/
RUN chmod 0555 /opt/red-shirt-polaris/entrypoint.sh && chown -R hermes:hermes /opt/red-shirt-polaris
ARG ALCF_GIT_SHA=dev
RUN printf '%s\n' "$ALCF_GIT_SHA" > /opt/red-shirt-polaris/REVISION
ENV HERMES_HOME=/opt/data A2A_HOST=127.0.0.1 A2A_PORT=9900 A2A_AGENT_NAME="Red Shirt Polaris"
USER hermes
WORKDIR /opt/data
ENTRYPOINT ["/opt/red-shirt-polaris/entrypoint.sh"]
```

Add a SHA-scoped multi-arch CI job publishing `ghcr.io/${{ github.repository_owner }}/alcf-red-shirt-polaris` with `ALCF_GIT_SHA=${{ github.sha }}`.

- [ ] **Step 5: Verify GREEN and syntax**

Run: `pytest -q tests/test_red_shirt_content.py && git diff --check`

Expected: all image-contract tests pass.

- [ ] **Step 6: Commit**

```bash
git add Dockerfile.red-shirt-polaris .github/workflows/build.yml tests/test_red_shirt_content.py
git commit -m "build(polaris): add Red Shirt compute image target"
```

---

### Task 2: Add Red Shirt Polaris identity and grounded docs

**Files:**
- Modify: `tests/test_red_shirt_content.py`
- Create: `config/red-shirt-polaris/SOUL.md`
- Create: `docs/polaris-snapshot/README.md`
- Create: `docs/polaris-snapshot/official/*.md`
- Create: `docs/polaris-snapshot/local/deployment-notes.md`

- [ ] **Step 1: Add failing SOUL and documentation-index tests**

Require exact identity, execution locus, ALCF model backend, A2A relationship, proxy constraint, local docs index, citations, source URL, retrieval date, and official/local classification:

```python
def test_soul_states_compute_identity_and_docs_contract():
    soul = (ROOT / "config/red-shirt-polaris/SOUL.md").read_text()
    for phrase in (
        "Red Shirt Polaris", "Apptainer", "PBS job", "Polaris compute node",
        "ALCF Inference Service", "Wesley", "standard A2A",
        "proxy.alcf.anl.gov:3128", "/opt/red-shirt-polaris/docs/README.md",
    ):
        assert phrase in soul
    assert "cite the local source path" in soul.lower()


def test_doc_index_has_provenance_for_every_snapshot():
    index = (ROOT / "docs/polaris-snapshot/README.md").read_text()
    docs = list((ROOT / "docs/polaris-snapshot").glob("*/*.md"))
    assert docs
    for doc in docs:
        assert str(doc.relative_to(ROOT / "docs/polaris-snapshot")) in index
    assert "Canonical URL" in index and "Retrieved" in index and "Classification" in index
```

- [ ] **Step 2: Run the tests and verify RED**

Run: `pytest -q tests/test_red_shirt_content.py`

Expected: missing SOUL and snapshot index failures.

- [ ] **Step 3: Fetch official documentation reproducibly**

Use direct canonical ALCF docs URLs and store cleaned Markdown snapshots. At minimum include system overview/getting started, PBS, filesystems, modules/programming environment, containers/Apptainer, and node-local storage. Record the canonical URL and retrieval date `2026-09-16` in `docs/polaris-snapshot/README.md`. If a page is blocked or unavailable, use the blocked-page recovery workflow and record the actual source; never synthesize missing official text.

- [ ] **Step 4: Write the SOUL and local deployment note**

The SOUL must be direct identity/operating guidance, not Star Trek role-play. Include:

```markdown
You are Red Shirt Polaris, a Hermes agent running inside an Apptainer container in a PBS job on a Polaris compute node at ALCF.

Before giving Polaris-specific instructions or taking a Polaris-specific action, read `/opt/red-shirt-polaris/docs/README.md` and the relevant bundled source. Cite the local source path in substantive answers. Distinguish official snapshots from locally measured deployment notes; local measurements are not ALCF policy.
```

Document measured DERP-only behavior, userspace Tailscale, real-port probing, and the non-authoritative nature of local findings in `local/deployment-notes.md`.

- [ ] **Step 5: Verify GREEN**

Run: `pytest -q tests/test_red_shirt_content.py && git diff --check`

Expected: all content tests pass and every snapshot appears in the index.

- [ ] **Step 6: Commit**

```bash
git add config/red-shirt-polaris docs/polaris-snapshot tests/test_red_shirt_content.py
git commit -m "feat(polaris): ground Red Shirt identity in bundled docs"
```

---

### Task 3: Implement fail-closed configuration and live-model selection

**Files:**
- Create: `tests/test_red_shirt_config.py`
- Create: `scripts/red_shirt_config.py`
- Create: `config/red-shirt-polaris/config.template.yaml`

- [ ] **Step 1: Write failing renderer tests**

Use temporary secret files and fixture catalog/jobs JSON. Cover mode `0600`, minimum token length, absent/offline preferred model, deterministic live fallback, no-live-model failure, A2A trusted identity, outbound Wesley registration, and absence of secret values from stdout/stderr:

```python
def test_selects_live_model_in_preference_order(tmp_path):
    result = run_renderer(tmp_path, catalog=CATALOG, jobs=JOBS,
                          preferred="offline/model", preferences=["live/model"])
    assert result.returncode == 0
    config = yaml.safe_load((tmp_path / "home/config.yaml").read_text())
    assert config["model"]["model"] == "live/model"


def test_refuses_when_no_usable_live_model(tmp_path):
    result = run_renderer(tmp_path, catalog=CATALOG, jobs={"running": []})
    assert result.returncode == 78
    assert "no live >=64000-token chat model" in result.stderr.lower()
```

- [ ] **Step 2: Run and verify RED**

Run: `pytest -q tests/test_red_shirt_config.py`

Expected: import/file-not-found failures because the renderer is absent.

- [ ] **Step 3: Implement `red_shirt_config.py`**

Expose explicit subcommands:

```text
red_shirt_config.py validate-secrets --headscale-key FILE --inbound-a2a FILE --outbound-a2a FILE
red_shirt_config.py render --home DIR --template FILE --token-helper FILE --catalog-fixture FILE? --jobs-fixture FILE?
```

Implementation requirements:

- validate regular files, ownership readability, exact `0600`, nonempty values, and minimum 16-character A2A tokens;
- obtain the inference token via the helper without printing it;
- query cluster catalogs/jobs through the ALCF proxy;
- keep only chat models with real context length `>=64000`;
- choose the requested model only when LIVE, otherwise choose the first LIVE model from an explicit ordered preference list;
- return exit `78` if none is live;
- render provider-specific context/output caps and `strip_tool_message_name`;
- render `gateway.platforms.a2a.enabled: true`, loopback port 9900, `a2a.trusted_peers: [wesley]`, and `a2a_agents.wesley`;
- write secrets only to `$HERMES_HOME/.env` with mode `0600`; config references environment expansion or secret-scope lookup rather than embedding A2A tokens;
- atomically replace generated files; and
- print only model/provider names and paths, never credential values.

- [ ] **Step 4: Run targeted tests and verify GREEN**

Run: `pytest -q tests/test_red_shirt_config.py`

Expected: all renderer tests pass.

- [ ] **Step 5: Exercise the real Hermes config loader**

Run with a temporary `HERMES_HOME` and fixture inputs, then execute the pinned/local Hermes config loader against the generated file. Assert the A2A platform and custom provider are present and the YAML contains no unresolved `${...}` placeholders.

- [ ] **Step 6: Commit**

```bash
git add scripts/red_shirt_config.py config/red-shirt-polaris/config.template.yaml tests/test_red_shirt_config.py
git commit -m "feat(polaris): render live fail-closed Hermes configuration"
```

---

### Task 4: Implement runtime probes and userspace A2A routing

**Files:**
- Create: `tests/test_red_shirt_runtime.py`
- Create: `scripts/red_shirt_probe.py`
- Modify: `scripts/connect_proxy.py` only if exact-authority forwarding needs a tested extension

- [ ] **Step 1: Write failing probe tests**

Test real localhost fake servers rather than static regexes. Require:

- Agent Card probe through an explicitly supplied proxy;
- unauthenticated A2A negative control expects 401;
- authenticated `SendMessage` call parses nonempty reply;
- inference smoke rejects HTTP 200 with null/empty content;
- JSON readiness record is valid and contains no supplied token;
- proxy routing chooses Tailscale only for exact Wesley tailnet authority and ALCF proxy for inference.

```python
def test_ready_record_never_contains_tokens(tmp_path):
    token = "unique-secret-value-12345"
    result = run_probe(tmp_path, inbound_token=token)
    assert token not in result.stdout
    payload = json.loads(result.stdout)
    assert payload["overall_ok"] is True
```

- [ ] **Step 2: Run and verify RED**

Run: `pytest -q tests/test_red_shirt_runtime.py`

Expected: missing probe implementation failures.

- [ ] **Step 3: Implement `red_shirt_probe.py`**

Use stdlib HTTP/JSON and explicit proxy handlers. Subcommands:

```text
probe inference --base-url URL --model ID --token-file FILE
probe card --url URL --proxy URL
probe a2a-negative --url URL
probe a2a-send --url URL --token-file FILE --message TEXT --proxy URL
probe ready-record --output FILE ...
```

Use A2A v1.0 `SendMessage` JSON-RPC with a generated context/message ID, parse artifact or status text, and reject empty content. Use bearer token files without placing contents in argv or logs.

- [ ] **Step 4: Verify whether Hermes/urllib honors the Tailscale outbound proxy**

Run the standard A2A client path against a localhost fake SOCKS/HTTP proxy. If `urllib` honors explicit HTTP proxying for `http://100.64.0.2:9900`, configure only that exact call with the Tailscale outbound HTTP proxy. If it does not, extend the existing proxy into an exact-authority loopback forwarder with live-socket tests. Do not set a global proxy that captures inference.

- [ ] **Step 5: Run tests and verify GREEN**

Run: `pytest -q tests/test_red_shirt_runtime.py tests/test_headscale_probe_files.py`

Expected: all runtime and existing CONNECT-rewriter tests pass.

- [ ] **Step 6: Commit**

```bash
git add scripts/red_shirt_probe.py scripts/connect_proxy.py tests/test_red_shirt_runtime.py
git commit -m "feat(polaris): add authenticated A2A and inference probes"
```

---

### Task 5: Implement the supervised compute entrypoint

**Files:**
- Modify: `tests/test_red_shirt_runtime.py`
- Create: `scripts/red_shirt_entrypoint.sh`

- [ ] **Step 1: Add failing lifecycle tests**

Create fake `tailscaled`, `tailscale`, `hermes`, and probe executables that append events to a log. Assert exact startup order, stop-on-failed-gate behavior, signal cleanup, `tailscale logout`, Serve removal, job-local-root removal, terminal JSON output, and no secret output.

```python
def test_runtime_orders_transport_before_hermes(fake_runtime):
    result = fake_runtime.run()
    assert result.returncode == 0
    assert fake_runtime.events[:6] == [
        "connect-proxy-ready", "tailscaled-ready", "tailscale-up",
        "tailscale-running", "tailscale-serve", "inference-smoke",
    ]
    assert "hermes-gateway" in fake_runtime.events
```

- [ ] **Step 2: Run and verify RED**

Run: `pytest -q tests/test_red_shirt_runtime.py -k lifecycle`

Expected: entrypoint missing.

- [ ] **Step 3: Implement the entrypoint**

Use a process registry rather than `pkill`/`pgrep`. Required skeleton:

```bash
#!/usr/bin/env bash
set -euo pipefail
umask 077
PIDS=()
cleanup() {
  rc=$?
  set +e
  tailscale --socket="$TS_SOCKET" serve reset
  tailscale --socket="$TS_SOCKET" logout
  for pid in "${PIDS[@]}"; do kill -TERM "$pid" 2>/dev/null; done
  for pid in "${PIDS[@]}"; do wait "$pid" 2>/dev/null; done
  rm -rf -- "$JOB_ROOT"
  python3 /opt/red-shirt-polaris/red_shirt_probe.py terminal-record --exit-code "$rc" --output "$TERMINAL_RECORD"
  exit "$rc"
}
trap cleanup EXIT INT TERM
```

Then implement the approved twelve-gate startup sequence. Wait on real sockets/HTTP/status with bounded deadlines; do not use blind fixed sleeps as readiness evidence. Launch `hermes gateway` only after inference smoke passes. Keep the entrypoint in the foreground waiting on the Hermes PID and terminate if any required child dies.

- [ ] **Step 4: Verify lifecycle GREEN and shell syntax**

Run: `pytest -q tests/test_red_shirt_runtime.py && bash -n scripts/red_shirt_entrypoint.sh`

Expected: lifecycle tests pass and shell syntax exits 0.

- [ ] **Step 5: Commit**

```bash
git add scripts/red_shirt_entrypoint.sh tests/test_red_shirt_runtime.py
git commit -m "feat(polaris): supervise Red Shirt runtime lifecycle"
```

---

### Task 6: Add reproducible SIF build and PBS resident launcher

**Files:**
- Create: `tests/test_red_shirt_polaris_launcher.py`
- Create: `deploy/polaris/build-red-shirt-sif.sh`
- Create: `deploy/polaris/red-shirt-polaris.pbs`
- Create: `deploy/polaris/RED_SHIRT_README.md`

- [ ] **Step 1: Write failing launcher tests**

Require no hardcoded `#PBS -A`, no `qsub -v` credential path, immutable image tag/digest placeholders, checksum verification, module order, bounded SquashFS resources, `/local/scratch`, existing bind destinations, read-only credential mounts, signal trap, and terminal-record preservation.

```python
def test_pbs_keeps_allocation_out_of_directives():
    text = PBS.read_text()
    assert "#PBS -A" not in text
    assert "qsub -v" not in text
    assert "sha256sum -c" in text
    assert "/local/scratch" in text
```

- [ ] **Step 2: Run and verify RED**

Run: `pytest -q tests/test_red_shirt_polaris_launcher.py`

Expected: missing launcher/build files.

- [ ] **Step 3: Implement SIF builder**

Follow the verified module order and resource bounds:

```bash
ml use /soft/modulefiles
ml spack-pe-base
ml apptainer
export APPTAINER_TMPDIR="/local/scratch/$USER/red-shirt-build-${PBS_JOBID%%.*}/tmp"
export APPTAINER_CACHEDIR="/local/scratch/$USER/red-shirt-build-${PBS_JOBID%%.*}/cache"
apptainer build --force --mksquashfs-args "-processors 4 -mem 4G" "$SIF" "docker://$IMAGE"
sha256sum "$SIF" > "$SIF.sha256"
apptainer exec "$SIF" hermes --version
apptainer exec "$SIF" tailscale version
```

Pin `IMAGE` to `sha-<commit>` plus recorded OCI digest after CI publishes it.

- [ ] **Step 4: Implement PBS launcher and runbook**

Use explicit `qsub -A datascience deploy/polaris/red-shirt-polaris.pbs` in docs, not in directives. Validate each credential file by metadata only. Mount persistent home at `/opt/data`, public CA and secret directory read-only, and job-local root at an existing SIF destination. Capture stdout/stderr and readiness/terminal JSON under `$HOME/red-shirt-polaris/runs/$PBS_JOBID/`.

- [ ] **Step 5: Verify GREEN**

Run: `pytest -q tests/test_red_shirt_polaris_launcher.py && bash -n deploy/polaris/build-red-shirt-sif.sh deploy/polaris/red-shirt-polaris.pbs`

Expected: all launcher tests pass.

- [ ] **Step 6: Commit**

```bash
git add deploy/polaris tests/test_red_shirt_polaris_launcher.py
git commit -m "feat(polaris): add Red Shirt SIF and PBS deployment"
```

---

### Task 7: Integrate, test, and independently review the branch

**Files:**
- Modify only files required by review findings.

- [ ] **Step 1: Run the complete local verification suite**

Run:

```bash
pytest -q
bash -n scripts/*.sh deploy/polaris/*.sh deploy/polaris/*.pbs
git diff --check main...HEAD
git status --short
```

Expected: zero test failures, syntax errors, whitespace errors, or untracked credentials.

- [ ] **Step 2: Run an offline end-to-end runtime smoke test**

Use fake Headscale/Tailscale, fake ALCF catalog/jobs/chat endpoint, and a real local Hermes config loader. Exercise startup→READY→authenticated A2A→signal→terminal record. Assert the event log and JSON records; do not accept unit tests alone.

- [ ] **Step 3: Run static secret and dangerous-shell scans on added lines**

Inspect `git diff main...HEAD` for literal tokens/passwords, `eval`, unsafe `shell=True`, broad `pkill`, secret-bearing argv/env, `curl -k`, and unverified downloads. Any match requires manual disposition.

- [ ] **Step 4: Dispatch exact-commit spec review**

Give an independent reviewer the design spec, plan, base SHA, head SHA, and full diff. Require explicit pass/fail for every objective, security boundary, teardown gate, and test requirement.

- [ ] **Step 5: Fix findings test-first and re-review**

For each valid finding: add a failing regression test, verify RED, implement the minimal fix, verify GREEN, rerun the full suite, and send the new exact commit to a fresh reviewer.

- [ ] **Step 6: Dispatch code-quality/security review**

Only after spec compliance passes, run independent code-quality/security review of the exact commit. Resolve all important/security findings and re-review.

- [ ] **Step 7: Commit final review fixes**

```bash
git add <reviewed-files>
git commit -m "fix(polaris): address Red Shirt deployment review"
```

---

### Task 8: Merge, publish, and verify immutable OCI artifacts

**Files:**
- Modify: `deploy/polaris/build-red-shirt-sif.sh` and docs only to replace the post-publish image placeholders with the immutable commit tag/digest.

- [ ] **Step 1: Merge reviewed increments to `main`**

Fast-forward only after reviews and tests pass. Push with the repository's pinned GitHub identity.

- [ ] **Step 2: Watch the exact GitHub Actions run**

Run `gh run list` filtered by the merged SHA, then `gh run watch <id> --exit-status`. Inspect logs for both architectures and the `alcf-red-shirt-polaris:sha-<short>` push.

- [ ] **Step 3: Read back the GHCR index and platform configs**

Use an anonymous pull token. Assert:

- media type is OCI index/manifest list;
- platforms include exactly the promised `linux/amd64` and `linux/arm64` (ignoring attestations);
- `org.opencontainers.image.revision` equals the merged commit; and
- the index digest is recorded.

- [ ] **Step 4: Pin the published commit tag and digest in deployment files**

Update the SIF builder/runbook with the actual immutable values, rerun launcher tests, commit, push, watch CI, and repeat registry readback. The final deployment commit—not an earlier provisional image—must be the recorded artifact.

- [ ] **Step 5: Verify repository equality**

Print and compare local `HEAD`, `origin/main`, and GitHub `main`; require exact equality before remote deployment.

---

### Task 9: Stage credentials safely and close the Headscale cleanup gate

**Files:**
- Update: `deploy/polaris/STATUS.md` with non-secret readback only.

- [ ] **Step 1: Verify current Headscale server state**

From an authorized management path, run the exact pinned Headscale CLI's version/help/list commands. Expire the old probe key if present, delete residual `probe-*` nodes, and list again. Never infer cleanup from logout logs alone.

- [ ] **Step 2: Create deployment credentials**

Create a short-lived reusable+ephemeral Headscale key and two independently generated A2A tokens. Configure Wesley inbound identity `red-shirt-polaris`; stage Polaris inbound identity `wesley`. Never paste values into chat.

- [ ] **Step 3: Stage Polaris files through hidden input**

Use a hidden `getpass` prompt on Polaris to create exact mode-`0600` files under `$HOME/red-shirt-polaris/secrets/`. Verify only regular-file status, owner, mode, nonzero size, and mtime.

- [ ] **Step 4: Verify ALCF token store without displaying tokens**

Run the helper's expiry/status command and a real gateway status request. If the 30-day session has expired, stop for the user's interactive Globus reauthentication rather than attempting automation.

- [ ] **Step 5: Record cleanup/staging status**

Update STATUS with key IDs/node IDs and state but no key material, hashes, or token lengths that add no operational value.

---

### Task 10: Build the SIF and launch Red Shirt Polaris

**Files:**
- Update: `deploy/polaris/STATUS.md` after measured results.

- [ ] **Step 1: Transfer only reviewed deployment artifacts to Polaris**

Use the existing human-opened Polaris ControlMaster. Check it with `ssh -O check polaris-login-04`; never close it. Copy the builder and PBS launcher to `$HOME/red-shirt-polaris/deploy/`.

- [ ] **Step 2: Submit the SIF build job**

Submit with `qsub -A datascience`. Poll `qstat -xf` to terminal state; inspect `Exit_status`, logs, SIF checksum, and in-SIF `hermes --version`/`tailscale version`.

- [ ] **Step 3: Submit the resident job**

Submit `red-shirt-polaris.pbs` with `-A datascience`. Poll until READY or terminal failure. On failure, read readiness/terminal JSON and PBS output before modifying anything.

- [ ] **Step 4: Read back runtime state**

Require the READY record to show the expected commit/SIF hash, compute hostname, `BackendState=Running`, tailnet address, selected LIVE ALCF model, successful inference smoke, local Agent Card readiness, and Serve status. No secret values may appear.

---

### Task 11: Verify authenticated standard A2A in both directions

**Files:**
- Update: `deploy/polaris/STATUS.md`
- Preserve raw non-secret evidence under the Polaris run directory.

- [ ] **Step 1: Verify Red Shirt's Agent Card from Wesley**

Fetch `http://<red-shirt-tailnet-ip>:9900/.well-known/agent-card.json` through Wesley's tailnet stack. Confirm name `Red Shirt Polaris`, standard JSON-RPC interface, and advertised tailnet URL.

- [ ] **Step 2: Verify unauthorized rejection**

Send `SendMessage` with no bearer token and with a deliberately wrong generated token. Require HTTP 401 in both cases.

- [ ] **Step 3: Send Wesley → Red Shirt identity/documentation task**

Use Hermes's standard A2A client with Wesley's Polaris credential. Ask Red Shirt to state its execution locus and answer a Polaris-specific question after consulting bundled docs. Require a nonempty response naming the compute/PBS/Apptainer context and citing a real local docs path.

- [ ] **Step 4: Prove the reply used ALCF inference**

Read the Red Shirt gateway/A2A audit and inference-side request evidence available locally. Correlate task/context IDs and timestamps. The agent's textual claim alone is not proof.

- [ ] **Step 5: Send Red Shirt → Wesley task**

Invoke the standard Hermes A2A client inside the Polaris container through the userspace Tailscale path. Ask Wesley to return a unique nonce supplied in the request. Require the exact nonce in the authenticated response and verify Wesley-side audit/session evidence.

- [ ] **Step 6: Record the acceptance matrix**

Write every gate, HTTP status, task/context identifier, model, relevant timestamps, and evidence path to STATUS. Do not record bearer values.

---

### Task 12: Verify teardown, queue hygiene, and finalize documentation

**Files:**
- Modify: `deploy/polaris/STATUS.md`
- Modify: `deploy/polaris/RED_SHIRT_README.md`

- [ ] **Step 1: Preserve evidence before termination**

Copy readiness/terminal records, A2A audit excerpts, inference smoke result, and PBS output into the persistent run directory.

- [ ] **Step 2: Terminate and verify PBS state**

Issue `qdel <jobid>`, re-poll `qstat`. If still running, use `qdel -W force`, re-poll, and report continued `R` honestly. Do not equate exit 0 with deletion.

- [ ] **Step 3: Verify Headscale and local cleanup**

List Headscale nodes and ensure the disposable Red Shirt node is absent or delete it by live numeric identifier. Verify the job-local `/local/scratch` root is absent. Expire the join key when deployment retries are finished.

- [ ] **Step 4: Audit the PBS queue**

Run `qstat -u parton`, identify held/stray jobs created during testing, remove them explicitly, and re-poll.

- [ ] **Step 5: Final verification**

Run local full tests and syntax checks again. Verify local `HEAD == origin/main == GitHub main`, CI green for the final commit, GHCR revision label equality, and the documented SIF checksum.

- [ ] **Step 6: Final documentation commit**

```bash
git add deploy/polaris/STATUS.md deploy/polaris/RED_SHIRT_README.md
git commit -m "docs(polaris): record verified Red Shirt deployment"
git push origin main
```

- [ ] **Step 7: Retire the worktree**

After final main/GitHub equality and no needed branch-only commits, remove the feature worktree and delete the merged feature branch.
