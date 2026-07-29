#!/usr/bin/env bash
# ALCF Agent container entrypoint.
#
# Responsibilities:
#   1. Resolve the ALCF inference base_url from ALCF_CLUSTER.
#   2. First-run: authenticate to the ALCF Inference Service (Globus).
#      Optional: authenticate to the IRI Facility API (separate Globus login).
#   3. Render the Hermes config template with a fresh inference access token.
#   4. Seed skills + curated memory into ~/.hermes (idempotent).
#   5. Start a background token-refresh loop (tokens expire in 48h).
#   6. Launch the Hermes web dashboard (the local web chat).
#
# The ~/.hermes and ~/.globus volumes persist tokens + memory across restarts,
# so steps 1-2 are skipped once tokens exist.
set -euo pipefail

ALCF_DIR=/opt/alcf
INFER_AUTH="$ALCF_DIR/inference_auth_token.py"
IRI_AUTH="$ALCF_DIR/alcf_facility_api_globus_token.py"
PY=/opt/venv/bin/python
CONFIG_OUT="$HERMES_HOME/config.yaml"

log() { printf '\033[36m[alcf-agent]\033[0m %s\n' "$*"; }
err() { printf '\033[31m[alcf-agent]\033[0m %s\n' "$*" >&2; }

# --- 1. Resolve inference base_url ------------------------------------------
case "${ALCF_CLUSTER:-sophia}" in
  sophia) ALCF_BASE_URL="https://inference-api.alcf.anl.gov/resource_server/sophia/vllm/v1" ;;
  metis)  ALCF_BASE_URL="https://inference-api.alcf.anl.gov/resource_server/metis/api/v1" ;;
  *) err "Unknown ALCF_CLUSTER='$ALCF_CLUSTER' (expected sophia|metis)"; exit 1 ;;
esac
export ALCF_BASE_URL

mkdir -p "$HERMES_HOME"

# --- 2. First-run Globus auth (interactive, idempotent) ---------------------
# The helper stores tokens under ~/.globus; if a valid token already exists it
# returns one without prompting, so this is safe to run every start.
authed_inference() {
  "$PY" "$INFER_AUTH" get_access_token >/dev/null 2>&1
}

if ! authed_inference; then
  log "First-time setup: authenticate to the ALCF Inference Service."
  log "A URL will be printed — open it, log in with your ALCF/Globus account,"
  log "then paste the authorization code back here."
  echo
  "$PY" "$INFER_AUTH" authenticate
  echo
  if ! authed_inference; then
    err "Inference authentication failed. Re-run the container to try again."
    exit 1
  fi
  log "ALCF Inference Service authentication OK."
fi

# Optional: IRI Facility API auth (job submission / filesystem ops).
if [[ "${ALCF_ENABLE_IRI:-1}" == "1" ]]; then
  if ! "$PY" "$IRI_AUTH" get_access_token >/dev/null 2>&1; then
    log "Optional: authenticate to the IRI Facility API for job submission."
    log "This is a SEPARATE Globus login. Press Ctrl-C to skip if you only"
    log "want inference/chat."
    echo
    "$PY" "$IRI_AUTH" authenticate || log "IRI auth skipped/failed — chat still works."
    echo
  fi
fi

# --- 3. Render config -------------------------------------------------------
render_config() {
  local token
  token="$("$PY" "$INFER_AUTH" get_access_token)"
  ALCF_ACCESS_TOKEN="$token" \
  ALCF_BASE_URL="$ALCF_BASE_URL" \
  ALCF_MODEL="${ALCF_MODEL:-openai/gpt-oss-120b}" \
  ALCF_MAX_TOKENS="${ALCF_MAX_TOKENS:-2048}" \
    envsubst < "$ALCF_DIR/config.template.yaml" > "$CONFIG_OUT"
}
render_config
log "Config rendered -> $CONFIG_OUT (cluster=$ALCF_CLUSTER model=${ALCF_MODEL:-openai/gpt-oss-120b})"

# --- 4. Seed skills + memory (idempotent) -----------------------------------
mkdir -p "$HERMES_HOME/skills/research"
cp -rn "$ALCF_DIR/skills/." "$HERMES_HOME/skills/research/" 2>/dev/null || true

# Seed the curated ALCF knowledge base into built-in memory (MEMORY.md), which
# Hermes always reads and injects every turn — backend-agnostic, no import step.
# Only seed if the user hasn't already got a MEMORY.md (never clobber edits).
if [[ -f "$ALCF_DIR/memory/MEMORY.md" && ! -f "$HERMES_HOME/MEMORY.md" ]]; then
  cp "$ALCF_DIR/memory/MEMORY.md" "$HERMES_HOME/MEMORY.md"
  log "Seeded ALCF knowledge base into MEMORY.md"
fi

# --- 5. Token refresh loop (tokens last 48h; refresh every 6h) --------------
(
  while true; do
    sleep 21600
    if render_config 2>/dev/null; then
      log "Refreshed inference access token."
    else
      err "Token refresh failed — a full re-auth may be needed (30-day policy)."
    fi
  done
) &

# --- 6. Launch the web dashboard --------------------------------------------
log "Starting web chat at http://localhost:${ALCF_DASHBOARD_PORT:-8787}"
exec hermes dashboard \
  --host 0.0.0.0 \
  --port "${ALCF_DASHBOARD_PORT:-8787}" \
  --no-open
