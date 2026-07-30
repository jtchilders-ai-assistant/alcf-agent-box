#!/usr/bin/env bash
# ALCF Agent container entrypoint (Option A: on top of nousresearch/hermes-agent).
#
#   1. Resolve the ALCF inference base_url from ALCF_CLUSTER.
#   2. First-run: authenticate to the ALCF Inference Service (Globus).
#      Optional: authenticate to the IRI Facility API (a SEPARATE Globus login).
#   3. Render the Hermes config with a fresh inference access token (Python, so
#      no envsubst dependency).
#   4. Seed skills + curated MEMORY.md into $HERMES_HOME (idempotent).
#   5. Background token-refresh loop (tokens last 48h; refresh every 6h).
#   6. Launch the Hermes web dashboard (the local web chat).
#
# $HERMES_HOME (/opt/data) and ~/.globus persist across restarts, so steps 1-2
# are skipped once tokens exist.
set -euo pipefail

ALCF_DIR=/opt/alcf
INFER_AUTH="$ALCF_DIR/inference_auth_token.py"
IRI_AUTH="$ALCF_DIR/alcf_facility_api_globus_token.py"
PY=/opt/hermes/.venv/bin/python
HERMES_HOME="${HERMES_HOME:-/opt/data}"
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
authed_inference() { "$PY" "$INFER_AUTH" get_access_token >/dev/null 2>&1; }

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

# --- 3. Dashboard auth (required for a container 0.0.0.0 bind) ---------------
# Hermes refuses a non-loopback dashboard bind without an auth gate. We hash a
# password at start (plaintext never persisted). Password source, in order:
#   ALCF_DASHBOARD_PASSWORD env  ->  else auto-generate one and print it once.
ALCF_DASHBOARD_USER="${ALCF_DASHBOARD_USER:-alcf}"
if [[ -z "${ALCF_DASHBOARD_PASSWORD:-}" ]]; then
  ALCF_DASHBOARD_PASSWORD="$("$PY" -c 'import secrets; print(secrets.token_urlsafe(12))')"
  log "No ALCF_DASHBOARD_PASSWORD set — generated one for this session:"
  printf '\033[33m    dashboard login:  user=%s  password=%s\033[0m\n' "$ALCF_DASHBOARD_USER" "$ALCF_DASHBOARD_PASSWORD"
  log "(set -e ALCF_DASHBOARD_PASSWORD=... to choose your own and keep it stable)"
fi
export ALCF_DASHBOARD_USER
export ALCF_DASHBOARD_PASSWORD_HASH="$("$PY" -c "from plugins.dashboard_auth.basic import hash_password; print(hash_password('$ALCF_DASHBOARD_PASSWORD'))")"

# --- 4. Render config (Python; no envsubst in the base image) ---------------
render_config() {
  local token
  token="$("$PY" "$INFER_AUTH" get_access_token)"
  ALCF_ACCESS_TOKEN="$token" \
  ALCF_BASE_URL="$ALCF_BASE_URL" \
  ALCF_MODEL="${ALCF_MODEL:-openai/gpt-oss-120b}" \
  ALCF_MAX_TOKENS="${ALCF_MAX_TOKENS:-2048}" \
  ALCF_DASHBOARD_USER="$ALCF_DASHBOARD_USER" \
  ALCF_DASHBOARD_PASSWORD_HASH="$ALCF_DASHBOARD_PASSWORD_HASH" \
  "$PY" - "$ALCF_DIR/config.template.yaml" "$CONFIG_OUT" <<'PYEOF'
import os, sys, string
src, dst = sys.argv[1], sys.argv[2]
tmpl = open(src, encoding="utf-8").read()
# Substitute ${VAR} placeholders from the environment; leave unknown ones as-is.
out = string.Template(tmpl).safe_substitute(os.environ)
open(dst, "w", encoding="utf-8").write(out)
PYEOF
}
render_config
log "Config rendered -> $CONFIG_OUT (cluster=$ALCF_CLUSTER model=${ALCF_MODEL:-openai/gpt-oss-120b})"

# --- 5. Seed skills + memory (idempotent) -----------------------------------
mkdir -p "$HERMES_HOME/skills/research"
cp -rn "$ALCF_DIR/skills/." "$HERMES_HOME/skills/research/" 2>/dev/null || true

# Built-in MEMORY.md is backend-agnostic and injected every turn. Seed only if
# the user hasn't already got one (never clobber their edits).
if [[ -f "$ALCF_DIR/memory/MEMORY.md" && ! -f "$HERMES_HOME/MEMORY.md" ]]; then
  cp "$ALCF_DIR/memory/MEMORY.md" "$HERMES_HOME/MEMORY.md"
  log "Seeded ALCF knowledge base into MEMORY.md"
fi

# --- 6. Token refresh loop (tokens last 48h; refresh every 6h) --------------
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

# --- 7. Launch: dashboard on loopback, Caddy (HTTPS) on the public port ------
# The dashboard chat's copy/paste needs a browser "secure context", so we serve
# it over HTTPS via Caddy (self-signed local cert). The dashboard itself binds
# 127.0.0.1:<internal>, and Caddy terminates TLS on the public port and proxies.
PUB_PORT="${ALCF_DASHBOARD_PORT:-8787}"
INT_PORT="${ALCF_DASHBOARD_INTERNAL_PORT:-9119}"
mkdir -p "$XDG_DATA_HOME" "$XDG_CONFIG_HOME" 2>/dev/null || true

log "Starting dashboard (internal) on 0.0.0.0:${INT_PORT} (auth gate ON)"
# Bind 0.0.0.0 so Hermes engages the auth gate (loopback bind would skip it).
# Only the public TLS port is published from the container, so the internal
# port is not directly reachable from the host — Caddy proxies to it.
hermes dashboard --host 0.0.0.0 --port "$INT_PORT" --no-open &
DASH_PID=$!

# Wait for the dashboard to come up (up to ~60s) before starting the proxy.
for i in $(seq 1 60); do
  if curl -sf -o /dev/null "http://127.0.0.1:${INT_PORT}/api/health" 2>/dev/null; then
    break
  fi
  if ! kill -0 "$DASH_PID" 2>/dev/null; then
    err "Dashboard exited during startup."; exit 1
  fi
  sleep 1
done

# Render the Caddyfile ports and launch Caddy on the public port (foreground).
export ALCF_DASHBOARD_PORT="$PUB_PORT" ALCF_DASHBOARD_INTERNAL_PORT="$INT_PORT"
"$PY" - "$ALCF_DIR/Caddyfile" /opt/data/Caddyfile <<'PYEOF'
import os, sys, string
src, dst = sys.argv[1], sys.argv[2]
open(dst, "w").write(string.Template(open(src).read()).safe_substitute(os.environ))
PYEOF

log "Web chat ready at https://localhost:${PUB_PORT}  (self-signed cert — click through the browser warning once)"
log "Login: user=${ALCF_DASHBOARD_USER}  (password you set via ALCF_DASHBOARD_PASSWORD)"
exec caddy run --config /opt/data/Caddyfile --adapter caddyfile
