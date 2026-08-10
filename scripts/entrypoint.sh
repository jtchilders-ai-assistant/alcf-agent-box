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
  local token ctxlen providers_block
  token="$("$PY" "$INFER_AUTH" get_access_token)"
  # Resolve the REAL serving context window for the LAUNCH model from the
  # server's max_model_len (ALCF caps some models below their published spec;
  # Hermes' family table would otherwise over-estimate it). Falls back to
  # $ALCF_CONTEXT_LENGTH / 128000 if the lookup fails. Never fatal.
  ctxlen="$(ALCF_INFER_AUTH="$INFER_AUTH" ALCF_PY="$PY" \
            "$PY" "$ALCF_DIR/resolve_context_length.py" \
            "$ALCF_BASE_URL" "${ALCF_MODEL:-google/gemma-4-31B-it}" 2>>/tmp/ctxlen.log)"
  # Guard: if the resolver printed nothing usable, hard-default here too.
  case "$ctxlen" in ''|*[!0-9]*) ctxlen=128000 ;; esac
  log "Model context window: $ctxlen tokens (from max_model_len; see /tmp/ctxlen.log)"

  # Generate the switchable-model `custom_providers:` block from the LIVE ALCF
  # catalog on BOTH clusters (sophia + metis). populate_models.py filters to chat
  # models and assigns each its real serving context window; it always prints a
  # valid block (committed static fallback on any discovery failure), so this is
  # never fatal. ALCF_ENABLE_METIS=0 drops the Metis provider.
  providers_block="$(ALCF_INFER_AUTH="$INFER_AUTH" ALCF_PY="$PY" \
            ALCF_ENABLE_METIS="${ALCF_ENABLE_METIS:-1}" \
            "$PY" "$ALCF_DIR/populate_models.py" 2>>/tmp/populate_models.log)"
  # Guard: a generated block MUST start with 'custom_providers:'. If somehow it
  # didn't (script crashed hard), leave $providers_block empty so the splice step
  # keeps the template's committed static fallback instead.
  case "$providers_block" in
    custom_providers:*) log "Model list generated from live catalog (see /tmp/populate_models.log)" ;;
    *) providers_block=""; err "populate_models produced no block; keeping static fallback list" ;;
  esac

  # --- Launch-model context floor guard ------------------------------------
  # Hermes hard-refuses to start any model whose context window is below its
  # MINIMUM_CONTEXT_LENGTH (64000). The ALCF gateway caps many models well below
  # that (all Llama 3.x/4, Mixtral, Devstral, Mistral-Large-2407 serve at
  # 16k-32k), so a user who overrides ALCF_MODEL to one of those would otherwise
  # hit a raw Hermes stacktrace at launch. Refuse EARLY with an actionable list
  # of valid models instead.
  #
  # ROBUST-AGAINST-ITS-OWN-FAILURE by construction:
  #   * $ctxlen was already defaulted to 128000 on ANY resolver failure (see the
  #     `case` above), and 128000 >= floor, so a broken/unreachable resolver
  #     NEVER trips this guard -- it fails OPEN (launch proceeds), never closed.
  #   * We only block on a genuine, positive sub-floor integer reading.
  #   * The valid-model list is derived from the block we just generated; if that
  #     extraction yields nothing (block empty/unparseable) we fall back to a
  #     hardcoded hint so the message is always useful. No step here can abort
  #     the launch except the one intentional `exit 78`.
  local floor=64000 launch_model valid_list
  launch_model="${ALCF_MODEL:-google/gemma-4-31B-it}"
  # Compare defensively: only treat $ctxlen as sub-floor when it is a clean
  # positive integer AND strictly less than the floor. `case` already guarantees
  # $ctxlen is all-digits, but re-check so this block is safe if moved.
  if printf '%s' "$ctxlen" | grep -Eq '^[0-9]+$' && [ "$ctxlen" -gt 0 ] && [ "$ctxlen" -lt "$floor" ]; then
    # Extract the sophia provider's model ids from the generated block for the
    # hint. `|| true` so a grep miss can't abort under `set -e`.
    valid_list="$(printf '%s\n' "$providers_block" \
      | sed -n '/name: alcf-sophia/,/name: alcf-metis/p' \
      | grep -E '^      [A-Za-z0-9].*:$' \
      | sed -E 's/^ +//; s/:$//' \
      | sort | awk 'NR>1{printf ", "}{printf "%s",$0}END{print ""}' 2>/dev/null || true)"
    if [ -z "$valid_list" ]; then
      valid_list="openai/gpt-oss-120b, openai/gpt-oss-20b, google/gemma-4-31B-it, google/gemma-4-26B-A4B-it, nvidia/nemotron-3-super-120b, argonne/AuroraGPT-IT-v4-0125"
    fi
    err "======================================================================"
    err "REFUSING TO START: model '$launch_model' has a context window of"
    err "$ctxlen tokens on the ALCF gateway, below Hermes' required minimum of"
    err "$floor tokens (Hermes rejects smaller windows at load)."
    err ""
    err "The ALCF inference service caps many models (all Llama 3.x/4, Mixtral,"
    err "Devstral, Mistral-Large-2407) below 64k, which makes them unusable in"
    err "Hermes regardless of their published spec."
    err ""
    err "Set ALCF_MODEL to one of these >=64k models and restart the container:"
    err "  $valid_list"
    err "======================================================================"
    exit 78   # EX_CONFIG: launch configuration is invalid
  fi
  # --- end launch-model context floor guard --------------------------------

  # Render: substitute ${...} placeholders, and splice the generated providers
  # block over the sentinel-to-EOF region of the template. The generated block
  # still contains ${ALCF_ACCESS_TOKEN}, so we splice FIRST then substitute.
  ALCF_ACCESS_TOKEN="$token" \
  ALCF_BASE_URL="$ALCF_BASE_URL" \
  ALCF_MODEL="${ALCF_MODEL:-google/gemma-4-31B-it}" \
  ALCF_MAX_TOKENS="${ALCF_MAX_TOKENS:-2048}" \
  ALCF_CONTEXT_LENGTH="$ctxlen" \
  ALCF_DASHBOARD_USER="$ALCF_DASHBOARD_USER" \
  ALCF_DASHBOARD_PASSWORD_HASH="$ALCF_DASHBOARD_PASSWORD_HASH" \
  ALCF_PROVIDERS_BLOCK="$providers_block" \
  "$PY" - "$ALCF_DIR/config.template.yaml" "$CONFIG_OUT" <<'PYEOF'
import os, sys, string
src, dst = sys.argv[1], sys.argv[2]
tmpl = open(src, encoding="utf-8").read()

# Splice: replace from the sentinel line (inclusive) to EOF with the generated
# custom_providers block, when one was produced. The sentinel is a YAML comment,
# so the template stays valid even if this splice is skipped.
SENTINEL = "#__ALCF_CUSTOM_PROVIDERS__"
block = os.environ.get("ALCF_PROVIDERS_BLOCK", "").strip()
if block:
    idx = tmpl.find(SENTINEL)
    if idx != -1:
        # cut back to the start of the sentinel's line
        line_start = tmpl.rfind("\n", 0, idx) + 1
        tmpl = tmpl[:line_start] + block + "\n"
    else:
        sys.stderr.write("[entrypoint] sentinel not found; using template as-is\n")

# Substitute ${VAR} placeholders from the environment; leave unknown ones as-is.
out = string.Template(tmpl).safe_substitute(os.environ)
open(dst, "w", encoding="utf-8").write(out)
PYEOF
}
render_config
log "Config rendered -> $CONFIG_OUT (cluster=$ALCF_CLUSTER model=${ALCF_MODEL:-google/gemma-4-31B-it})"

# --- 4b. Model warm-up (hot/cold) status banner -----------------------------
# The model dropdown lists the full ALCF catalog, but ALCF only keeps a subset
# loaded on GPU at any time. Selecting a "cold" model triggers a 10-15 min load
# and returns HTTP 503 "online but not ready" in the meantime -- which looks
# like a failure but is just warm-up. Print which offered models are hot now so
# the user can pick an instant one or know to wait. Purely informational and
# best-effort: never fatal (|| true), and the script itself reports "unknown"
# rather than guessing if /jobs is unreachable.
if [ "${ALCF_SHOW_MODEL_STATUS:-1}" != "0" ]; then
  ALCF_INFER_AUTH="$INFER_AUTH" ALCF_PY="$PY" \
    "$PY" "$ALCF_DIR/populate_models.py" --hot-report 2>>/tmp/populate_models.log \
    | while IFS= read -r line; do log "$line"; done || true
fi

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

# Some Hermes code paths (config load during startup) write a STOCK default
# SOUL.md into $HERMES_HOME before this seed step runs, and older volumes carry
# one from a pre-ALCF-SOUL image. A stock/legacy SOUL.md has no ALCF seed stamp,
# so managed_seed would treat it as "user-edited" and KEEP it -- the ALCF
# identity would never land, and a fresh "what can you do?" would answer as
# generic Hermes with no mention of ALCF. Detect the stock/legacy Hermes SOUL
# (which carries zero ALCF/user intent) and delete it so managed_seed can seed
# the real ALCF identity. This is order-independent: it fixes both the
# fresh-volume race and stale volumes. A genuinely user-customized SOUL.md
# (anything not matching these known-stock signatures) is left untouched.
is_stock_hermes_soul() {  # $1=path -> exit 0 if it's the stock/legacy Hermes default
  local f="$1"
  [[ -f "$f" ]] || return 1
  # Signature 1: the current DEFAULT_SOUL_MD one-liner (identity by its opening).
  if head -c 200 "$f" | grep -q "You are Hermes Agent, an intelligent AI assistant created by Nous Research"; then
    return 0
  fi
  # Signature 2: the legacy comment-only scaffold (no persona text at all) --
  # every non-blank line is an HTML comment / markdown heading, no real content.
  if grep -q "This file defines the agent's personality and tone" "$f" 2>/dev/null; then
    return 0
  fi
  return 1
}
SOUL_DST="$HERMES_HOME/SOUL.md"
SOUL_STAMP="$STAMP_DIR/$(echo "$SOUL_DST" | md5sum | cut -d' ' -f1).sha"
if [[ -f "$SOUL_DST" && ! -f "$SOUL_STAMP" ]] && is_stock_hermes_soul "$SOUL_DST"; then
  rm -f "$SOUL_DST"
  log "Replacing stock Hermes SOUL.md with the ALCF Agent identity"
fi

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
