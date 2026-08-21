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

## PREFER the `bash` MCP tool when it is in your toolset

If your tool list contains an MCP-provided **`bash`** tool (server `alcf-bash`),
use THAT for compute-node commands instead of shelling out to this CLI. It is
the same machinery underneath, but: one required argument (`command`), the
account/queue/walltime already bound, ONE warm node held across your whole
conversation, and the same persistent-shell state described below. Fall back to
the CLI here when the MCP tool is absent or you need non-default
endpoint/queue/walltime/venv per command.

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

## THE TWO MACHINES — container vs cluster (read this first)

Your `terminal` / `write_file` / `read_file` tools act on the **agent
container**; ONLY the `--cmd` string runs on the **cluster**. The two share no
filesystem:

- Container `$HOME` = `/opt/data`. Cluster `$HOME` = `/home/<username>`.
  `/opt/data`, `/opt/hermes`, `/opt/alcf` do NOT exist on any ALCF node.
- **SINGLE-QUOTE `--cmd`.** In `--cmd "cd $HOME/x"` the double quotes expand
  `$HOME` in the container (→ `/opt/data/x`) BEFORE submission. The helper
  refuses commands referencing container-only paths (override:
  `--allow-container-paths`).
- `write_file` cannot create files on the cluster (and IRI upload is a 501
  stub). Stage a small file with a single-line `printf '...\n' > file` in
  `--cmd`, or `git clone` on the node (the proxy is set up automatically).
- Start a build session with an orientation probe:
  `--cmd 'whoami && echo $HOME && pwd && hostname'` and use THOSE paths.

## Internet access from compute nodes (proxy — automatic)

Compute nodes have no direct outbound internet. The helper automatically
exports `http_proxy`/`https_proxy=http://proxy.alcf.anl.gov:3128` (when unset)
before your command, so `git clone` / `curl` / `pip install` / `apptainer pull`
work on the node. `--no-proxy-setup` disables the injection. If a PBS script
will run OUTSIDE remote-bash, include those exports in it yourself.

## Timeouts — set BOTH, and check the queue before retrying

1. The helper waits `--timeout` seconds (default 1200) for the result — and the
   `terminal` tool has its OWN per-call timeout that can kill the CLI first.
   This image's config default is 1320s (stock Hermes is 180s), so plain calls
   are safe; if you pass a per-call terminal timeout, keep it ≥ the helper's
   `--timeout`, and never lower it below a plausible cold start + queue wait.
   (The MCP `bash` tool manages this itself — prefer it.)
2. **A timeout usually means the PBS job is stuck in the queue, not that the
   command failed.** Resubmitting spawns ANOTHER queued job and pays another
   cold start. Instead check queue congestion first —
   `/opt/hermes/.venv/bin/python /opt/alcf/alcf_facility.py jobs --cluster
   polaris` (see `alcf-facility-status-and-jobs`) — then wait, switch
   `--endpoint`, or tell the user. The killed command may still finish on the
   node; shared `--session` state will reflect it.

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

    # Target Crux instead of Polaris (endpoints: polaris, crux, sophia, edith):
    $PY /opt/alcf/alcf_remote_bash.py run --account <PROJECT> --endpoint crux \
        --cmd "hostname"

    # JSON output (for programmatic use):
    $PY /opt/alcf/alcf_remote_bash.py run --account <PROJECT> --json --cmd "uname -a"

## Fast path: `batch` (many commands on ONE warm node)

For an interactive build loop — compile → read error → fix → rebuild — do NOT
issue a separate `run` per step. Each fresh `run` risks paying the ~1-minute
cold start again if the endpoint idled out. Instead use `batch`, which runs a
list of commands through a **single warm Executor session**: the first command
pays cold start (~1 min), and every subsequent command reuses the **same compute
node** and returns in **~1 second** (verified live: 20.9s cold, then 1.0s / 1.0s
on the same node `x3108…`).

    PY=/opt/hermes/.venv/bin/python

    # Commands from a file, one per line (# comments + blank lines ignored):
    $PY /opt/alcf/alcf_remote_bash.py batch --account <PROJECT> \
        --run-dir '$HOME/myproj' --cmds-file steps.txt

    # Or repeat --cmd (order preserved):
    $PY /opt/alcf/alcf_remote_bash.py batch --account <PROJECT> \
        --cmd 'module load spack-pe-base cmake' \
        --cmd 'cmake -B build' \
        --cmd 'cmake --build build -j'

`batch` stops at the first non-zero exit (so a failed configure doesn't waste a
build) unless you pass `--keep-going`. This is the recommended mode whenever you
have 2+ steps.

**Prefer `--cmds-file` over long inline `--cmd` strings.** Write the steps to a
file with `write_file` first (the file lives in the container — that's fine,
the CLI reads it locally and ships each line to the node). Long, quote-heavy
inline commands have to survive multiple escaping layers and are the main
trigger for malformed tool calls; a cmds-file keeps each step clean.

## Shell state — what persists between commands (and what doesn't)

Commands sharing a `--session` (default: the endpoint name, so consecutive
`run`s and `batch` steps share state automatically) behave like ONE persistent
shell — a state file on the cluster is restored/saved around every command:

- **`cd` persists.** The next command starts where the last one left off.
- **`export FOO=...` persists.** (An UNexported `FOO=...` does NOT — use export.)
- **`module load X` persists** (its effect is exported env: PATH, etc.), so
  `--cmd 'module load spack-pe-base cmake'` followed by `--cmd 'cmake -B build'`
  works as separate steps.
- **Files persist** on the cluster filesystems regardless of session.
- Per-job vars (`PBS_*`, `TMPDIR`, hostname) deliberately do NOT carry over.
- State survives the warm block idling out — even a later command landing on a
  DIFFERENT node restores it (the state file lives on the home filesystem).
- `--fresh` starts clean; `--session ''` disables state; `--session NAME`
  keeps separate workstreams isolated.

**Pitfall — one command per line still applies:** in `--cmds-file`, each line is
a *separate* `bash -lc` invocation, so a multi-line construct (a `<<EOF`
heredoc, a `for`/`if` block spanning lines) will break — its lines run as
independent commands. Keep each step on a single line; to write a file on the
node use a single-line `printf '...\n' > f`, not a heredoc.

## Output limits — grep the log, don't re-run

Combined stdout+stderr beyond `--max-output` bytes (default 20000) is returned
**head+tail**, and the FULL stream is saved on the node first; the truncation
marker names the file (under `~/.alcf_remote_bash/logs/`). When a build log is
truncated, the next step is to `grep -n 'error' <that file>` (or `tail -50`) in
a follow-up command — do NOT re-run the build to "see more output", and do not
raise `--max-output` unless you truly need the whole stream (`--full-output`
allows up to the ~10 MB RPC cap).

### Activating a venv on the node (`--venv`)

Both `run` and `batch` accept `--venv <path>` to `source <path>/bin/activate`
before every command in the session — handy for Python builds/tests that need a
specific environment on the cluster filesystem:

    $PY /opt/alcf/alcf_remote_bash.py batch --account <PROJECT> \
        --venv /eagle/<project>/<you>/myenv --cmds-file test_steps.txt

(Point `--venv` at YOUR environment on a cluster filesystem; there is no default.)

### Options

- `--account` (required): ALCF project charged for the PBS job.
- `--cmd` (required for `run`; repeatable for `batch`): shell command; runs under
  `bash -lc` (so `module` works).
- `--cmds-file` (`batch` only): file with one command per line.
- `--endpoint`: `polaris` (default), `crux`, `sophia`, or `edith`.
- `--queue`: PBS queue (default `debug`).
- `--walltime`: `HH:MM:SS` (default `0:10:00`). Raise for long builds.
- `--nodes`: nodes per block (default 1).
- `--run-dir`: working dir on the cluster (default `$HOME`; a `cd` in a session
  overrides it for later commands).
- `--venv`: cluster venv to `source .../bin/activate` before each command.
- `--timeout`: seconds to wait per command result (default 1200).
- `--session`: shell-state session name (default: the endpoint name; `''`
  disables state). See "Shell state" above.
- `--fresh`: wipe the session state before the first command.
- `--max-output`: returned stdout+stderr byte budget (default 20000); overflow
  is truncated head+tail with the full log saved on the node.
- `--full-output`: return up to the ~10 MB RPC cap.
- `--keep-going` (`batch` only): continue after a non-zero exit.
- `--yes`: allow a destructive-looking command (confirm with the user first).
- `--no-proxy-setup`: do NOT auto-export the ALCF HTTP proxy on the node.
- `--allow-container-paths`: skip the refusal of commands that reference
  container-only paths (`/opt/data`, `/opt/hermes`, `/opt/alcf`).

## Behavior & pitfalls (verified 2026-08-04)

- **Runs under the user's identity/allocation.** `whoami` on the node is the
  user; the job appears as their PBS job (`PBS_ENVIRONMENT=PBS_BATCH`).
- **Latency & warm reuse:** first call is ~1 min (the MEP boots a per-user
  endpoint + a PBS job + the node). Subsequent calls while the endpoint is warm
  are **~1 second** (verified live: 20.9s cold → 1.0s → 1.0s on the same node).
  The node stays warm because the MEP keeps the PBS block alive between
  submissions. **To exploit this, use `batch`** (many commands in one session) —
  or, for separate `run` calls, keep `--endpoint/--account/--queue/--walltime`
  IDENTICAL so a later call lands on the still-running block. Changing any of
  those forces a new block (cold start again).
- **Output size cap:** the helper returns at most `--max-output` bytes (default
  20000) head+tail, saving the full stream to `~/.alcf_remote_bash/logs/` on the
  node — the marker names the file; grep/tail it in a follow-up command.
  (Globus Compute's own hard cap on a result payload is ~10 MB; `--full-output`
  goes up to that.)
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
