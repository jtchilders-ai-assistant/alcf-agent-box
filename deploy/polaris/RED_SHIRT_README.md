# Red Shirt Polaris — deployment runbook

Resident Hermes agent named **Red Shirt Polaris**, running inside an
Apptainer container in a Polaris PBS job. Communicates with Wesley over
authenticated standard A2A v1.0 through the private Headscale tailnet, using
the ALCF Inference Service as its only LLM backend.

Design: `docs/superpowers/specs/2026-09-16-red-shirt-polaris-design.md`
Plan:   `docs/superpowers/plans/2026-09-16-red-shirt-polaris.md`

## Pinned artifact

`deploy/polaris/build-red-shirt-sif.sh` and `deploy/polaris/red-shirt-polaris.pbs`
both refuse to run without an explicit, CI-published image reference and
digest — there is no default and no `latest` fallback:

```bash
export RED_SHIRT_IMAGE=ghcr.io/<owner>/alcf-red-shirt-polaris:sha-<short-sha>
export RED_SHIRT_DIGEST=sha256:<manifest-digest-from-the-CI-run>
```

Both variables must come from a completed CI run of the
`build-red-shirt-polaris` job (Task 1) for the exact commit being deployed.
Never substitute a guessed or previously-seen digest.

## 1. Stage credentials and layout

All credential material is staged as mode-`0600` regular files under
`$HOME/red-shirt-polaris/secrets/` — never in git, never in an OCI/SIF
layer, never in `qsub -v`, environment variables, command-line arguments,
Discord, or shell history.

```bash
BASE_DIR="$HOME/red-shirt-polaris"
install -d -m 700 "$BASE_DIR/secrets" "$BASE_DIR/home"

install -m 600 /secure/source/headscale-auth.key   "$BASE_DIR/secrets/headscale-auth.key"
install -m 600 /secure/source/inbound-a2a.token    "$BASE_DIR/secrets/inbound-a2a.token"
install -m 600 /secure/source/outbound-a2a.token   "$BASE_DIR/secrets/outbound-a2a.token"
install -m 600 /secure/source/caddy-root.crt       "$BASE_DIR/secrets/caddy-root.crt"

for f in "$BASE_DIR"/secrets/*; do
  test "$(stat -c '%a' "$f")" = 600 || { echo "bad mode: $f" >&2; exit 1; }
done
```

- `headscale-auth.key` — short-lived, reusable-for-retries, ephemeral-node-scoped
  Headscale join key. **Not reusable across deployments**; expire it after
  use (see Teardown).
- `inbound-a2a.token` — bearer token Wesley presents when calling Red Shirt
  Polaris; Red Shirt Polaris authenticates the caller as `wesley`.
- `outbound-a2a.token` — bearer token Red Shirt Polaris presents when
  calling Wesley; Wesley authenticates the caller as `red-shirt-polaris`.
- `caddy-root.crt` — public trust material (not a private key) for the
  Headscale/Caddy reverse proxy.

`$BASE_DIR/home` persists across job restarts: Hermes config, sessions,
memory, A2A conversation history, and the audit log all live there. Only
Tailscale node identity and sockets are job-local and disposable.

### Multiple isolated identities from one SIF

The SIF is immutable and may be reused for multiple isolated Hermes agents.
Give every identity a distinct writable home and secrets directory, for
example:

```text
$HOME/hermes-agents/red-shirt/home  -> /opt/data
$HOME/hermes-agents/blue-shirt/home -> /opt/data
```

Each home independently contains its `SOUL.md`, `config.yaml`, `.env`,
`state.db`, memories, sessions, skills, logs, A2A records, and task files.
Each concurrently running identity must also have distinct Tailscale state,
socket, ports, hostname, A2A credentials, run directory, and scratch root.
Never mount one writable Hermes home into concurrent agents: their SQLite WAL
files, sockets, locks, credentials, and identities would collide.

For each identity, stage a small instance-specific launcher that sets
`BASE_DIR`, `HERMES_HOME_DIR`, and `SECRETS_DIR` before invoking the common
launcher. Credentials remain files under that selected secrets directory.
Do not pass either credentials or path configuration with `qsub -v`; keeping
the complete instance definition in a reviewed launcher makes each deployment
reproducible and avoids inheriting unrelated submit-shell state.

```bash
BASE_DIR="$HOME/hermes-agents/blue-shirt"
HERMES_HOME_DIR="$BASE_DIR/home"
SECRETS_DIR="$BASE_DIR/secrets"
# The instance launcher then runs the common launch body with these values.
```

## 2. Build the SIF

Run on a Polaris node with the pinned image exported (see above):

```bash
bash deploy/polaris/build-red-shirt-sif.sh
```

The script loads `spack-pe-base` before `apptainer` (verified module order:
`ml use /soft/modulefiles` → `ml spack-pe-base` → `ml apptainer`), keeps all
temporary extraction/cache traffic on `/local/scratch/$USER/...`, bounds
`mksquashfs` to 4 processors / 4 GiB, and writes:

```text
$HOME/red-shirt-polaris/red-shirt-polaris-<tag>.sif
$HOME/red-shirt-polaris/red-shirt-polaris-<tag>.sif.sha256
```

It also runs `apptainer exec "$SIF" hermes --version` and
`apptainer exec "$SIF" tailscale version` from inside the built SIF as an
executable sanity check — a build that produces a broken image fails here
rather than at job runtime. If the PBS-side `sha256sum -c` check ever fails,
discard both files and rebuild rather than trusting a partial artifact.

## 3. Submit

The project is supplied explicitly on the command line — PBS does not
expand shell variables inside `#PBS` directives, so the launcher never
embeds `#PBS -A`:

```bash
export RED_SHIRT_IMAGE=ghcr.io/<owner>/alcf-red-shirt-polaris:sha-<short-sha>
export RED_SHIRT_DIGEST=sha256:<manifest-digest-from-the-CI-run>
qsub -A datascience deploy/polaris/red-shirt-polaris.pbs
```

Never use `qsub -v` for credentials — the launcher reads credential *paths*
that default to `$HOME/red-shirt-polaris/secrets/...` and validates each
file's metadata (existence, regular file, mode `0600`) before doing
anything else. Credential contents are never printed, hashed, or logged.

## Direct host and multi-node MPI access

For this deployment, Apptainer is a **packaging boundary**, not a security
sandbox. Red Shirt intentionally receives arbitrary user-level command access
within its PBS allocation and mounted writable filesystems. It does not gain
root, scheduler-administrator authority, another user's permissions, or access
to nodes outside the allocation.

The PBS launcher resolves `cray-mpich-abi` and the module environment on the
host before entering Apptainer. It injects the resolved executable and library
paths and read-only binds for `/opt/cray`, `/opt/nvidia`,
`/opt/cray/libfabric`, `/soft`, and the live PALS runtime directory. This lets
Red Shirt use host PALS `mpiexec` directly instead of requiring an MCP broker.

The scheduler-provided host file is authoritative and is never synthesized or
replaced with hard-coded hostnames. The launcher copies `$PBS_NODEFILE`
verbatim and records its checksum:

```text
host:      $HOME/red-shirt-polaris/home/runs/<PBS_JOBID>/pbs_nodefile
container: /opt/data/runs/<PBS_JOBID>/pbs_nodefile
env:       RED_SHIRT_HOSTFILE=/opt/data/runs/<PBS_JOBID>/pbs_nodefile
```

Every multi-node command must pass that file explicitly, for example:

```bash
mpiexec --hostfile "$RED_SHIRT_HOSTFILE" -n 2 --ppn 1 \
  apptainer exec <verified-binds-and-environment> <pinned.sif> <program>
```

Before using Pepper, submit the two-node acceptance job and require both its
native and containerized MPI hello-world stages to pass:

```bash
qsub -A datascience \
  -v SIF="$HOME/red-shirt-polaris/<pinned>.sif" \
  deploy/polaris/red-shirt-mpi-acceptance.pbs
```

`SIF` is a non-secret path; credentials must never be passed with `qsub -v`.
Acceptance evidence, including the exact
host file, module list, versions, link audit, rank placement, and terminal
record, is written beneath
`$HOME/red-shirt-polaris/home/runs/<PBS_JOBID>/mpi-acceptance/`.

### Bounded autonomous Pepper campaign

Stage the image-managed campaign tools under `$HOME/red-shirt-polaris/campaign-tools/`
from the exact merged revision, preserving executable modes. Then submit the dedicated
one-shot launcher with a reviewed instance wrapper that exports only non-secret paths
(`SIF`, and optional `MAX_ATTEMPTS`/`HERMES_TIMEOUT`); do not use `qsub -v` for
credentials:

```bash
qsub -A datascience deploy/polaris/red-shirt-pepper-campaign.pbs
```

The launcher allocates exactly two nodes, verifies the pinned SIF checksum,
serializes attempts through `attempt-ledger.lock`, defaults to one total attempt,
starts the correlated host watcher, generates attempt-local context and the ordered
seven-stage preflight contract, refreshes and smokes inference, and invokes the
bounded campaign wrapper. Red Shirt—not the launcher—selects and installs candidate
application dependencies and executes the preflight before configuring/building
Pepper. Attempt evidence is retained under
`$HOME/red-shirt-polaris/home/campaigns/pepper-gpu-8rank/attempts/<NNN>/`.

## 4. Monitor

```bash
qstat -xf "$JOBID"
```

Look specifically at `Exit_status` — a job appearing to have started or
ended is not evidence of success by itself. Per-job output lands under:

```text
$HOME/red-shirt-polaris/runs/<PBS_JOBID>/metadata.txt    # non-secret provenance
$HOME/red-shirt-polaris/runs/<PBS_JOBID>/stdout.log
$HOME/red-shirt-polaris/runs/<PBS_JOBID>/stderr.log
$HOME/red-shirt-polaris/runs/<PBS_JOBID>/ready.json      # readiness record (in-container probe)
$HOME/red-shirt-polaris/runs/<PBS_JOBID>/terminal.json   # terminal record (always written)
```

`ready.json` and `terminal.json` are machine-readable, contain no secret
values, and are written under the **persistent** run directory (not
job-local scratch) so they survive after the job ends.

## 5. Failure handling

If the job's `Exit_status` is nonzero:

1. Read `terminal.json` first — it records the exit code and is written by
   the launcher's own signal-safe cleanup trap even when startup fails
   partway through.
2. Read `stderr.log` and `metadata.txt` for the failing gate.
3. Common fail-closed causes: a credential file missing/not mode `0600`,
   the SIF or its `.sha256` sidecar missing, `sha256sum -c` mismatch (never
   trust a partial artifact — rebuild), or no live ALCF chat model with
   >=64000-token context (exit `78`).
4. Never rerun against a SIF that failed its checksum check.

## 6. Teardown

Delete the job, then **re-poll** `qstat` — `qdel`'s own return code is not
sufficient evidence the job actually stopped:

```bash
qdel "$JOBID"
qstat -xf "$JOBID"   # re-poll after qdel; confirm the job is absent or terminal
```

Then, on the Headscale server, revoke the ephemeral join key and remove the
disposable probe node (never trust the launcher's in-job cleanup alone for
server-side state):

```bash
cd /opt/headscale
docker compose exec headscale headscale preauthkeys list --user PLACEHOLDER_USER_ID
docker compose exec headscale headscale preauthkeys expire --id PLACEHOLDER_KEY_ID
docker compose exec headscale headscale nodes list
docker compose exec headscale headscale nodes delete --identifier PLACEHOLDER_NODE_ID
docker compose exec headscale headscale nodes list
```

Finally, securely remove the staged Headscale join key from Polaris (the
per-peer A2A tokens and CA cert may be kept for the next deployment):

```bash
rm -f "$HOME/red-shirt-polaris/secrets/headscale-auth.key"
test ! -e "$HOME/red-shirt-polaris/secrets/headscale-auth.key"
```

Persistent Hermes state (`$HOME/red-shirt-polaris/home`) and the reproducible
SIF are retained so a restart is cheap — only the Tailscale node identity is
disposable.
