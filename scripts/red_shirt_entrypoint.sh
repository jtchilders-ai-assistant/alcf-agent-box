#!/usr/bin/env bash
# Red Shirt Polaris compute entrypoint.
#
# Design: docs/superpowers/specs/2026-09-16-red-shirt-polaris-design.md
# Plan:   docs/superpowers/plans/2026-09-16-red-shirt-polaris.md (Task 5/7B)
#
# Two modes:
#   RED_SHIRT_TEST_MODE=1  -> minimal harness mode (Task 5 slice): own the
#       Hermes child (+ optional support child) directly from
#       RED_SHIRT_HERMES_CMD/RED_SHIRT_SUPPORT_CMD, no networking, no
#       readiness gates, no content seeding. Preserved verbatim for the
#       existing lifecycle tests.
#   default (production path) -> a fail-closed job-parent/job-root guard
#       (current-uid ownership of RED_SHIRT_JOB_PARENT and any pre-existing
#       RED_SHIRT_JOB_ROOT, no symlink components, strict descendant
#       relationship, no overlap with HERMES_HOME) runs before any child
#       process is launched or any filesystem path is created/chmod/removed,
#       then the full ordered startup sequence: verify mounted credentials,
#       stand up the selective CONNECT proxy and userspace tailscaled, join
#       Headscale, configure Serve, render Hermes config with a live-model
#       inference smoke test, seed managed content, launch `hermes gateway
#       run` in the foreground, verify the Agent Card both locally and
#       through the tailnet path, emit READY, then on exit/signal tear
#       everything down (only the guard-validated job root is ever removed)
#       and emit a terminal record.
set -euo pipefail
umask 077

# ===========================================================================
# Mode selector
# ===========================================================================
if [ "${RED_SHIRT_TEST_MODE:-0}" = "1" ]; then

  if [ -z "${RED_SHIRT_HERMES_CMD:-}" ]; then
    echo "red_shirt_entrypoint: RED_SHIRT_HERMES_CMD is required" >&2
    exit 2
  fi

  JOB_ROOT="${RED_SHIRT_JOB_ROOT:-}"
  TERM_TIMEOUT="${RED_SHIRT_TERM_TIMEOUT:-10}"
  TERMINAL_OUTPUT="${RED_SHIRT_TERMINAL_OUTPUT:-}"
  READY_OUTPUT="${RED_SHIRT_READY_OUTPUT:-}"
  SUPPORT_CMD="${RED_SHIRT_SUPPORT_CMD:-}"

  declare -a OWNED_PIDS=()
  HERMES_PID=""
  SUPPORT_PID=""
  FINALIZED=0

  terminate_owned() {
    local pid alive waited
    for pid in "${OWNED_PIDS[@]}"; do
      if [ -n "$pid" ] && kill -0 "$pid" 2>/dev/null; then
        kill -TERM "$pid" 2>/dev/null || true
      fi
    done
    waited=0
    while [ "$waited" -lt "$TERM_TIMEOUT" ]; do
      alive=0
      for pid in "${OWNED_PIDS[@]}"; do
        if [ -n "$pid" ] && kill -0 "$pid" 2>/dev/null; then
          alive=1
        fi
      done
      if [ "$alive" -eq 0 ]; then
        break
      fi
      sleep 1
      waited=$((waited + 1))
    done
    for pid in "${OWNED_PIDS[@]}"; do
      if [ -n "$pid" ] && kill -0 "$pid" 2>/dev/null; then
        kill -KILL "$pid" 2>/dev/null || true
      fi
    done
    for pid in "${OWNED_PIDS[@]}"; do
      if [ -n "$pid" ]; then
        wait "$pid" 2>/dev/null || true
      fi
    done
  }

  finalize() {
    local code="$1"
    if [ "$FINALIZED" -eq 1 ]; then
      exit "$code"
    fi
    FINALIZED=1
    terminate_owned
    if [ -n "$JOB_ROOT" ] && [ -d "$JOB_ROOT" ]; then
      rm -rf -- "$JOB_ROOT"
    fi
    if [ -n "$TERMINAL_OUTPUT" ]; then
      local ok="true"
      if [ "$code" -ne 0 ]; then
        ok="false"
      fi
      printf '{"exit_code": %d, "ok": %s}\n' "$code" "$ok" > "$TERMINAL_OUTPUT"
    fi
    exit "$code"
  }

  on_signal() {
    finalize 143
  }
  trap on_signal TERM INT

  "$RED_SHIRT_HERMES_CMD" &
  HERMES_PID=$!
  OWNED_PIDS+=("$HERMES_PID")

  if [ -n "$SUPPORT_CMD" ]; then
    "$SUPPORT_CMD" &
    SUPPORT_PID=$!
    OWNED_PIDS+=("$SUPPORT_PID")
  fi

  if [ -n "$READY_OUTPUT" ]; then
    printf '{"ok": true}\n' > "$READY_OUTPUT"
  fi

  SUPPORT_DIED=0
  if [ -n "$SUPPORT_PID" ]; then
    while true; do
      if ! kill -0 "$HERMES_PID" 2>/dev/null; then
        break
      fi
      if ! kill -0 "$SUPPORT_PID" 2>/dev/null; then
        SUPPORT_DIED=1
        break
      fi
      sleep 0.2
    done
  fi

  if [ "$SUPPORT_DIED" -eq 1 ]; then
    finalize 1
  fi

  set +e
  wait "$HERMES_PID"
  HERMES_CODE=$?
  set -e
  finalize "$HERMES_CODE"
fi

# ===========================================================================
# Production path
# ===========================================================================

RS_DIR="${RED_SHIRT_DIR:-/opt/red-shirt-polaris}"
RS_HOME="${RED_SHIRT_HOME:-${HERMES_HOME:-/opt/data}}"

# Fail-closed production job-root guard. RED_SHIRT_TEST_MODE=1 keeps the
# legacy minimal behavior used by pre-existing lifecycle tests; every other
# invocation must pass this validation, in full, before any child process is
# launched or any filesystem path is created/chmod/removed.
if [ "${RED_SHIRT_TEST_MODE:-}" != "1" ]; then
  if ! CANON_JOB_ROOT=$(python3 - "${RED_SHIRT_JOB_PARENT:-}" "${RED_SHIRT_JOB_ROOT:-}" "${HERMES_HOME:-}" <<'PYEOF'
import os
import sys

job_parent, job_root, hermes_home = sys.argv[1:4]


def fail(msg):
    print("red_shirt_entrypoint: " + msg, file=sys.stderr)
    sys.exit(1)


for name, val in (
    ("RED_SHIRT_JOB_PARENT", job_parent),
    ("RED_SHIRT_JOB_ROOT", job_root),
    ("HERMES_HOME", hermes_home),
):
    if not val:
        fail(f"{name} must be a nonempty absolute path")
    if not os.path.isabs(val):
        fail(f"{name} must be an absolute path")
    if os.path.normpath(val) == "/":
        fail(f"{name} must not be '/'")


def iter_prefixes(path):
    p = path.rstrip("/")
    parts = [part for part in p.split("/") if part]
    cur = ""
    for part in parts:
        cur += "/" + part
        yield cur


def find_symlink_component(path):
    for prefix in iter_prefixes(path):
        if os.path.islink(prefix):
            return prefix
    return None

link = find_symlink_component(job_parent)
if link:
    fail(f"RED_SHIRT_JOB_PARENT contains a symlink component: {link}")

if not os.path.isdir(job_parent):
    fail("RED_SHIRT_JOB_PARENT must be an existing directory")

parent_st = os.stat(job_parent)
if parent_st.st_uid != os.getuid():
    fail("RED_SHIRT_JOB_PARENT exists but is not owned by the current user")

link = find_symlink_component(job_root)
if link:
    fail(f"RED_SHIRT_JOB_ROOT contains a symlink component: {link}")

if os.path.lexists(job_root):
    if os.path.islink(job_root):
        fail("RED_SHIRT_JOB_ROOT must not be a symlink")
    if not os.path.isdir(job_root):
        fail("RED_SHIRT_JOB_ROOT exists and is not a directory")
    st = os.stat(job_root)
    if st.st_uid != os.getuid():
        fail("RED_SHIRT_JOB_ROOT exists but is not owned by the current user")

canon_parent = os.path.realpath(job_parent)
canon_root = os.path.realpath(job_root)
canon_home = os.path.realpath(hermes_home)

rel = os.path.relpath(canon_root, canon_parent)
if rel == "." or rel.startswith(".."):
    fail("RED_SHIRT_JOB_ROOT must be a strict descendant of RED_SHIRT_JOB_PARENT")


def overlaps(a, b):
    if a == b:
        return True
    if not os.path.relpath(a, b).startswith(".."):
        return True
    if not os.path.relpath(b, a).startswith(".."):
        return True
    return False

if overlaps(canon_root, canon_home):
    fail("RED_SHIRT_JOB_ROOT must not overlap with HERMES_HOME")

print(canon_root)
PYEOF
  ); then
    exit 2
  fi
  JOB_ROOT="$CANON_JOB_ROOT"
  mkdir -m 0700 -p -- "$JOB_ROOT"
fi

CONFIG_PY="${RED_SHIRT_CONFIG_PY:-$RS_DIR/red_shirt_config.py}"
PROBE_PY="${RED_SHIRT_PROBE_PY:-$RS_DIR/red_shirt_probe.py}"
CONNECT_PROXY_PY="${RED_SHIRT_CONNECT_PROXY_PY:-$RS_DIR/connect_proxy.py}"
PYTHON_BIN="${RED_SHIRT_PYTHON:-python3}"

TAILSCALED_BIN="${RED_SHIRT_TAILSCALED_BIN:-tailscaled}"
TAILSCALE_BIN="${RED_SHIRT_TAILSCALE_BIN:-tailscale}"
HERMES_BIN="${RED_SHIRT_HERMES_BIN:-hermes}"

HEADSCALE_KEY_FILE="${RED_SHIRT_HEADSCALE_KEY_FILE:-}"
HEADSCALE_CA_FILE="${RED_SHIRT_HEADSCALE_CA_FILE:-}"
INBOUND_A2A_FILE="${RED_SHIRT_INBOUND_A2A_FILE:-}"
OUTBOUND_A2A_FILE="${RED_SHIRT_OUTBOUND_A2A_FILE:-}"
TOKEN_HELPER="${RED_SHIRT_TOKEN_HELPER:-}"

TEMPLATE="${RED_SHIRT_TEMPLATE:-$RS_DIR/config/config.template.yaml}"
CLUSTER="${RED_SHIRT_CLUSTER:-sophia}"
PREFERRED_MODEL="${RED_SHIRT_PREFERRED_MODEL:-}"
A2A_PORT="${RED_SHIRT_A2A_PORT:-9900}"
WESLEY_URL="${RED_SHIRT_WESLEY_URL:-}"
HEADSCALE_URL="${RED_SHIRT_HEADSCALE_URL:-}"
TS_HOSTNAME="${RED_SHIRT_HOSTNAME:-$(hostname)}"

ALCF_PROXY="${RED_SHIRT_ALCF_PROXY:-proxy.alcf.anl.gov:3128}"
ALCF_PROXY_URL="${RED_SHIRT_ALCF_PROXY_URL:-http://$ALCF_PROXY}"
CONNECT_PROXY_PORT="${RED_SHIRT_CONNECT_PROXY_PORT:-18443}"
TS_OUTBOUND_HTTP_PORT="${RED_SHIRT_TS_OUTBOUND_HTTP_PORT:-1056}"
TS_STATE_DIR="${RED_SHIRT_TS_STATE_DIR:-$JOB_ROOT/ts-state}"
TS_SOCKET="${RED_SHIRT_TS_SOCKET:-$JOB_ROOT/tailscaled.sock}"

TS_UP_TIMEOUT="${RED_SHIRT_TS_UP_TIMEOUT:-60}"
TS_RUNNING_TIMEOUT="${RED_SHIRT_TS_RUNNING_TIMEOUT:-60}"
CARD_TIMEOUT="${RED_SHIRT_CARD_TIMEOUT:-30}"
TERM_TIMEOUT="${RED_SHIRT_TERM_TIMEOUT:-10}"

READY_OUTPUT="${RED_SHIRT_READY_OUTPUT:-}"
TERMINAL_OUTPUT="${RED_SHIRT_TERMINAL_OUTPUT:-}"

REVISION="unknown"
if [ -f "$RS_DIR/REVISION" ]; then
  REVISION="$(cat "$RS_DIR/REVISION" 2>/dev/null || echo unknown)"
fi

case "$CLUSTER" in
  sophia)  ALCF_BASE_URL="https://inference-api.alcf.anl.gov/resource_server/sophia/vllm/v1" ;;
  metis)   ALCF_BASE_URL="https://inference-api.alcf.anl.gov/resource_server/metis/api/v1" ;;
  minerva) ALCF_BASE_URL="https://inference-api.alcf.anl.gov/resource_server/minerva/vllm/v1" ;;
  *) echo "red_shirt_entrypoint: unknown RED_SHIRT_CLUSTER '$CLUSTER'" >&2; exit 78 ;;
esac
# Test-only override: point the pre-Hermes inference smoke test at a fake
# local server instead of the real ALCF endpoint. Unset in production.
if [ -n "${RED_SHIRT_ALCF_BASE_URL_OVERRIDE:-}" ]; then
  ALCF_BASE_URL="$RED_SHIRT_ALCF_BASE_URL_OVERRIDE"
fi

declare -a MODEL_PREFERENCE_ARGS=()
if [ -n "${RED_SHIRT_MODEL_PREFERENCE:-}" ]; then
  # shellcheck disable=SC2206
  _pref_list=(${RED_SHIRT_MODEL_PREFERENCE})
  for _p in "${_pref_list[@]}"; do
    MODEL_PREFERENCE_ARGS+=("--model-preference" "$_p")
  done
fi

log() { printf '[red-shirt] %s\n' "$*" >&2; }

# ---------------------------------------------------------------------------
# Owned-child bookkeeping (never process-grep/process-kill; only PIDs we
# started ourselves are ever tracked or signalled)
# ---------------------------------------------------------------------------
declare -a OWNED_PIDS=()
declare -a OWNED_LABELS=()
CONNECT_PROXY_PID=""
TAILSCALED_PID=""
HERMES_PID=""
FINALIZED=0
SELECTED_MODEL=""

own_child() {  # $1=pid $2=label
  OWNED_PIDS+=("$1")
  OWNED_LABELS+=("$2")
}

_wait_for() {  # $1=timeout_seconds $2=check-command (eval'd)
  local timeout="$1" check="$2" waited=0
  while [ "$waited" -lt "$timeout" ]; do
    if eval "$check"; then
      return 0
    fi
    sleep 1
    waited=$((waited + 1))
  done
  return 1
}

port_open() {  # $1=port
  "$PYTHON_BIN" - "$1" <<'PYEOF'
import socket, sys
s = socket.socket()
s.settimeout(0.5)
rc = s.connect_ex(("127.0.0.1", int(sys.argv[1])))
s.close()
sys.exit(0 if rc == 0 else 1)
PYEOF
}

terminate_owned() {
  local i pid alive waited
  for i in "${!OWNED_PIDS[@]}"; do
    pid="${OWNED_PIDS[$i]}"
    if [ -n "$pid" ] && kill -0 "$pid" 2>/dev/null; then
      kill -TERM "$pid" 2>/dev/null || true
    fi
  done
  waited=0
  while [ "$waited" -lt "$TERM_TIMEOUT" ]; do
    alive=0
    for pid in "${OWNED_PIDS[@]}"; do
      if [ -n "$pid" ] && kill -0 "$pid" 2>/dev/null; then
        alive=1
      fi
    done
    if [ "$alive" -eq 0 ]; then
      break
    fi
    sleep 1
    waited=$((waited + 1))
  done
  for pid in "${OWNED_PIDS[@]}"; do
    if [ -n "$pid" ] && kill -0 "$pid" 2>/dev/null; then
      kill -KILL "$pid" 2>/dev/null || true
    fi
  done
  for pid in "${OWNED_PIDS[@]}"; do
    if [ -n "$pid" ]; then
      wait "$pid" 2>/dev/null || true
    fi
  done
}

write_terminal_record() {  # $1=exit_code
  local code="$1" ok="true"
  if [ "$code" -ne 0 ]; then
    ok="false"
  fi
  if [ -n "$TERMINAL_OUTPUT" ]; then
    "$PYTHON_BIN" - "$TERMINAL_OUTPUT" "$code" "$ok" <<'PYEOF'
import json, os, sys, tempfile
path, code, ok = sys.argv[1], int(sys.argv[2]), sys.argv[3] == "true"
payload = {"exit_code": code, "ok": ok}
d = os.path.dirname(path) or "."
os.makedirs(d, exist_ok=True)
fd, tmp = tempfile.mkstemp(dir=d, prefix=".terminal.", suffix=".tmp")
with os.fdopen(fd, "w", encoding="utf-8") as f:
    json.dump(payload, f, sort_keys=True)
os.replace(tmp, path)
PYEOF
  fi
}

cleanup_network() {
  # Reset Serve and logout — best-effort, never fatal, never blocks teardown.
  if [ -S "$TS_SOCKET" ]; then
    "$TAILSCALE_BIN" --socket="$TS_SOCKET" serve reset >/dev/null 2>&1 || true
    "$TAILSCALE_BIN" --socket="$TS_SOCKET" logout >/dev/null 2>&1 || true
  fi
}

finalize() {
  local code="$1"
  if [ "$FINALIZED" -eq 1 ]; then
    exit "$code"
  fi
  FINALIZED=1
  set +e
  cleanup_network
  terminate_owned
  if [ -n "$JOB_ROOT" ] && [ -d "$JOB_ROOT" ]; then
    rm -rf -- "$JOB_ROOT"
  fi
  write_terminal_record "$code"
  exit "$code"
}

on_signal() {
  finalize 143
}
trap on_signal TERM INT

fail() {  # $1=message
  log "FATAL: $1"
  finalize 1
}

# ---------------------------------------------------------------------------
# Gate 1: validate mounted credentials before starting any child.
# ---------------------------------------------------------------------------
if [ -z "$HEADSCALE_KEY_FILE" ] || [ -z "$INBOUND_A2A_FILE" ] || [ -z "$OUTBOUND_A2A_FILE" ]; then
  fail "RED_SHIRT_HEADSCALE_KEY_FILE / RED_SHIRT_INBOUND_A2A_FILE / RED_SHIRT_OUTBOUND_A2A_FILE are required"
fi
if ! "$PYTHON_BIN" "$CONFIG_PY" validate-secrets \
      --headscale-key "$HEADSCALE_KEY_FILE" \
      --inbound-a2a "$INBOUND_A2A_FILE" \
      --outbound-a2a "$OUTBOUND_A2A_FILE" >/dev/null 2>&1; then
  fail "credential validation failed (see red_shirt_config.py validate-secrets for the exact reason)"
fi

# ---------------------------------------------------------------------------
# Gate 2: create the job-local root and required state dirs.
# ---------------------------------------------------------------------------
mkdir -p "$JOB_ROOT" "$TS_STATE_DIR"
chmod 700 "$JOB_ROOT" "$TS_STATE_DIR"

# ---------------------------------------------------------------------------
# Gate 3: start and bounded-readiness-check the selective CONNECT proxy.
# ---------------------------------------------------------------------------
"$PYTHON_BIN" "$CONNECT_PROXY_PY" --listen-port "$CONNECT_PROXY_PORT" --upstream "$ALCF_PROXY" &
CONNECT_PROXY_PID=$!
own_child "$CONNECT_PROXY_PID" "connect-proxy"
if ! _wait_for 20 "port_open $CONNECT_PROXY_PORT"; then
  fail "selective CONNECT proxy never bound on port $CONNECT_PROXY_PORT"
fi
log "connect-proxy ready on 127.0.0.1:$CONNECT_PROXY_PORT"

# ---------------------------------------------------------------------------
# Gate 4: start userspace tailscaled with job-local state/socket, configured
# to reach the Headscale/ALCF proxy chain through the CONNECT proxy above.
# ---------------------------------------------------------------------------
http_proxy="http://127.0.0.1:$CONNECT_PROXY_PORT"
SSL_CERT_FILE="$HEADSCALE_CA_FILE" \
HTTP_PROXY="$http_proxy" HTTPS_PROXY="$http_proxy" \
http_proxy="$http_proxy" https_proxy="$http_proxy" \
"$TAILSCALED_BIN" \
  --tun=userspace-networking \
  --state="$TS_STATE_DIR/state.json" \
  --socket="$TS_SOCKET" \
  --outbound-http-proxy-listen="127.0.0.1:$TS_OUTBOUND_HTTP_PORT" \
  >"$JOB_ROOT/tailscaled.log" 2>&1 &
TAILSCALED_PID=$!
own_child "$TAILSCALED_PID" "tailscaled"
if ! _wait_for 30 "[ -S '$TS_SOCKET' ]"; then
  fail "tailscaled socket never appeared at $TS_SOCKET"
fi
log "tailscaled ready (socket $TS_SOCKET)"

# ---------------------------------------------------------------------------
# Gate 5: join Headscale and wait for BackendState=Running.
# ---------------------------------------------------------------------------
if [ -z "$HEADSCALE_URL" ]; then
  fail "RED_SHIRT_HEADSCALE_URL is required"
fi
TS_UP_RC=0
if [ -z "$HEADSCALE_CA_FILE" ] || [ ! -r "$HEADSCALE_CA_FILE" ]; then
  fail "RED_SHIRT_HEADSCALE_CA_FILE must name a readable CA certificate"
fi
SSL_CERT_FILE="$HEADSCALE_CA_FILE" timeout "$TS_UP_TIMEOUT" "$TAILSCALE_BIN" --socket="$TS_SOCKET" up \
    --auth-key="file:$HEADSCALE_KEY_FILE" \
    --login-server="$HEADSCALE_URL" \
    --hostname="$TS_HOSTNAME" \
    >/dev/null 2>"$JOB_ROOT/tailscale-up.log" || TS_UP_RC=$?
if [ "$TS_UP_RC" -ne 0 ]; then
  fail "tailscale up failed (rc=$TS_UP_RC); see $JOB_ROOT/tailscale-up.log"
fi
if ! _wait_for "$TS_RUNNING_TIMEOUT" \
    "[ \"\$(\"$TAILSCALE_BIN\" --socket=\"$TS_SOCKET\" status --json 2>/dev/null | \"$PYTHON_BIN\" -c 'import json,sys; print(json.load(sys.stdin).get(\"BackendState\",\"\"))' 2>/dev/null)\" = Running ]"; then
  fail "tailscale BackendState never reached Running"
fi
log "tailscale BackendState=Running"

TAILNET_IP="$("$TAILSCALE_BIN" --socket="$TS_SOCKET" ip -4 2>/dev/null | head -n1)"
if [ -z "$TAILNET_IP" ]; then
  fail "could not resolve our own tailnet IPv4 address"
fi
A2A_PUBLIC_URL="http://$TAILNET_IP:$A2A_PORT/"

# ---------------------------------------------------------------------------
# Gate 6: configure Tailscale Serve tailnet TCP :A2A_PORT -> 127.0.0.1:A2A_PORT.
# ---------------------------------------------------------------------------
if ! "$TAILSCALE_BIN" --socket="$TS_SOCKET" serve --bg --tcp="$A2A_PORT" "tcp://127.0.0.1:$A2A_PORT" \
    >/dev/null 2>"$JOB_ROOT/tailscale-serve.log"; then
  fail "tailscale serve configuration failed; see $JOB_ROOT/tailscale-serve.log"
fi
log "tailscale serve tcp:$A2A_PORT -> 127.0.0.1:$A2A_PORT"

# ---------------------------------------------------------------------------
# Gate 7: render Hermes config (live-model selection), then run a direct
# inference smoke test BEFORE Hermes is ever started.
# ---------------------------------------------------------------------------
if [ -z "$PREFERRED_MODEL" ]; then
  fail "RED_SHIRT_PREFERRED_MODEL is required"
fi
declare -a FIXTURE_ARGS=()
# Optional, test-only: offline/deterministic catalog+jobs fixtures for the
# renderer (mirrors red_shirt_config.py's own --catalog-fixture/--jobs-fixture
# flags exactly; unset in production, which always queries the live ALCF
# catalog/jobs endpoints through the token helper).
if [ -n "${RED_SHIRT_CATALOG_FIXTURE:-}" ]; then
  FIXTURE_ARGS+=("--catalog-fixture" "$RED_SHIRT_CATALOG_FIXTURE")
fi
if [ -n "${RED_SHIRT_JOBS_FIXTURE:-}" ]; then
  FIXTURE_ARGS+=("--jobs-fixture" "$RED_SHIRT_JOBS_FIXTURE")
fi

RENDER_OUT="$JOB_ROOT/render.out"
if ! "$PYTHON_BIN" "$CONFIG_PY" render \
      --home "$RS_HOME" \
      --template "$TEMPLATE" \
      --token-helper "$TOKEN_HELPER" \
      --inbound-a2a "$INBOUND_A2A_FILE" \
      --outbound-a2a "$OUTBOUND_A2A_FILE" \
      --preferred-model "$PREFERRED_MODEL" \
      "${MODEL_PREFERENCE_ARGS[@]}" \
      "${FIXTURE_ARGS[@]}" \
      --cluster "$CLUSTER" \
      --a2a-port "$A2A_PORT" \
      --a2a-public-url "$A2A_PUBLIC_URL" \
      --wesley-url "$WESLEY_URL" \
      --wesley-proxy "http://127.0.0.1:$TS_OUTBOUND_HTTP_PORT" \
      --wesley-proxy-authority "$("$PYTHON_BIN" - "$WESLEY_URL" <<'PYEOF'
import sys
from urllib.parse import urlsplit
print(urlsplit(sys.argv[1]).netloc)
PYEOF
)" \
      >"$RENDER_OUT" 2>"$JOB_ROOT/render.err"; then
  fail "Hermes config render failed (no live/eligible model, or bad credentials); see $JOB_ROOT/render.err"
fi
SELECTED_MODEL="$(awk -F': ' '/^model: /{print $2; exit}' "$RENDER_OUT")"
if [ -z "$SELECTED_MODEL" ]; then
  fail "renderer did not report a selected model"
fi
log "rendered config; selected model=$SELECTED_MODEL"

INFER_TOKEN_FILE="$JOB_ROOT/inference.token"
if ! "$PYTHON_BIN" "$TOKEN_HELPER" get_access_token --service inference > "$INFER_TOKEN_FILE" 2>"$JOB_ROOT/token.err"; then
  rm -f "$INFER_TOKEN_FILE"
  fail "could not obtain an inference access token for the smoke test"
fi
chmod 600 "$INFER_TOKEN_FILE"
if ! "$PYTHON_BIN" "$PROBE_PY" inference \
      --base-url "$ALCF_BASE_URL" \
      --model "$SELECTED_MODEL" \
      --token-file "$INFER_TOKEN_FILE" \
      --proxy "$ALCF_PROXY_URL" >"$JOB_ROOT/inference-smoke.json" 2>&1; then
  rm -f "$INFER_TOKEN_FILE"
  fail "direct inference smoke test failed before Hermes was started; see $JOB_ROOT/inference-smoke.json"
fi
rm -f "$INFER_TOKEN_FILE"
log "inference smoke OK"

# ---------------------------------------------------------------------------
# Gate 8: seed SOUL/docs/skills using managed semantics (seed absent, refresh
# only an image-identical previously managed copy, preserve user edits).
# ---------------------------------------------------------------------------
STAMP_DIR="$RS_HOME/.red_shirt_seed_stamps"
mkdir -p "$STAMP_DIR"

managed_seed() {  # $1=src $2=dst $3=label
  local src="$1" dst="$2" label="$3" stamp
  [ -f "$src" ] || return 0
  stamp="$STAMP_DIR/$(printf '%s' "$dst" | "$PYTHON_BIN" -c 'import hashlib,sys; print(hashlib.sha256(sys.stdin.read().encode()).hexdigest())').sha"
  if [ ! -f "$dst" ]; then
    mkdir -p "$(dirname "$dst")"
    cp "$src" "$dst"
    "$PYTHON_BIN" -c "import hashlib,sys; print(hashlib.sha256(open(sys.argv[1],'rb').read()).hexdigest())" "$src" > "$stamp"
    log "seeded $label"
  elif [ -f "$stamp" ] && [ "$("$PYTHON_BIN" -c "import hashlib,sys; print(hashlib.sha256(open(sys.argv[1],'rb').read()).hexdigest())" "$dst")" = "$(cat "$stamp")" ]; then
    if ! cmp -s "$src" "$dst"; then
      cp "$src" "$dst"
      "$PYTHON_BIN" -c "import hashlib,sys; print(hashlib.sha256(open(sys.argv[1],'rb').read()).hexdigest())" "$src" > "$stamp"
      log "updated $label from image"
    fi
  else
    log "kept user-edited $label"
  fi
}

managed_seed_tree() {  # $1=source_dir $2=destination_dir $3=label_prefix
  local source_dir="$1" destination_dir="$2" label_prefix="$3" relative
  [ -d "$source_dir" ] || return 0
  while IFS= read -r -d '' relative; do
    relative="${relative#./}"
    managed_seed \
      "$source_dir/$relative" \
      "$destination_dir/$relative" \
      "$label_prefix/$relative"
  done < <(
    cd "$source_dir"
    "$PYTHON_BIN" - <<'PYEOF'
import os
import sys
for root, dirs, files in os.walk("."):
    dirs.sort()
    files.sort()
    for name in files:
        path = os.path.join(root, name)
        sys.stdout.buffer.write(path.encode("utf-8") + b"\0")
PYEOF
  )
}

managed_seed "$RS_DIR/config/SOUL.md" "$RS_HOME/SOUL.md" "SOUL.md"
managed_seed_tree "$RS_DIR/docs" "$RS_HOME/docs" "docs"
managed_seed_tree "$RS_DIR/skills" "$RS_HOME/skills" "skills"

# ---------------------------------------------------------------------------
# Gate 9: launch the Hermes A2A gateway as an owned foreground child.
# ---------------------------------------------------------------------------
HERMES_HOME="$RS_HOME" A2A_HOST=127.0.0.1 A2A_PORT="$A2A_PORT" \
  "$HERMES_BIN" gateway run --no-supervise --external-supervisor \
  >"$JOB_ROOT/hermes.log" 2>&1 &
HERMES_PID=$!
own_child "$HERMES_PID" "hermes-gateway"

# ---------------------------------------------------------------------------
# Gate 10: bounded-check the local Agent Card. The external tailnet-path
# check is intentionally performed by Wesley after READY: a userspace
# tailscaled node cannot reliably hairpin through Tailscale Serve to its own
# tailnet IP, so making that self-dial a startup gate produces a false failure.
# ---------------------------------------------------------------------------
if ! _wait_for "$CARD_TIMEOUT" \
    "\"$PYTHON_BIN\" \"$PROBE_PY\" card --url \"http://127.0.0.1:$A2A_PORT\" >/dev/null 2>&1"; then
  fail "local Agent Card never became ready on 127.0.0.1:$A2A_PORT"
fi
log "local Agent Card ready"
log "tailnet-path Agent Card awaiting external Wesley probe ($TAILNET_IP:$A2A_PORT)"

# ---------------------------------------------------------------------------
# Gate 11: emit READY — non-secret evidence only.
# ---------------------------------------------------------------------------
if [ -n "$READY_OUTPUT" ]; then
  "$PYTHON_BIN" - "$READY_OUTPUT" <<PYEOF
import json, os, tempfile
payload = {
    "ok": True,
    "model": "$SELECTED_MODEL",
    "revision": "$REVISION",
    "hostname": "$TS_HOSTNAME",
    "tailnet_ip": "$TAILNET_IP",
    "a2a_port": $A2A_PORT,
    "transport": "userspace-tailscale",
    "inference_smoke": "ok",
    "card_local": "ok",
    "card_tailnet": "external_pending",
}
path = "$READY_OUTPUT"
d = os.path.dirname(path) or "."
os.makedirs(d, exist_ok=True)
fd, tmp = tempfile.mkstemp(dir=d, prefix=".ready.", suffix=".tmp")
with os.fdopen(fd, "w", encoding="utf-8") as f:
    json.dump(payload, f, sort_keys=True)
os.replace(tmp, path)
PYEOF
fi
log "READY"

# ---------------------------------------------------------------------------
# Gate 12: foreground wait; detect any required child dying and tear down.
# ---------------------------------------------------------------------------
while true; do
  if ! kill -0 "$HERMES_PID" 2>/dev/null; then
    break
  fi
  if ! kill -0 "$CONNECT_PROXY_PID" 2>/dev/null; then
    log "required child connect-proxy died"
    finalize 1
  fi
  if ! kill -0 "$TAILSCALED_PID" 2>/dev/null; then
    log "required child tailscaled died"
    finalize 1
  fi
  sleep 0.5
done

set +e
wait "$HERMES_PID"
HERMES_CODE=$?
set -e
finalize "$HERMES_CODE"
