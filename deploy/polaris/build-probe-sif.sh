#!/usr/bin/env bash
# Build the immutable Headscale probe OCI image as an Apptainer SIF on Polaris.
set -euo pipefail

IMAGE="ghcr.io/jtchilders-ai-assistant/alcf-agent-headscale-probe:sha-d26b9fd"
EXPECTED_DIGEST="sha256:e39f7851e4bac35fe508e870e1439ddd08916751268305deff0f2b52e18d7e46"
OUT_DIR="${OUT_DIR:-$HOME/polaris-headscale-preflight}"
SIF="${SIF:-$OUT_DIR/alcf-headscale-probe-sha-d26b9fd.sif}"
JOB_TAG="${PBS_JOBID:-manual-$$}"
SCRATCH_ROOT="/local/scratch/${USER}/headscale-probe-${JOB_TAG%%.*}"

ml use /soft/modulefiles
ml spack-pe-base
ml apptainer

mkdir -p "$OUT_DIR" "$SCRATCH_ROOT/tmp" "$SCRATCH_ROOT/cache"
export APPTAINER_TMPDIR="$SCRATCH_ROOT/tmp"
export APPTAINER_CACHEDIR="$SCRATCH_ROOT/cache"
export HTTP_PROXY="${HTTP_PROXY:-http://proxy.alcf.anl.gov:3128}"
export HTTPS_PROXY="${HTTPS_PROXY:-$HTTP_PROXY}"
export http_proxy="$HTTP_PROXY"
export https_proxy="$HTTPS_PROXY"

printf 'source_image=%s\nsource_manifest_digest=%s\n' "$IMAGE" "$EXPECTED_DIGEST"
apptainer build --force \
  --mksquashfs-args "-processors 4 -mem 4G" \
  "$SIF" "docker://$IMAGE"
apptainer inspect "$SIF" >/dev/null
apptainer exec "$SIF" tailscale version
sha256sum "$SIF" | tee "${SIF}.sha256"
printf 'sif=%s\n' "$SIF"
