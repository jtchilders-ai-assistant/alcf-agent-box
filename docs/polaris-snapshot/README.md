# Polaris documentation snapshot: source and provenance index

This directory bundles the documentation Red Shirt Polaris consults before
giving Polaris-specific advice or taking a Polaris-specific action (see
`config/red-shirt-polaris/SOUL.md`). It is split into two classifications
that must never be conflated:

- **`official/`** — cleaned Markdown snapshots of real, published ALCF/Argonne
  user documentation, each retrieved directly from a canonical
  `docs.alcf.anl.gov` URL (backed by the raw Markdown source in
  `github.com/argonne-lcf/user-guides`) and recorded below with its exact
  retrieval date. Nothing in `official/` was invented, summarized, or
  paraphrased — it is the fetched page content with a small provenance
  header prepended.
- **`local/`** — this deployment's own locally measured findings about its
  own runtime behavior (network transport, proxy requirements, etc.). Local
  notes are **not** ALCF policy and are explicitly labeled as such in their
  own text; they supplement but never override or restate official guidance.

Every file below appears here with its title, canonical URL (or, for locally
measured notes, the repository-local provenance of the measurement), retrieval
date, local path, and classification. If an official topic could not be
retrieved, the gap is recorded rather than any text being fabricated to fill
it.

## Official snapshots

| Title | Canonical URL | Retrieved | Local path | Classification |
|---|---|---|---|---|
| Polaris Machine Overview | https://docs.alcf.anl.gov/polaris/ | 2026-09-16 | `official/polaris-overview.md` | official |
| Getting Started on Polaris | https://docs.alcf.anl.gov/polaris/getting-started/ | 2026-09-16 | `official/polaris-getting-started.md` | official |
| Running Jobs on Polaris | https://docs.alcf.anl.gov/polaris/running-jobs/ | 2026-09-16 | `official/polaris-running-jobs.md` | official |
| ALCF File Systems and Storage | https://docs.alcf.anl.gov/data-management/filesystem-and-storage/ | 2026-09-16 | `official/filesystems-and-storage.md` | official |
| Compiling and Linking Overview on Polaris | https://docs.alcf.anl.gov/polaris/compiling-and-linking/ | 2026-09-16 | `official/compiling-and-linking.md` | official |
| Containers on Polaris | https://docs.alcf.anl.gov/polaris/containers/containers/ | 2026-09-16 | `official/containers.md` | official |

Minimum required-topic coverage from the design spec, mapped to the snapshots
above:

- overview / getting started → `polaris-overview.md`, `polaris-getting-started.md`
- PBS job queues → `polaris-running-jobs.md` (queue table, interactive jobs,
  `qsub` usage)
- filesystems / storage → `filesystems-and-storage.md`
- modules / programming environment → `compiling-and-linking.md` (Cray PE,
  `PrgEnv-*` modules, compiler wrappers)
- containers / Apptainer → `containers.md`
- node-local storage → covered inside `filesystems-and-storage.md` under
  "Local Node SSD" (`/local/scratch`, wiped between PBS jobs, no backups)

Each file in `official/` carries an HTML-comment provenance header at the top
recording its title, canonical URL, retrieval date, classification, and
upstream source (`raw.githubusercontent.com/argonne-lcf/user-guides@main`),
matching the table above.

## Local (measured, non-official) notes

| Title | Source | Retrieved | Local path | Classification |
|---|---|---|---|---|
| Red Shirt Polaris deployment notes (measured Tailscale/proxy/A2A behavior) | Measured on this deployment; see `deploy/polaris/STATUS.md` for the raw PBS job evidence in this repository | 2026-09-16 | `local/deployment-notes.md` | local |

## Gaps

No official topic in the required minimum set was unavailable — all six
required topics above were fetched successfully from canonical
`docs.alcf.anl.gov` URLs on 2026-09-16. If a future refresh finds a page
blocked or moved, record the gap here rather than fabricating replacement
text.
