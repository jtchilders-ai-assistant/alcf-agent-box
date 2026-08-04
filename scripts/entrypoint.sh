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

# --- 0. Version banner ------------------------------------------------------
# Print the ALCF-agent-box git revision + build date baked at image build, plus
# the underlying Hermes SHA, so you can confirm exactly which image is running.
_alcf_sha="dev"; _alcf_date="unknown"
if [[ -f "$ALCF_DIR/.alcf_version" ]]; then
  _alcf_sha="$(sed -n 1p "$ALCF_DIR/.alcf_version")"
  _alcf_date="$(sed -n 2p "$ALCF_DIR/.alcf_version")"
fi
_hermes_sha="$(cat /opt/hermes/.hermes_build_sha 2>/dev/null | cut -c1-12 || true)"
printf '\033[1;36m╔══════════════════════════════════════════════════════════╗\033[0m\n'
printf '\033[1;36m║\033[0m  ALCF Agent in a Box\n'
printf '\033[1;36m║\033[0m  version : %s  (built %s)\n' "$_alcf_sha" "$_alcf_date"
printf '\033[1;36m║\033[0m  hermes  : %s\n' "${_hermes_sha:-unknown}"
printf '\033[1;36m╚══════════════════════════════════════════════════════════╝\033[0m\n'
# Drop a chat-readable copy on the volume (refreshed every start so it always
# reflects the running image; the agent reads it when asked "what version?").
mkdir -p "${HERMES_HOME:-/opt/data}" 2>/dev/null || true
printf 'ALCF Agent in a Box\nalcf-agent-box commit: %s\nbuilt: %s\nhermes commit: %s\nrepo: https://github.com/jtchilders-ai-assistant/alcf-agent-box\n' \
  "$_alcf_sha" "$_alcf_date" "${_hermes_sha:-unknown}" > "${HERMES_HOME:-/opt/data}/ALCF_VERSION.txt" 2>/dev/null || true

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

# Optional: Globus Compute auth (remote-bash: build/run software on ALCF compute
# nodes). This is a THIRD, separate Globus login. ON by default (consistent with
# IRI); it runs code under the user's allocation, so it can be hard-disabled with
# ALCF_ENABLE_GLOBUS_COMPUTE=0. Destructive commands still require --yes at run time.
REMOTE_BASH="$ALCF_DIR/alcf_remote_bash.py"
if [[ "${ALCF_ENABLE_GLOBUS_COMPUTE:-1}" == "1" ]]; then
  # `check` exits 0 only when enabled AND a Globus Compute login exists; when we
  # are here (enabled) a non-zero exit means the login is missing -> prompt.
  if ! "$PY" "$REMOTE_BASH" check >/dev/null 2>&1; then
    log "Optional: authenticate to Globus Compute (lets the agent build/run"
    log "software on ALCF compute nodes). This is a SEPARATE (third) Globus login."
    log "Press Ctrl-C to skip if you only want inference/chat + IRI."
    echo
    "$PY" "$REMOTE_BASH" authenticate || log "Globus Compute auth skipped/failed — remote-bash unavailable until you run it."
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
  ALCF_MODEL="${ALCF_MODEL:-google/gemma-4-31B-it}" \
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
log "Config rendered -> $CONFIG_OUT (cluster=$ALCF_CLUSTER model=${ALCF_MODEL:-google/gemma-4-31B-it})"

# --- 5. Seed / refresh skills + memory ---------------------------------------
# ALCF skills and the knowledge base are IMAGE-MANAGED: we refresh them from the
# image on every start so knowledge-base fixes land for users who keep their
# existing volume — WITHOUT clobbering user edits. We track the checksum of what
# we last wrote; if the on-disk copy still matches that (user hasn't touched it)
# we overwrite with the image's newer version, otherwise we leave the user's
# edited copy alone and log a note.
STAMP_DIR="$HERMES_HOME/.alcf_seed_stamps"
mkdir -p "$STAMP_DIR" "$HERMES_HOME/skills/research" "$HERMES_HOME/memories"

managed_seed() {  # $1=source file, $2=dest file, $3=label
  local src="$1" dst="$2" label="$3" stamp
  stamp="$STAMP_DIR/$(echo "$dst" | md5sum | cut -d' ' -f1).sha"
  [[ -f "$src" ]] || return 0
  if [[ ! -f "$dst" ]]; then
    cp "$src" "$dst"; sha256sum "$src" | cut -d' ' -f1 > "$stamp"
    log "Seeded $label"
  elif [[ -f "$stamp" ]] && sha256sum -c <(echo "$(cat "$stamp")  $dst") >/dev/null 2>&1; then
    # on-disk copy is byte-identical to what we last seeded => safe to refresh
    if ! cmp -s "$src" "$dst"; then
      cp "$src" "$dst"; sha256sum "$src" | cut -d' ' -f1 > "$stamp"
      log "Updated $label from image"
    fi
  else
    log "Kept your edited $label (image has a newer version at $src)"
  fi
}

managed_seed "$ALCF_DIR/memory/MEMORY.md" "$HERMES_HOME/memories/MEMORY.md" "ALCF knowledge base (memories/MEMORY.md)"
managed_seed "$ALCF_DIR/SOUL.md" "$HERMES_HOME/SOUL.md" "ALCF Agent identity (SOUL.md)"
# Skills: refresh each SKILL.md tree wholesale when unmodified (skills are
# reference material, not typically user-edited). Simple approach: mirror the
# baked skills dir, overwriting — skills live under skills/research/ which is
# image-owned here.
cp -r "$ALCF_DIR/skills/." "$HERMES_HOME/skills/research/" 2>/dev/null || true

# --- 6. Token refresh loop (tokens last 48h; refresh every 6h) --------------
# The inference token IS the api_key, and it rotates. We re-render the config
# with a fresh token every 6h. If a refresh FAILS, the token has almost
# certainly hit the 30-day hard re-auth limit — from then on every LLM call
# will fail upstream (typically HTTP 401). A chat-only user never sees the
# container log, so we (a) print a loud, actionable banner to the log AND (b)
# drop a status file on the volume that the agent reads and surfaces IN CHAT
# (see memory/MEMORY.md → "Inference token expiry"). On success we clear it.
TOKEN_STATUS="$HERMES_HOME/.inference_token_status"
: > "$TOKEN_STATUS" 2>/dev/null || true   # start clean (empty = healthy)
reauth_cmd="docker exec -it <container> \\
  /opt/hermes/.venv/bin/python /opt/alcf/inference_auth_token.py authenticate"
(
  while true; do
    sleep 21600
    if render_config 2>/dev/null; then
      log "Refreshed inference access token."
      : > "$TOKEN_STATUS" 2>/dev/null || true   # healthy again
    else
      err "╔═══════════════════════════════════════════════════════════════╗"
      err "║  INFERENCE TOKEN REFRESH FAILED                               ║"
      err "║  Your ALCF inference login has expired (30-day re-auth limit). ║"
      err "║  The agent's LLM calls will fail until you re-authenticate.    ║"
      err "║  Run this on your host, then the agent works again:           ║"
      err "╚═══════════════════════════════════════════════════════════════╝"
      err "    $reauth_cmd"
      # Machine-readable note the agent reads to explain the failure in chat.
      printf 'EXPIRED\n%s\n' \
        "Inference Globus login expired (30-day re-auth). Re-authenticate on the host: $reauth_cmd" \
        > "$TOKEN_STATUS" 2>/dev/null || true
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
