#!/usr/bin/env bash
# Authenticate Red Shirt Polaris to ALCF services through its final SIF.
# Run interactively on a Polaris login node. Tokens persist below
# $RED_SHIRT_BASE_DIR/home and are reused by subsequent PBS jobs.
set -euo pipefail

BASE_DIR="${RED_SHIRT_BASE_DIR:-$HOME/red-shirt-polaris}"
SIF="${RED_SHIRT_SIF:-$BASE_DIR/red-shirt-polaris-current.sif}"
ACTION="${1:-authenticate}"
ENABLE_IRI="${ALCF_ENABLE_IRI:-0}"
ENABLE_COMPUTE="${ALCF_ENABLE_GLOBUS_COMPUTE:-0}"

case "$ACTION" in
  authenticate|check|status) ;;
  *)
    printf 'Usage: %s [authenticate|check|status]\n' "$0" >&2
    exit 2
    ;;
esac

if [ ! -r "$SIF" ]; then
  printf 'ERROR: Red Shirt SIF is not readable: %s\n' "$SIF" >&2
  printf 'Set RED_SHIRT_SIF to an explicit SIF path if needed.\n' >&2
  exit 1
fi

mkdir -p "$BASE_DIR/home"
chmod 700 "$BASE_DIR/home"

module use /soft/modulefiles
module load spack-pe-base
module load apptainer

AUTH_HELPER="/opt/red-shirt-polaris/alcf_combined_auth.py"
AUTH_HELPER_BIND=()
if [ -n "${RED_SHIRT_AUTH_HELPER:-}" ]; then
  if [ ! -r "$RED_SHIRT_AUTH_HELPER" ] || [ ! -f "$RED_SHIRT_AUTH_HELPER" ]; then
    printf 'ERROR: RED_SHIRT_AUTH_HELPER is not a readable file: %s\n' \
      "$RED_SHIRT_AUTH_HELPER" >&2
    exit 1
  fi
  AUTH_HELPER="/run/red-shirt-auth-helper.py"
  AUTH_HELPER_BIND=(--bind "$RED_SHIRT_AUTH_HELPER:/run/red-shirt-auth-helper.py:ro")
fi

exec apptainer exec --cleanenv \
  --env HOME=/opt/data \
  --env HERMES_HOME=/opt/data \
  --env ALCF_ENABLE_IRI="$ENABLE_IRI" \
  --env ALCF_ENABLE_GLOBUS_COMPUTE="$ENABLE_COMPUTE" \
  --bind "$BASE_DIR/home:/opt/data" \
  "${AUTH_HELPER_BIND[@]}" \
  "$SIF" \
  /opt/hermes/.venv/bin/python \
  "$AUTH_HELPER" "$ACTION"
