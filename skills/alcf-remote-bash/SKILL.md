---
name: alcf-remote-bash
description: Build and run software on ALCF compute nodes — compile, pip install, apptainer build, run tests — via Globus Compute, under the user's own allocation.
category: research
---

# ALCF remote-bash (build & run software on ALCF systems)

Run an arbitrary shell command on an **ALCF compute node** using the baked helper
`/opt/alcf/alcf_remote_bash.py` (run with `/opt/hermes/.venv/bin/python`). The
command is submitted to an ALCF **multi-user Globus Compute endpoint (MEP)**,
which launches a PBS job on a compute node **under the user's own account /
allocation** and returns `exit_code` + stdout + stderr.

This is the capability that lets the agent actually **build software on ALCF**:
`module load`, `cmake`/`make`, `pip install`, `apptainer build/pull`, running a
test suite, etc. — not just read state.

Reference: https://docs.alcf.anl.gov/services/globus-compute/

## When to load this skill

The user asks to **build / compile / install / run** something *on ALCF* — e.g.
"build my code on Polaris", "compile this on a compute node", "pip install X in
my environment on Crux", "build an apptainer container on ALCF", "run my test
suite on a Polaris node".

For read-only "state of my work" questions (is X up, my jobs, my allocations),
use `alcf-facility-status-and-jobs` instead. For submitting a *batch* PBS script
and polling it, the IRI job-submission path (`alcf-iri-facility-api`) is the
right tool. remote-bash is for **synchronous, interactive** build/run commands.

## IMPORTANT — this is opt-in and gated

remote-bash runs **arbitrary code on ALCF charged to the user's allocation**, so:

1. It is **ON by default** but can be hard-disabled by starting the container
   with `-e ALCF_ENABLE_GLOBUS_COMPUTE=0`. If disabled, the helper says so and
   exits. (The flag gates all Globus Compute access, not just remote-bash.)
2. It requires a **one-time Globus Compute login** — a THIRD Globus login,
   separate from the inference and IRI logins. The agent cannot complete the
   browser login itself; ask the user to run the `authenticate` step on the host.
3. Destructive-looking commands (`rm -rf`, `mkfs`, `dd of=/…`, fork bombs, etc.)
   are **refused** unless `--yes` is passed. Before you add `--yes`, confirm with
   the user in plain language what will run and where.
4. The MEP **requires** `--account` (the ALCF project to charge) and a `--queue`.

## First-run check + login

    PY=/opt/hermes/.venv/bin/python

    # Is it enabled + logged in? (exit 0 = ready, non-zero = not ready)
    $PY /opt/alcf/alcf_remote_bash.py check

    # One-time interactive Globus Compute login (prints a URL; user pastes code).
    # Ask the user to run this on the host if `check` says login is MISSING:
    #   docker exec -it <container> /opt/hermes/.venv/bin/python \
    #     /opt/alcf/alcf_remote_bash.py authenticate

## Running commands

    PY=/opt/hermes/.venv/bin/python

    # Simplest: run a command on Polaris debug queue under the user's project.
    $PY /opt/alcf/alcf_remote_bash.py run --account <PROJECT> \
        --cmd "hostname; module load spack-pe-base cmake; cmake --version"

    # Build a project (bump walltime for long builds):
    $PY /opt/alcf/alcf_remote_bash.py run --account <PROJECT> --walltime 0:30:00 \
        --cmd 'cd $HOME/myproj && cmake -B build && cmake --build build -j'

    # Build an Apptainer image (needs `module load` — apptainer is NOT on the
    # default PATH):
    $PY /opt/alcf/alcf_remote_bash.py run --account <PROJECT> --walltime 0:30:00 \
        --cmd 'module load apptainer 2>/dev/null || module load singularity; \
               apptainer build $HOME/img.sif docker://ubuntu:22.04'

    # Target Crux instead of Polaris:
    $PY /opt/alcf/alcf_remote_bash.py run --account <PROJECT> --endpoint crux \
        --cmd "hostname"

    # JSON output (for programmatic use):
    $PY /opt/alcf/alcf_remote_bash.py run --account <PROJECT> --json --cmd "uname -a"

### Options

- `--account` (required): ALCF project charged for the PBS job.
- `--cmd` (required): shell command; runs under `bash -lc` (so `module` works).
- `--endpoint`: `polaris` (default) or `crux`.
- `--queue`: PBS queue (default `debug`).
- `--walltime`: `HH:MM:SS` (default `0:10:00`). Raise for long builds.
- `--nodes`: nodes per block (default 1).
- `--run-dir`: working dir on the cluster (default `$HOME`).
- `--timeout`: seconds to wait for the result (default 1200).
- `--yes`: allow a destructive-looking command (confirm with the user first).

## Behavior & pitfalls (verified 2026-08-04)

- **Runs under the user's identity/allocation.** `whoami` on the node is the
  user; the job appears as their PBS job (`PBS_ENVIRONMENT=PBS_BATCH`).
- **Latency:** first call is ~1 min (the MEP boots a per-user endpoint + a PBS
  job + the node). Subsequent calls while the endpoint is warm are **seconds**.
  Tell the user the first build command will take about a minute to start.
- **`module load` is needed** for apptainer/singularity and many tools — they
  are NOT on the default PATH. The helper already uses `bash -lc`, so `module`
  resolves; you still have to `module load` the specific tool in `--cmd`.
- **Long builds:** raise `--walltime` and, if needed, `--timeout`. The client
  blocks until the result returns.
- **Runaway job loop:** if jobs fail-loop (usually a bad custom environment),
  submissions won't stop on their own. Recovery is on the CLUSTER:
  `rm ~/.globus_compute/*/daemon.pid` (via SSH or the IRI FS tools), then fix
  the command/environment. PBS job logs are on the cluster under
  `~/.globus_compute/<uep-name>/submit_scripts/`.
- **Serialization:** the image pins Python 3.13 to match the MEP workers, and the
  helper uses the `AllCodeStrategies` serializer, so functions deserialize on the
  node. A minor patch-level Python difference only produces a harmless warning.

## Suggested agent flow

1. Run `check`. If disabled → tell the user it was turned off with
   `-e ALCF_ENABLE_GLOBUS_COMPUTE=0` (remove it to re-enable) and stop. If login
   missing → give the `authenticate` command to run on the host and stop.
2. Confirm the command + `--account` with the user, especially anything that
   writes or deletes files.
3. Run it; report the compute node, exit code, and stdout/stderr plainly.
