# Red Shirt environment catalog

The Red Shirt environment catalog is a versioned SQLite snapshot used to discover Polaris modules, executable paths, declared dependencies, and prior attempt observations. It is a **discovery aid**, not a compatibility service or proof.

## Deployment model

There is no sidecar process, database daemon, network listener, or separate container. An initialized Polaris login shell runs the collector, producing an immutable site database and SHA-256 sidecar. A campaign verifies and copies those files into its attempt directory, then bind-mounts the database read-only at `/environment/site.sqlite`. Each attempt has a separate mode-`0600` writable overlay.

## Schema and evidence

- `snapshots`: system/source identity, timestamps, finalization, and collection completeness.
- `entities`: modules, executables, paths, and related discoverable objects.
- `relations`: explicit directed edges such as prerequisites, conflicts, ELF `NEEDED`, RPATH, and RUNPATH.
- `observations`: timestamped claims, evidence levels, outcomes, command provenance, exit codes, and evidence references.
- `entity_fts`: FTS5 index for entity search.
- `metadata`: schema/tool version and overlay linkage.

Evidence levels remain distinct: requested, declared, detected, compiled/linked, runtime-observed, and scientific. A catalog record cannot establish compile, link, runtime, topology, Pepper, or scientific success. Contradictory observations coexist and retain their lineage.

## Collection on Polaris

Run from an initialized login shell so `LMOD_CMD` and `MODULEPATH` are available. The collector executes Lmod directly as `"$LMOD_CMD" sh ...`; it never evaluates shell output or uses `shell=True`.

```bash
CATALOG="$HOME/red-shirt-polaris/catalog/polaris-site.sqlite"
mkdir -p "$(dirname "$CATALOG")"

python3 scripts/red_shirt_env_catalog.py collect \
  --output "$CATALOG" \
  --system polaris \
  --source-id "$(hostname)-$(date -u +%Y%m%dT%H%M%SZ)" \
  --discover-modules \
  --module-limit 200 \
  --command-timeout 30 \
  --executable cc \
  --executable CC \
  --executable mpiexec

python3 scripts/red_shirt_env_catalog.py finalize --db "$CATALOG"
python3 scripts/red_shirt_env_catalog.py verify --db "$CATALOG"
```

For a focused/deeper snapshot, create a new output rather than attempting to append to an existing catalog:

```bash
DEEP="$HOME/red-shirt-polaris/catalog/polaris-toolchain.sqlite"
python3 scripts/red_shirt_env_catalog.py collect \
  --output "$DEEP" \
  --system polaris \
  --source-id "$(hostname)-toolchain-$(date -u +%Y%m%dT%H%M%SZ)" \
  --module PrgEnv-gnu \
  --module cray-mpich \
  --module cuda \
  --module kokkos \
  --executable cc \
  --executable CC \
  --command-timeout 30
python3 scripts/red_shirt_env_catalog.py finalize --db "$DEEP"
python3 scripts/red_shirt_env_catalog.py verify --db "$DEEP"
```

A failed or timed-out probe marks collection incomplete. It does not prove a dependency is absent.

## Campaign staging

```bash
install -m 0444 "$CATALOG" "$HOME/red-shirt-polaris/campaign-tools/polaris-site.sqlite"
install -m 0444 "$CATALOG.sha256" "$HOME/red-shirt-polaris/campaign-tools/polaris-site.sqlite.sha256"
export RED_SHIRT_ENV_CATALOG="$HOME/red-shirt-polaris/campaign-tools/polaris-site.sqlite"
export SIF="$HOME/red-shirt-polaris/red-shirt-polaris-<tag>.sif"
qsub -A datascience deploy/polaris/red-shirt-pepper-campaign.pbs
```

The launcher verifies the original and copied snapshot, records its digest, snapshot ID, and collection status in `facts.json`, and exposes the attempt overlay path. It fails closed on missing or mismatched artifacts.

## Deterministic queries

The CLI emits sorted JSON.

```bash
CLI=/opt/red-shirt-polaris/red_shirt_env_catalog.py
SITE=/environment/site.sqlite

"$CLI" status --db "$SITE"
"$CLI" search --db "$SITE" --query kokkos --limit 25
"$CLI" show --db "$SITE" --name kokkos
"$CLI" dependencies --db "$SITE" --name kokkos --max-depth 5 --limit 100
"$CLI" reverse-dependencies --db "$SITE" --name libmpi.so --max-depth 5 --limit 100
```

Merged search uses `--overlay "$RED_SHIRT_ENV_CATALOG_OVERLAY"` and preserves the source snapshot identity.

## Recording attempt observations

`observe` accepts a JSON object, not a path to a JSON document. Evidence files must already exist beneath the declared absolute attempt root, and their lowercase SHA-256 must match.

```bash
EVIDENCE="$TASK_ROOT/evidence/configure.log"
DIGEST="$(sha256sum "$EVIDENCE" | cut -d' ' -f1)"
PAYLOAD="$(python3 - "$EVIDENCE" "$DIGEST" "$RED_SHIRT_ENV_PROFILE_ID" <<'PY'
import json, sys
path, digest, profile = sys.argv[1:]
print(json.dumps({
    "subject": "pepper-configure",
    "kind": "command",
    "claim": "Pepper configure completed",
    "evidence_level": "detected",
    "outcome": "success",
    "evidence_path": path,
    "evidence_sha256": digest,
    "command_exit_code": 0,
    "env_profile_id": profile,
}, separators=(",", ":")))
PY
)"
"$CLI" observe \
  --site "$SITE" \
  --overlay "$RED_SHIRT_ENV_CATALOG_OVERLAY" \
  --attempt-root "$TASK_ROOT" \
  --input "$PAYLOAD"
```

Record failures as failures; never overwrite earlier contradictory observations.

## Threat model and prohibited data

The catalog rejects secret-shaped keys and values transactionally. Never ingest credentials, bearer or authorization headers, tokens, passwords, private keys, connection strings with userinfo, raw environment dumps, raw command output, private configuration, or secret-bearing logs. Collection stores only allowlisted parsed structure and structured command provenance.

Site snapshots are mode `0444`; overlays are mode `0600`. Evidence files remain outside SQLite and are referenced by canonical path and digest. Those controls reduce accidental leakage but do not turn Apptainer into a security sandbox.

## Freshness and limitations

The snapshot describes what collection observed at one time. Check `status`, source ID, finalization time, collection status, and provenance before using a record. Refresh after maintenance or module changes by creating and finalizing a new snapshot. Keep prior snapshots if needed for provenance; do not mutate finalized files.

Catalog data proposes candidates. Only fresh compile, link, runtime, multi-node, application-output, and scientific validation evidence can support corresponding success claims.
