#!/usr/bin/env bash
# Build the immutable Red Shirt Polaris OCI image as an Apptainer SIF on
# Polaris.
#
# Design: docs/superpowers/specs/2026-09-16-red-shirt-polaris-design.md
# Plan:   docs/superpowers/plans/2026-09-16-red-shirt-polaris.md (Task 6)
#
# Fail-closed image pin
# ----------------------
# This task cannot know the real CI-published GHCR tag/digest for
# Dockerfile.red-shirt-polaris at this commit -- Task 1's CI job publishes
# it, and that publish has not happened as part of this task. RED_SHIRT_IMAGE
# and RED_SHIRT_DIGEST therefore have NO default: `${VAR:?msg}` aborts
# immediately with an actionable message if either is unset, rather than
# silently falling back to `latest` or a stale/guessed digest. Set both
# explicitly once CI has published the image for the commit you are
# deploying:
#
#   export RED_SHIRT_IMAGE=ghcr.io/<owner>/alcf-red-shirt-polaris:sha-<short-sha>
#   export RED_SHIRT_DIGEST=sha256:<manifest-digest-from-the-CI-run>
#   bash deploy/polaris/build-red-shirt-sif.sh
set -euo pipefail

RED_SHIRT_IMAGE="${RED_SHIRT_IMAGE:?RED_SHIRT_IMAGE is not set. Publish Dockerfile.red-shirt-polaris via CI first, then export RED_SHIRT_IMAGE=ghcr.io/<owner>/alcf-red-shirt-polaris:sha-<short-sha> (never 'latest').}"
RED_SHIRT_DIGEST="${RED_SHIRT_DIGEST:?RED_SHIRT_DIGEST is not set. Record the CI-published OCI manifest digest and export RED_SHIRT_DIGEST=sha256:<digest> -- never build from a mutable tag alone.}"

case "$RED_SHIRT_DIGEST" in
  sha256:*) : ;;
  *) printf 'ERROR: RED_SHIRT_DIGEST must be of the form sha256:<hex>, got: %s\n' "$RED_SHIRT_DIGEST" >&2; exit 1 ;;
esac
case "$RED_SHIRT_IMAGE" in
  *:latest) printf 'ERROR: RED_SHIRT_IMAGE must not be the mutable "latest" tag: %s\n' "$RED_SHIRT_IMAGE" >&2; exit 1 ;;
esac

# Pin by BOTH tag and digest -- the tag is a human-readable label, the digest
# is the immutable content address actually fetched.
PINNED_IMAGE="${RED_SHIRT_IMAGE}@${RED_SHIRT_DIGEST}"

OUT_DIR="${OUT_DIR:-$HOME/red-shirt-polaris}"
SIF_TAG="$(printf '%s' "$RED_SHIRT_IMAGE" | sed 's/.*://')"
SIF="${SIF:-$OUT_DIR/red-shirt-polaris-${SIF_TAG}.sif}"
JOB_TAG="${PBS_JOBID:-manual-$$}"
SCRATCH_ROOT="/local/scratch/${USER}/red-shirt-build-${JOB_TAG%%.*}"

cleanup_scratch() {
  rm -rf -- "$SCRATCH_ROOT"
}
trap cleanup_scratch EXIT

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

printf 'source_image=%s\nsource_manifest_digest=%s\npinned_ref=%s\n' \
  "$RED_SHIRT_IMAGE" "$RED_SHIRT_DIGEST" "$PINNED_IMAGE"

apptainer build --force \
  --mksquashfs-args "-processors 4 -mem 4G" \
  "$SIF" "docker://$PINNED_IMAGE"

apptainer inspect "$SIF" >/dev/null
sha256sum "$SIF" | tee "${SIF}.sha256"

# In-SIF executable version checks -- confirm the image actually carries a
# working Hermes and Tailscale, not just that the build step exited 0.
apptainer exec "$SIF" hermes --version
apptainer exec "$SIF" tailscale version

printf 'sif=%s\n' "$SIF"
