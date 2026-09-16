#!/usr/bin/env bash
# scripts/headscale_probe.sh — Headscale / Tailscale connectivity probe.
#
# Runs inside the ghcr.io/.../alcf-agent-headscale-probe container.
# Uses userspace networking (no TUN), chains through the local connect_proxy
# rewrite proxy, which in turn chains to proxy.alcf.anl.gov:3128.
#
# Required files (mounted at container start):
#   AUTH_KEY_FILE   Path to a file containing the Headscale auth key.
#                   Default: /run/secrets/headscale-auth-key
#   CA_FILE         Path to the Caddy root CA cert (PEM).
#                   Default: /run/secrets/caddy-root.crt
#
# Configurable env vars (with defaults):
#   AUTH_KEY_FILE          (see above)
#   CA_FILE                (see above)
#   HEADSCALE_URL          https://143.198.112.69.sslip.io
#   WESLEY_IP              100.64.0.2
#   WESLEY_URL             http://100.64.0.2:8642/health
#   TS_STATE_DIR           /tmp/ts-state
#   TS_SOCKET              /tmp/tailscaled.sock
#   TS_SOCKS5_PORT         1055
#   TS_OUTBOUND_HTTP_PORT  1056
#   CONNECT_PROXY_PORT     18443
#   ALCF_PROXY             proxy.alcf.anl.gov:3128
#   TS_UP_TIMEOUT          60   # seconds for tailscale up timeout
#
# Usage:
#   docker run --rm \
#     -v /path/to/auth.key:/run/secrets/headscale-auth-key:ro \
#     -v /path/to/caddy-root.crt:/run/secrets/caddy-root.crt:ro \
#     ghcr.io/jtchilders-ai-assistant/alcf-agent-headscale-probe
#
# Exit codes:
#   0 — all probe steps passed
#   1 — one or more steps failed (see JSON summary in stdout)

set -euo pipefail

# ---------------------------------------------------------------------------
# Defaults
# ---------------------------------------------------------------------------
AUTH_KEY_FILE="${AUTH_KEY_FILE:-/run/secrets/headscale-auth-key}"
CA_FILE="${CA_FILE:-/run/secrets/caddy-root.crt}"

HEADSCALE_URL="${HEADSCALE_URL:-https://143.198.112.69.sslip.io}"
HEADSCALE_IP="143.198.112.69"

WESLEY_IP="${WESLEY_IP:-100.64.0.2}"
WESLEY_URL="${WESLEY_URL:-http://100.64.0.2:8642/health}"

TS_STATE_DIR="${TS_STATE_DIR:-/tmp/ts-state}"
TS_SOCKET="${TS_SOCKET:-/tmp/tailscaled.sock}"
TS_SOCKS5_PORT="${TS_SOCKS5_PORT:-1055}"
TS_OUTBOUND_HTTP_PORT="${TS_OUTBOUND_HTTP_PORT:-1056}"

CONNECT_PROXY_PORT="${CONNECT_PROXY_PORT:-18443}"
ALCF_PROXY="${ALCF_PROXY:-proxy.alcf.anl.gov:3128}"

TS_UP_TIMEOUT="${TS_UP_TIMEOUT:-60}"

# ---------------------------------------------------------------------------
# Validate required files before doing anything
# ---------------------------------------------------------------------------
if [ ! -r "${AUTH_KEY_FILE}" ]; then
  python3 -c "import json,sys; print(json.dumps({'error':'AUTH_KEY_FILE not readable','file':sys.argv[1]}))" "${AUTH_KEY_FILE}" >&2
  exit 1
fi
if [ ! -f "${CA_FILE}" ] || [ ! -r "${CA_FILE}" ]; then
  python3 -c "import json,sys; print(json.dumps({'error':'CA_FILE not readable','file':sys.argv[1]}))" "${CA_FILE}" >&2
  exit 1
fi

# ---------------------------------------------------------------------------
# SSL_CERT_FILE so tailscaled trusts the mounted Caddy root CA
# ---------------------------------------------------------------------------
export SSL_CERT_FILE="${CA_FILE}"

# ---------------------------------------------------------------------------
# JSON result accumulator (populated via _result; emitted by Python at end)
# ---------------------------------------------------------------------------
RESULT_STEPS=()
RESULT_OKS=()
RESULT_DETAILS=()

_result() {
  local step="$1" ok="$2" detail="$3"
  RESULT_STEPS+=("${step}")
  RESULT_OKS+=("${ok}")
  RESULT_DETAILS+=("${detail}")
}

# ---------------------------------------------------------------------------
# Cleanup trap — stop tailscaled and connect_proxy on any exit
# ---------------------------------------------------------------------------
CONNECT_PROXY_PID=""
TAILSCALED_PID=""

cleanup() {
  set +e
  if [ -n "${TAILSCALED_PID}" ] && kill -0 "${TAILSCALED_PID}" 2>/dev/null; then
    tailscale --socket="${TS_SOCKET}" logout 2>/dev/null || true
    kill "${TAILSCALED_PID}" 2>/dev/null
    wait "${TAILSCALED_PID}" 2>/dev/null
  fi
  if [ -n "${CONNECT_PROXY_PID}" ] && kill -0 "${CONNECT_PROXY_PID}" 2>/dev/null; then
    kill "${CONNECT_PROXY_PID}" 2>/dev/null
    wait "${CONNECT_PROXY_PID}" 2>/dev/null
  fi
}
trap cleanup EXIT SIGTERM INT

# ---------------------------------------------------------------------------
# 1. Start the local CONNECT rewrite proxy
#    Its upstream is the ALCF proxy; tailscaled's http_proxy points here.
# ---------------------------------------------------------------------------
python3 /usr/local/bin/connect_proxy.py \
  --listen-port "${CONNECT_PROXY_PORT}" \
  --upstream "${ALCF_PROXY}" \
  &
CONNECT_PROXY_PID=$!

# Wait for the proxy to bind using Python socket.connect_ex (portable).
PROXY_READY=false
for _i in $(seq 1 20); do
  if python3 -c "
import socket, sys
s = socket.socket()
s.settimeout(0.5)
rc = s.connect_ex(('127.0.0.1', int("${CONNECT_PROXY_PORT}")))
s.close()
sys.exit(0 if rc == 0 else 1)
" 2>/dev/null; then
    PROXY_READY=true
    break
  fi
  sleep 0.2
done

if [ "${PROXY_READY}" = "false" ]; then
  _result "connect_proxy_start" false "proxy never bound on port ${CONNECT_PROXY_PORT}"
  # Emit JSON and exit — no point continuing without the proxy
  python3 - <<'PYEOF'
import json, os, sys

steps   = os.environ.get("_RESULT_STEPS_JSON", "")
oks     = os.environ.get("_RESULT_OKS_JSON", "")
details = os.environ.get("_RESULT_DETAILS_JSON", "")
import ast
result = [
    {"step": "connect_proxy_start", "ok": False,
     "detail": "proxy never bound on port " + os.environ.get("CONNECT_PROXY_PORT", "18443")}
]
print(json.dumps({"overall_ok": False, "results": result}, indent=2))
PYEOF
  exit 1
fi

# ---------------------------------------------------------------------------
# 2. Explicit Headscale TLS validation via connect_proxy (pre-join check).
#    Uses --cacert CA_FILE; never -k.  Routes through the local rewrite proxy
#    so the sslip.io → numeric-IP rewrite is exercised before tailscale joins.
# ---------------------------------------------------------------------------
HS_HTTP_CODE=""
HS_CURL_RC=0
HS_HTTP_CODE=$(curl \
  --proxy "http://127.0.0.1:${CONNECT_PROXY_PORT}" \
  --cacert "${CA_FILE}" \
  --silent \
  --max-time 15 \
  --write-out "%{http_code}" \
  --output /dev/null \
  "${HEADSCALE_URL}/health" 2>/dev/null) || HS_CURL_RC=$?

if [ "${HS_CURL_RC}" -eq 0 ] && [ "${HS_HTTP_CODE}" = "200" ]; then
  _result "headscale_tls_check" true "HTTP 200 from ${HEADSCALE_URL}/health"
else
  _result "headscale_tls_check" false "rc=${HS_CURL_RC} http=${HS_HTTP_CODE}"
fi

# ---------------------------------------------------------------------------
# 3. Start tailscaled in userspace-networking mode.
#    Its http_proxy / https_proxy points to the local rewrite proxy so that
#    the Headscale connection goes through connect_proxy → ALCF proxy.
# ---------------------------------------------------------------------------
mkdir -p "${TS_STATE_DIR}"

http_proxy="http://127.0.0.1:${CONNECT_PROXY_PORT}"
HTTP_PROXY="${http_proxy}"
https_proxy="${http_proxy}"
HTTPS_PROXY="${http_proxy}"
export http_proxy HTTP_PROXY https_proxy HTTPS_PROXY

tailscaled \
  --tun=userspace-networking \
  --state="${TS_STATE_DIR}/state.json" \
  --socket="${TS_SOCKET}" \
  --socks5-server="127.0.0.1:${TS_SOCKS5_PORT}" \
  --outbound-http-proxy-listen="127.0.0.1:${TS_OUTBOUND_HTTP_PORT}" \
  &
TAILSCALED_PID=$!

# Wait for tailscaled socket
SOCKET_READY=false
for _i in $(seq 1 30); do
  if [ -S "${TS_SOCKET}" ]; then
    SOCKET_READY=true
    break
  fi
  sleep 1
done
if [ "${SOCKET_READY}" = "false" ]; then
  _result "tailscaled_start" false "socket ${TS_SOCKET} never appeared"
  # fall through to emit JSON and exit 1
else
  _result "tailscaled_start" true "socket ready"
fi

# ---------------------------------------------------------------------------
# 4. Connect to Headscale via tailscale up (with output suppressed to avoid
#    leaking registration URLs or auth tokens, and with an explicit timeout).
#    Gated on: socket ready AND TLS preflight returned HTTP 200.
# ---------------------------------------------------------------------------
if [ "${SOCKET_READY}" = "true" ] && [ "${HS_CURL_RC}" -eq 0 ] && [ "${HS_HTTP_CODE}" = "200" ]; then
  TS_UP_RC=0
  timeout "${TS_UP_TIMEOUT}" tailscale \
      --socket="${TS_SOCKET}" \
      up \
      --auth-key=file:"${AUTH_KEY_FILE}" \
      --login-server="${HEADSCALE_URL}" \
      --hostname="probe-$(hostname)" \
      >/dev/null 2>/dev/null || TS_UP_RC=$?
  if [ "${TS_UP_RC}" -eq 0 ]; then
    _result "tailscale_up" true "connected"
  elif [ "${TS_UP_RC}" -eq 124 ]; then
    _result "tailscale_up" false "tailscale up timed out after ${TS_UP_TIMEOUT}s"
  else
    _result "tailscale_up" false "tailscale up exited rc=${TS_UP_RC}"
  fi
fi

# ---------------------------------------------------------------------------
# 5. Wait for tailscale status to show BackendState=Running
# ---------------------------------------------------------------------------
TS_RUNNING=false
for _i in $(seq 1 30); do
  STATUS_JSON=$(tailscale --socket="${TS_SOCKET}" status --json 2>/dev/null || echo "{}")
  BACKEND=$(echo "${STATUS_JSON}" | python3 -c "import json,sys; d=json.load(sys.stdin); print(d.get('BackendState',''))" 2>/dev/null || true)
  if [ "${BACKEND}" = "Running" ]; then
    TS_RUNNING=true
    break
  fi
  sleep 2
done
if [ "${TS_RUNNING}" = "true" ]; then
  _result "tailscale_running" true "BackendState=Running"
else
  _result "tailscale_running" false "BackendState never reached Running"
fi

# ---------------------------------------------------------------------------
# 6. Ping WESLEY_IP over the Tailscale tunnel
# ---------------------------------------------------------------------------
if [ "${TS_RUNNING}" = "true" ]; then
  if tailscale --socket="${TS_SOCKET}" ping --c=3 "${WESLEY_IP}" >/dev/null 2>&1; then
    _result "ping_wesley" true "ping ${WESLEY_IP} OK"
  else
    _result "ping_wesley" false "ping ${WESLEY_IP} failed"
  fi
else
  _result "ping_wesley" false "skipped (tailscale not running)"
fi

# ---------------------------------------------------------------------------
# 7. curl WESLEY_URL through the SOCKS5 proxy
# ---------------------------------------------------------------------------
CURL_OUT=""
CURL_RC=0
if [ "${TS_RUNNING}" = "true" ]; then
  CURL_OUT=$(curl \
    --socks5 "127.0.0.1:${TS_SOCKS5_PORT}" \
    --silent \
    --max-time 10 \
    --cacert "${CA_FILE}" \
    --write-out "%{http_code}" \
    --output /dev/null \
    "${WESLEY_URL}" 2>/dev/null) || CURL_RC=$?
  if [ "${CURL_RC}" -eq 0 ] && [ "${CURL_OUT}" != "000" ]; then
    _result "curl_wesley" true "HTTP ${CURL_OUT}"
  else
    _result "curl_wesley" false "rc=${CURL_RC} http=${CURL_OUT}"
  fi
else
  _result "curl_wesley" false "skipped (tailscale not running)"
fi

# ---------------------------------------------------------------------------
# 8. Emit JSON summary — built with Python to safely handle special chars
#    in paths (quotes, backslashes, etc.)
# ---------------------------------------------------------------------------
_STEPS_JSON=$(python3 -c "import json,sys; print(json.dumps(sys.argv[1:]))" "${RESULT_STEPS[@]+"${RESULT_STEPS[@]}"}")
_OKS_JSON=$(python3 -c "import json,sys; print(json.dumps(sys.argv[1:]))" "${RESULT_OKS[@]+"${RESULT_OKS[@]}"}")
_DETAILS_JSON=$(python3 -c "import json,sys; print(json.dumps(sys.argv[1:]))" "${RESULT_DETAILS[@]+"${RESULT_DETAILS[@]}"}")
export _STEPS_JSON _OKS_JSON _DETAILS_JSON

python3 - <<'PYEOF'
import json, os, sys

steps   = json.loads(os.environ["_STEPS_JSON"])
oks_raw = json.loads(os.environ["_OKS_JSON"])
details = json.loads(os.environ["_DETAILS_JSON"])

results = [
    {"step": s, "ok": (o == "true"), "detail": d}
    for s, o, d in zip(steps, oks_raw, details)
]
overall_ok = all(r["ok"] for r in results) if results else False
print(json.dumps({"overall_ok": overall_ok, "results": results}, indent=2))
# Exit non-zero if any step failed so the container has a meaningful exit code.
sys.exit(0 if overall_ok else 1)
PYEOF
