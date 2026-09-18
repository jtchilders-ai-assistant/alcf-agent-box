# Red Shirt Polaris Multi-node MPI Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Run Red Shirt on the lead node of a multi-node Polaris allocation with direct user-level access to the allocation's host MPI environment, then use it to build and execute reproducible MPI-enabled Pepper simulations across the allocated nodes.

**Architecture:** The PBS launcher resolves modules and MPI paths on the host before entering Apptainer, preserves the allocation's exact `$PBS_NODEFILE`, and injects the resolved executable/library environment plus read-only host-system binds into Red Shirt. Red Shirt retains its unrestricted terminal tool and invokes host PALS `mpiexec` directly; each MPI rank launches the pinned SIF and the MPI-enabled Pepper executable. The container remains a packaging boundary, not a security sandbox, while Unix-account and PBS-allocation boundaries remain authoritative.

**Tech Stack:** PBS Pro, HPE PALS `mpiexec`, Cray MPICH ABI, Apptainer, Bash, pytest, CMake, Kokkos, Pepper, Hermes A2A.

---

## File map

- Modify `deploy/polaris/red-shirt-polaris.pbs`: request configurable multi-node resources, resolve the host MPI/module environment, preserve and expose the host file, and bind required host trees.
- Create `scripts/red_shirt_mpi_env.sh`: focused host-side environment discovery and validation sourced by the PBS launcher.
- Create `tests/test_red_shirt_mpi_bridge.py`: static and executable tests for host-file handling, environment propagation, bind construction, and fail-closed behavior.
- Modify `tests/test_red_shirt_polaris_launcher.py`: extend the allowed bind-destination contract for verified host paths.
- Modify `deploy/polaris/RED_SHIRT_README.md`: document direct host execution, multi-node submission, the host file, acceptance ladder, and multiple isolated agent homes.
- Modify `docs/superpowers/specs/2026-09-16-red-shirt-polaris-design.md`: record that the container is a packaging boundary and that direct user-level allocation access is intentional.
- Create `deploy/polaris/red-shirt-mpi-acceptance.pbs`: bounded two-node acceptance job that runs native and containerized MPI hello-world before Pepper.

## Task 1: Specify and test the host MPI bridge contract

**Files:**
- Create: `tests/test_red_shirt_mpi_bridge.py`
- Modify: `tests/test_red_shirt_polaris_launcher.py`

- [ ] **Step 1: Write failing structural tests**

Tests must require:

```python
assert 'PBS_NODEFILE' in launcher
assert 'hostfile' in launcher.lower()
assert 'APPTAINERENV_RED_SHIRT_HOSTFILE="/opt/data/runs/${JOB_ID}/pbs_nodefile"' in launcher
assert 'ml cray-mpich-abi' in launcher
assert 'CRAY_LD_LIBRARY_PATH' in launcher
assert '--bind /opt/cray:/opt/cray:ro' in normalized_launcher
assert '--bind /opt/nvidia:/opt/nvidia:ro' in normalized_launcher
assert '--bind /opt/cray/libfabric:/opt/cray/libfabric:ro' in normalized_launcher
assert 'palsd' in launcher
```

Also require that the launcher validates `$PBS_NODEFILE` as a readable regular file, copies it to `$RUN_DIR/pbs_nodefile`, records its SHA-256, and does not synthesize or hard-code hostnames.

- [ ] **Step 2: Run the focused tests and verify RED**

Run:

```bash
pytest -q tests/test_red_shirt_mpi_bridge.py tests/test_red_shirt_polaris_launcher.py
```

Expected: failures identify missing host-file preservation, MPI environment injection, and host binds.

- [ ] **Step 3: Commit the failing contract tests**

```bash
git add tests/test_red_shirt_mpi_bridge.py tests/test_red_shirt_polaris_launcher.py
git commit -m "test(polaris): specify Red Shirt MPI bridge"
```

## Task 2: Implement host-side MPI environment discovery

**Files:**
- Create: `scripts/red_shirt_mpi_env.sh`
- Modify: `deploy/polaris/red-shirt-polaris.pbs`

- [ ] **Step 1: Implement the minimum tested discovery helper**

The helper must run on the PBS host and:

```bash
ml use /soft/modulefiles
ml spack-pe-base
ml apptainer
ml cray-mpich-abi
```

It must fail unless `mpiexec`, `apptainer`, `$PBS_NODEFILE`, and `CRAY_LD_LIBRARY_PATH` are present. It must dynamically select the existing PALS runtime directory from `/run/palsd` or `/var/run/palsd`, export resolved paths, and never hard-code a versioned PALS or MPICH directory.

- [ ] **Step 2: Preserve the exact host file**

Copy without rewriting:

```bash
install -m 600 "$PBS_NODEFILE" "$RUN_DIR/pbs_nodefile"
sha256sum "$RUN_DIR/pbs_nodefile" > "$RUN_DIR/pbs_nodefile.sha256"
```

Set:

```bash
export APPTAINERENV_RED_SHIRT_HOSTFILE="/opt/data/runs/${JOB_ID}/pbs_nodefile"
export APPTAINERENV_PBS_NODEFILE="$APPTAINERENV_RED_SHIRT_HOSTFILE"
```

The original path and preserved checksum belong in `metadata.txt`.

- [ ] **Step 3: Inject resolved host execution paths**

Pass a host-derived `PATH`, `LD_LIBRARY_PATH`, `CRAY_LD_LIBRARY_PATH`, and necessary Cray/PALS variables through `APPTAINERENV_*`. Bind only paths that exist, including `/opt/cray`, `/opt/nvidia`, `/opt/cray/libfabric`, `/soft`, and the discovered PALS runtime directory. Keep credentials read-only and Hermes state writable as before.

- [ ] **Step 4: Run focused tests and shell syntax checks**

```bash
pytest -q tests/test_red_shirt_mpi_bridge.py tests/test_red_shirt_polaris_launcher.py tests/test_red_shirt_release_contract.py
bash -n scripts/red_shirt_mpi_env.sh deploy/polaris/red-shirt-polaris.pbs
```

Expected: all pass.

- [ ] **Step 5: Commit**

```bash
git add scripts/red_shirt_mpi_env.sh deploy/polaris/red-shirt-polaris.pbs tests/test_red_shirt_mpi_bridge.py tests/test_red_shirt_polaris_launcher.py
git commit -m "feat(polaris): expose host MPI allocation to Red Shirt"
```

## Task 3: Add the two-node MPI acceptance job

**Files:**
- Create: `deploy/polaris/red-shirt-mpi-acceptance.pbs`
- Modify: `tests/test_red_shirt_mpi_bridge.py`

- [ ] **Step 1: Write failing acceptance-launcher tests**

Require `select=2`, an explicit bounded debug walltime, `$PBS_NODEFILE`, `--hostfile`, two total ranks with one rank per node, module/version capture, native hello-world, containerized hello-world, link audit, rank/size/hostname output, and machine-readable terminal status.

- [ ] **Step 2: Run the focused test and verify RED**

```bash
pytest -q tests/test_red_shirt_mpi_bridge.py
```

Expected: failure because the acceptance launcher does not exist.

- [ ] **Step 3: Implement the acceptance launcher**

The job must compile a minimal C MPI hello-world with the host wrapper, then run:

```bash
mpiexec --hostfile "$PBS_NODEFILE" -n 2 --ppn 1 \
  ./hello_mpi

mpiexec --hostfile "$PBS_NODEFILE" -n 2 --ppn 1 \
  apptainer exec <production-equivalent binds/env> "$SIF" \
  /opt/data/runs/<job>/hello_mpi
```

Preserve exact commands, module list, versions, host file, `ldd`, stdout/stderr, and exit codes. Verify two distinct allocated hosts and ranks `0/2` and `1/2`.

- [ ] **Step 4: Verify locally**

```bash
pytest -q tests/test_red_shirt_mpi_bridge.py
bash -n deploy/polaris/red-shirt-mpi-acceptance.pbs
git diff --check
```

- [ ] **Step 5: Commit**

```bash
git add deploy/polaris/red-shirt-mpi-acceptance.pbs tests/test_red_shirt_mpi_bridge.py
git commit -m "test(polaris): add two-node MPI acceptance job"
```

## Task 4: Document the deployment model

**Files:**
- Modify: `deploy/polaris/RED_SHIRT_README.md`
- Modify: `docs/superpowers/specs/2026-09-16-red-shirt-polaris-design.md`

- [ ] **Step 1: Document multiple isolated identities**

Add a runbook section explaining that one immutable SIF can back multiple identities when each gets a distinct writable Hermes home and distinct secrets/runtime networking state. Explicitly prohibit concurrent reuse of one writable home.

- [ ] **Step 2: Document direct host access and the host file**

State that Apptainer is a packaging boundary, not the intended security boundary. Document `$PBS_NODEFILE` preservation at:

```text
host:      $HOME/red-shirt-polaris/home/runs/<PBS_JOBID>/pbs_nodefile
container: /opt/data/runs/<PBS_JOBID>/pbs_nodefile
```

Document host-side module resolution, injected environment, host-system binds, `mpiexec --hostfile`, and the two-stage hello-world acceptance gate.

- [ ] **Step 3: Document boundaries accurately**

Red Shirt has arbitrary user-level execution within the allocation and mounted writable filesystems. It does not gain root, scheduler-administrator authority, access beyond the Unix account, or nodes outside the PBS allocation.

- [ ] **Step 4: Verify documentation claims against code**

```bash
python - <<'PY'
from pathlib import Path
pbs = Path('deploy/polaris/red-shirt-polaris.pbs').read_text()
doc = Path('deploy/polaris/RED_SHIRT_README.md').read_text()
for token in ('PBS_NODEFILE', 'pbs_nodefile', 'cray-mpich-abi', 'mpiexec', '/opt/cray'):
    assert token in pbs and token in doc, token
PY
git diff --check
```

- [ ] **Step 5: Commit**

```bash
git add deploy/polaris/RED_SHIRT_README.md docs/superpowers/specs/2026-09-16-red-shirt-polaris-design.md
git commit -m "docs(polaris): describe isolated identities and direct MPI access"
```

## Task 5: Run the two-node bridge acceptance on Polaris

**Files:**
- Runtime evidence: `$HOME/red-shirt-polaris/home/runs/<PBS_JOBID>/mpi-acceptance/`

- [ ] **Step 1: Verify the borrowed SSH master**

```bash
polaris-cm status --json
```

Require `transport_ok: true`, `remote_user: parton`, and a Polaris login host.

- [ ] **Step 2: Stage and submit through `polaris-cm`**

Use `polaris-cm run` for bounded staging/submission. Submit with:

```bash
qsub -A datascience deploy/polaris/red-shirt-mpi-acceptance.pbs
```

Do not use raw off-cluster SSH and do not use `qsub -v` for credential values.

- [ ] **Step 3: Poll PBS and inspect evidence**

Require PBS terminal state with `Exit_status = 0`, two distinct hosts in the preserved host file, native and containerized rank proofs, and no `not found` line in the link audit.

- [ ] **Step 4: Stop on bridge failure**

If native MPI fails, fix host module/launcher setup. If native passes but containerized MPI fails, fix binds/environment. Do not modify Pepper to compensate for an unproven bridge.

## Task 6: Build clean MPI-enabled Pepper and run multi-node simulations

**Files:**
- Runtime task root: `$HOME/red-shirt-polaris/home/tasks/pepper-mpi-<date>/`

- [ ] **Step 1: Create a clean checkout**

Clone `https://gitlab.com/spice-mc/pepper.git`, record the exact commit, require an empty `git status --porcelain`, and keep the prior dirty serial tree separate as historical evidence.

- [ ] **Step 2: Configure with MPI enabled**

Use the accepted compiler/runtime bridge. Preserve configure output and `CMakeCache.txt`. Fail unless MPI is detected and `PEPPER_MPI_DISABLED` is absent or false.

- [ ] **Step 3: Build and test**

Use at most eight build jobs. Preserve raw logs and verify the executable exists and runs.

- [ ] **Step 4: Launch deterministic multi-node simulations**

From Red Shirt's terminal, use the preserved host file explicitly:

```bash
mpiexec --hostfile "$RED_SHIRT_HOSTFILE" -n 2 --ppn 1 \
  apptainer exec <accepted binds/env> "$RED_SHIRT_SIF" \
  <pepper-executable> <fixed process/events/seed arguments>

mpiexec --hostfile "$RED_SHIRT_HOSTFILE" -n 4 --ppn 2 \
  apptainer exec <accepted binds/env> "$RED_SHIRT_SIF" \
  <pepper-executable> <fixed process/events/seed arguments>
```

Record exact command, process, event count, seed, ranks, ranks per node, threads, placement, timings, outputs, and exit status. Select the concrete Pepper process/event count from its own documented examples after inspecting the clean checkout; do not invent an input.

- [ ] **Step 5: Verify the scientific result independently**

Check output files exist and are nonempty, all expected ranks participated, reported event totals match requested totals, and the working tree remains clean. Distinguish application success from transport/A2A success.

- [ ] **Step 6: Write durable report**

Require `REPORT.md` and machine-readable `RESULT.json` under the task root, including repository/SIF/PBS provenance and links to raw logs.

## Task 7: Full verification and integration

**Files:**
- All files above

- [ ] **Step 1: Run local verification**

```bash
pytest -q \
  tests/test_red_shirt_config.py \
  tests/test_red_shirt_runtime.py \
  tests/test_red_shirt_polaris_launcher.py \
  tests/test_red_shirt_release_contract.py \
  tests/test_red_shirt_mpi_bridge.py
bash -n \
  scripts/red_shirt_entrypoint.sh \
  scripts/red_shirt_mpi_env.sh \
  deploy/polaris/red-shirt-polaris.pbs \
  deploy/polaris/red-shirt-mpi-acceptance.pbs
git diff --check
git status --short
```

- [ ] **Step 2: Review security and correctness**

Confirm no credentials, generated state, `.hermes/`, or runtime artifacts are tracked. Confirm the launcher never synthesizes hosts, never requests ranks beyond the allocation, and does not claim the container is a security sandbox.

- [ ] **Step 3: Commit and push reviewed increments**

Push the branch, require CI green, merge only reviewed increments, and verify local main, origin main, and GitHub main resolve to the same commit before retiring the worktree.
