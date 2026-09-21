# Red Shirt Resident Reliability Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make Red Shirt reliably complete bounded scientific campaigns by giving it a coherent runtime contract, focused resident skills, fresh inference authorization, durable phase/terminal evidence, and a verified Polaris compiler/MPI/CUDA/Kokkos boundary.

**Architecture:** Keep stable identity and evidence principles in `SOUL.md`; generate attempt-specific `AGENTS.md`, `ENV.md`, and `STATUS.json` beside each task; install only a curated resident skill bundle; and make the outer campaign wrapper own authentication readiness and failure synthesis. Keep application dependency selection, installation, build, tests, execution, and scientific analysis under Red Shirt's control. Treat Apptainer as packaging rather than a security sandbox while retaining Unix-account and PBS-allocation boundaries.

**Tech Stack:** Bash, Python 3, pytest, Hermes Agent, Apptainer, PBS Pro, HPE PALS/Cray MPICH, CUDA, Kokkos, CMake, Ninja, GHCR multi-arch CI.

---

## Acceptance invariants

- Red Shirt, not the orchestrator, owns application dependencies, configuration, build, tests, run, and analysis.
- Infrastructure may expose and verify the host compiler/MPI/GPU boundary but may not silently perform the application task.
- A launch acknowledgement is never treated as command completion.
- Requested CMake flags are not proof of configured, compiled, linked, or executed features.
- Every attempt leaves durable terminal evidence even if Hermes inference fails.
- No credentials or secret values appear in source, generated context, logs, process arguments, or terminal records.
- Every coupled-stack claim is backed by a production-equivalent probe.
- Release artifacts are immutable and provenance-checked; the Red Shirt image is multi-arch.

## File map

Expected paths may be refined after the reconciliation inventory, but responsibilities must remain separate:

- Modify `config/red-shirt-polaris/SOUL.md`: durable execution, evidence, and failure-reporting principles.
- Modify `scripts/red_shirt_entrypoint.sh`: deterministic curated skill seeding and reusable authentication/inference readiness entry point.
- Create `config/red-shirt-polaris/skills/*/SKILL.md` or an equivalently dedicated image subtree: curated resident skills only.
- Create `scripts/red_shirt_task_context.py`: generate and validate `AGENTS.md`, `ENV.md`, and initial `STATUS.json` atomically without secrets.
- Create `scripts/red_shirt_campaign.py` or an equivalent focused wrapper: prepare an attempt, refresh/smoke inference, invoke Hermes, and synthesize wrapper failure records.
- Extend `scripts/red_shirt_mpi_env.sh`: emit machine-readable host environment facts needed by `ENV.md` and the bridge.
- Promote/refactor the useful `.tmp-red-shirt-host-{bridge,watcher,client}.sh` and rank-wrapper logic into stable named scripts only after tests define their protocol.
- Replace `.tmp-red-shirt-gpu-campaign.pbs` with a maintained bounded campaign launcher or template; do not ship attempt numbers or machine-local transient paths.
- Extend `tests/test_red_shirt_content.py`, `tests/test_red_shirt_release_contract.py`, `tests/test_red_shirt_runtime.py`, `tests/test_red_shirt_mpi_bridge.py`, and `tests/test_red_shirt_polaris_launcher.py`.
- Create focused tests for context generation, campaign failure synthesis, curated skill installation, and coupled toolchain contracts.
- Update `deploy/polaris/RED_SHIRT_README.md` and relevant design/runbook documentation from verified behavior.

## Phase 0: Reconcile the existing multi-node branch

- [ ] **Inventory tracked and untracked work**

Record `git status`, branch/base SHAs, tracked diffs, and every untracked `.tmp-*` artifact. Classify each artifact as one of: stable product code to promote, test/evidence fixture to retain, historical runtime evidence to archive outside git, or disposable scratch.

- [ ] **Verify the existing tracked MPI fixes**

Run:

```bash
pytest -q tests/test_red_shirt_mpi_bridge.py tests/test_red_shirt_polaris_launcher.py tests/test_red_shirt_content.py
bash -n scripts/red_shirt_mpi_env.sh deploy/polaris/red-shirt-polaris.pbs deploy/polaris/red-shirt-mpi-acceptance.pbs
git diff --check
```

Require zero failures before preserving the increment.

- [ ] **Commit the verified tracked fixes as one logical increment**

The commit includes only the four already tracked files unless inspection proves another file is inseparable. Do not add `.tmp-*` files wholesale.

- [ ] **Record the untracked artifact disposition**

Write a short inventory under `docs/superpowers/plans/` or the implementation log section of this plan. Preserve commands and findings, but redact all credential material.

## Phase 1: Durable resident context contract

### Task 1: Tighten SOUL without adding volatile environment facts

- [ ] Write failing assertions in `tests/test_red_shirt_content.py` requiring these durable rules:
  - Apptainer is a packaging boundary, not the user-level security boundary.
  - Red Shirt owns application install/build/test/run/analysis.
  - A background launch is not completion; collect the terminal result of every decisive command.
  - Contradictory evidence blocks success.
  - Distinguish requested configuration, detected configuration, compiled/link evidence, and runtime evidence.
  - Always leave an honest terminal checkpoint on failure or time exhaustion.
- [ ] Run the focused test and verify the new assertions fail for the intended missing language.
- [ ] Add minimal non-roleplay wording to `SOUL.md` and rerun the test to green.
- [ ] Commit the SOUL/test increment.

### Task 2: Generate per-attempt AGENTS.md, ENV.md, and STATUS.json

- [ ] Write failing tests for a context generator requiring:
  - atomic writes and mode-safe files;
  - no overwrite of a non-generated `AGENTS.md`;
  - explicit task root, write boundary, bridge commands/schemas, timeout units, process discipline, checkpoints, evidence hierarchy, time-budget behavior, and completion contract;
  - observed PBS/SIF/Hermes/compiler/MPI/CUDA/Kokkos facts in `ENV.md`, labeled as facts rather than compatibility proof;
  - secret-name and secret-value redaction;
  - an initial valid `STATUS.json` at phase `discovery`.
- [ ] Verify RED.
- [ ] Implement `scripts/red_shirt_task_context.py` with a small CLI and deterministic output.
- [ ] Verify GREEN plus Python compilation and malformed-input failure cases.
- [ ] Integrate generation before the one-shot Hermes invocation and verify that `hermes --in <task>` sees `AGENTS.md` from that exact directory.
- [ ] Commit generator, integration, tests, and generated-file schema documentation.

## Phase 2: Curated resident skills and deterministic seeding

Implement and test one skill at a time. For each skill: run a baseline pressure/application scenario without the skill, capture the failure mode, write the minimal skill, rerun the same scenario, then close observed loopholes before proceeding.

### Task 3: `polaris-resident-build`

- [ ] Baseline-test toolchain discovery, module/container separation, CMake-cache interpretation, and task-local dependency installation.
- [ ] Add the skill with valid frontmatter and concise trigger text.
- [ ] Verify it does not claim that requested flags prove a configured feature.
- [ ] Commit the tested skill and its fixture/tests.

### Task 4: `scientific-evidence-contract`

- [ ] Baseline-test contradictory logs, dirty source, missing raw data, and pressure to report success.
- [ ] Add evidence hierarchy, raw-log/provenance requirements, and honest `DONE`/`FAILED` semantics.
- [ ] Verify the model refuses unsupported numerical claims and preserves the first unresolved error.
- [ ] Commit the tested skill and its fixture/tests.

### Task 5: `long-command-process-discipline`

- [ ] Baseline-test a command promoted after a timeout and pressure to rerun it.
- [ ] Add seconds-based timeout guidance, exact-handle polling, and no-duplicate-execution rules.
- [ ] Verify launch acknowledgement is not reported as completion.
- [ ] Commit the tested skill and its fixture/tests.

### Task 6: `polaris-mpi-apptainer`

- [ ] Baseline-test host `mpiexec --no-transfer -> apptainer exec`, exact `$PBS_NODEFILE`, PALS/PMI inheritance, and local-rank GPU assignment.
- [ ] Add production-equivalent bridge usage and prohibit a container-local `mpiexec` substitute.
- [ ] Verify rank/host/GPU evidence requirements.
- [ ] Commit the tested skill and its fixture/tests.

### Task 7: Deterministic curated skill installation

- [ ] Write a failing release-contract test proving the image copies only the curated Red Shirt skill subtree and that `managed_seed` recursively handles skill directories.
- [ ] Verify RED against the current broad `COPY skills/` and regular-file-only seeding path.
- [ ] Implement recursive managed seeding with per-file stamps and preservation of user-modified files.
- [ ] Remove unrelated Apple/media/design/office/social skills from the Red Shirt image payload without changing the laptop image.
- [ ] Verify fresh-home install, unchanged-image refresh, user-edit preservation, deletion/rename behavior, and valid skill frontmatter.
- [ ] Commit the curated bundle and seeding increment.

## Phase 3: Campaign authorization and terminal failure guarantees

### Task 8: Fresh authorization and real inference smoke per campaign

- [ ] Write failing tests proving a campaign may not invoke Hermes until the combined token helper refreshes/obtains the inference token, renders current config, and a real nonempty-content probe succeeds with a reasoning-safe output budget.
- [ ] Require the token to be passed by protected file/reference, never argv, prompt, logs, `AGENTS.md`, or `ENV.md`.
- [ ] Verify RED against the direct `/opt/hermes/bin/hermes --yolo` launch.
- [ ] Extract/reuse the entrypoint readiness logic rather than cloning a divergent token workflow.
- [ ] Verify failed refresh, 401, 503, empty content, and timeout all prevent Hermes launch with classified non-secret evidence.
- [ ] Commit authorization gating and tests.

### Task 9: Wrapper-generated terminal artifacts

- [ ] Write failing tests for Hermes exit before `REPORT.md`, `RESULT.json`, or a marker exists.
- [ ] Define a wrapper-owned record such as `WRAPPER_RESULT.json` containing Hermes exit code, classified provider/runtime error, current phase, last successful checkpoint, and presence of required artifacts.
- [ ] Synthesize `FAILED`, `REPORT.md`, and `RESULT.json` only when absent; mark them explicitly as wrapper-generated and never overwrite agent-generated artifacts.
- [ ] Use atomic writes and guarantee exactly one terminal marker.
- [ ] Verify 401, signal, timeout, malformed result, and successful-agent cases.
- [ ] Commit wrapper failure synthesis and tests.

## Phase 4: Structured bridge and coherent scientific-stack preflight

### Task 10: Promote the attempt-local bridge to stable product scripts

- [ ] Convert observed bridge defects into failing tests: malformed JSON, Python 3.6 nonce compatibility, unset optional fields under `set -u`, worker exit without response, stable helper paths, empty STOP sentinel, bounded shutdown, atomic script replacement, and nonce-correlated responses.
- [ ] Verify RED against the promoted initial implementation or test fixture.
- [ ] Create stable bridge/client/watcher/rank-wrapper scripts with actions `env_report`, `run_script`, and `run8` (or equally explicit names).
- [ ] Return nonce, action, exit code, start/end timestamps, stdout/stderr paths, script checksum, and environment profile ID.
- [ ] Preserve arbitrary user-level application control while enforcing only path/allocation/protocol safety boundaries.
- [ ] Verify GREEN and commit.

### Task 11: Validate the coupled compiler/MPI/CUDA/Kokkos contract

- [ ] Write a failing acceptance contract requiring one selected environment to prove, in order:
  1. C++20 `<concepts>` compilation;
  2. native MPI compile/link and two-rank execution;
  3. complete MPI/GTL dynamic-link resolution;
  4. selected CUDA runtime satisfies MPI/GTL dependencies;
  5. Kokkos reports CUDA, CUDA lambda, CUDA constexpr, and Ampere 80 support;
  6. minimal Kokkos CUDA program builds and runs through the production rank path;
  7. Pepper configure-only reaches its required feature checks.
- [ ] Implement a probe that records exact modules, wrapper targets, versions, paths, cache/config output, link audit, and stage results without installing/building Pepper for the resident.
- [ ] Fail closed at the first broken boundary and expose that evidence through `ENV.md`; never describe the stack as “tested” from a partial probe.
- [ ] Run the static/local fixture suite and commit.

## Phase 5: Task-only prompt and acceptance semantics

### Task 12: Replace the campaign prompt

- [ ] Write failing tests requiring the prompt to contain only the scientific goal, immutable constraints, source revision, output requirements, and instruction to read `AGENTS.md`/`ENV.md`.
- [ ] Require absence of brittle prescriptions such as preferring external Kokkos 4.6.02 or claiming the environment is tested before the coupled probe passes.
- [ ] Add the approved Pepper task contract: 8 ranks, 2 nodes, 4 A100s/node, fixed-seed Drell-Yan `ppee`, 13.6 TeV, at least 20,000 accepted events, raw output, and three labeled PNG+CSV/JSON histograms.
- [ ] Require a ten-minute finalization reserve and phase checkpoints.
- [ ] Verify RED, implement the reduced prompt, then verify GREEN.
- [ ] Commit prompt/template and tests.

## Phase 6: Local verification and review

### Task 13: Full local gates

- [ ] Run all Red Shirt tests, then the repository test suite appropriate to the changed surface.
- [ ] Run `bash -n` on every changed shell/PBS file and `python -m py_compile` on every changed Python file.
- [ ] Run `git diff --check` and inspect `git status --short`.
- [ ] Search the complete diff for credential patterns, transient paths, attempt numbers, unredacted bearer values, and broad `COPY skills/` regressions.
- [ ] Exercise a fake campaign end to end for success, inference 401, Hermes crash, timeout, and partial-artifact cases.
- [ ] Request independent specification and code-quality reviews; block on Critical/Important findings and repeat review after fixes.

## Phase 7: Release and Polaris acceptance

### Task 14: Integrate and publish exact image

- [ ] Rebase/merge reviewed increments without losing the existing multi-node history.
- [ ] Push with the `jtchilders-ai-assistant` identity and require CI green.
- [ ] Resolve the exact Red Shirt image digest from the Red Shirt workflow target, not the sibling laptop image.
- [ ] Verify the GHCR index contains linux/amd64 and linux/arm64 and its revision label equals the merged commit.

### Task 15: Build and verify pinned SIF on Polaris

- [ ] Verify `polaris-cm status --json`; stop if no human-owned master is usable.
- [ ] Build the SIF from the immutable digest with bounded mksquashfs resources and record OCI digest plus SIF SHA-256.
- [ ] Execute production startup CLIs inside the exact SIF with production-equivalent binds.
- [ ] Verify image revision, Hermes version, curated skills, context generator, token helper, and real inference smoke.

### Task 16: Run bounded acceptance ladder

- [ ] Run bridge-only two-node MPI acceptance.
- [ ] Run the coupled compiler/MPI/CUDA/Kokkos/Apple-of-Pepper configure probe.
- [ ] Run one bounded resident Pepper campaign with one active PBS job and capped submissions.
- [ ] Require fresh auth before each one-shot, generated context, phase checkpoints, terminal artifacts, raw logs/data, exact 8-rank/8-GPU evidence, and outer mechanical validation.
- [ ] On failure, report the terminal layer and preserved evidence; do not silently resubmit beyond the cap.

## Phase 8: Completion and cleanup

### Task 17: Prove final repository and runtime state

- [ ] Re-run all local gates from a clean checkout of the merged SHA.
- [ ] Verify local main SHA equals origin/main and GitHub main.
- [ ] Verify the accepted SIF and GHCR image both identify that exact SHA.
- [ ] Update the Red Shirt runbook with only observed commands/results and classify remaining limitations.
- [ ] Remove/archive disposable `.tmp-*` files, retire superseded branches/worktrees, and preserve only durable evidence.
- [ ] Record final campaign result and next action in durable task progress.

## Current reconciliation inventory (2026-09-20)

- Main: `e93ee21d294788d88a4ffc83df87c3096d9b6cd2`, four commits ahead of `origin/main` at inspection time.
- Existing worktree: `/Users/jchilders/workspaces/alcf-agent-box/.worktrees/red-shirt-multinode`.
- Branch: `feat/red-shirt-multinode`, HEAD `6f9af87e28bd067030bc2ca7bc4dd7776e7a28c1`, based on current local main.
- Tracked uncommitted files:
  - `deploy/polaris/red-shirt-mpi-acceptance.pbs`
  - `deploy/polaris/red-shirt-polaris.pbs`
  - `scripts/red_shirt_mpi_env.sh`
  - `tests/test_red_shirt_mpi_bridge.py`
- Untracked artifact disposition (classified by content, file type, checksum, and observed campaign role):
  - **Promote under stable names after protocol-first tests:** `.tmp-red-shirt-host-bridge.sh`, `.tmp-red-shirt-host-client.sh`, `.tmp-red-shirt-host-watcher.sh`, `.tmp-red-shirt-rank-wrapper.sh`, `.tmp-gpu-rank-probe.py`.
  - **Use as design/test evidence, then delete rather than ship verbatim:** `.tmp-red-shirt-gpu-campaign.pbs`, `.tmp-red-shirt-pepper-mpi-agent.pbs` (attempt-specific paths/prompts and direct Hermes launch).
  - **Historical diagnostic scratch; archive outside git or delete after extracting assertions:** `.tmp-pals-probe.pbs`, `.tmp-pals-rw.pbs`, `.tmp-pmi-probe.pbs`, `.tmp-runtime-probe.pbs`, `.tmp-pepper-mpi-build.pbs`, `.tmp-pepper-mpi-test.pbs`.
  - None is approved for wholesale commit, and no secret values were found by the credential-pattern scan; the only credential-related match was a token-helper *path*.
- Fresh Phase 0 verification: `55 passed in 4.21s`; all three shell/PBS syntax checks and `git diff --check` exited zero.
- The tracked MPI hardening increment was committed as `3353c90` (`fix(polaris): harden container MPI acceptance`).
