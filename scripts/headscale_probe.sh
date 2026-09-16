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
#   WESLEY_URL             http://100.64.0.2/
#   TS_STATE_DIR           /tmp/ts-state
#   TS_SOCKET              /tmp/tailscaled.sock
#   TS_SOCKS5_PORT         1055
#   TS_OUTBOUND_HTTP_PORT  1056
#   CONNECT_PROXY_PORT     18443
#   ALCF_PROXY             proxy.alcf.anl.gov:3128
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
WESLEY_URL="${WESLEY_URL:-http://${WESLEY_IP}/}"

TS_STATE_DIR="${TS_STATE_DIR:-/tmp/ts-state}"
TS_SOCKET="${TS_SOCKET:-/tmp/tailscaled.sock}"
TS_SOCKS5_PORT="${TS_SOCKS5_PORT:-1055}"
TS_OUTBOUND_HTTP_PORT="${TS_OUTBOUND_HTTP_PORT:-1056}"

CONNECT_PROXY_PORT="${CONNECT_PROXY_PORT:-18443}"
ALCF_PROXY="${ALCF_PROXY:-proxy.alcf.anl.gov:3128}"

# ---------------------------------------------------------------------------
# Validate required files before doing anything
# ---------------------------------------------------------------------------
if [ ! -r "${AUTH_KEY_FILE}" ]; then
  echo '{"error":"AUTH_KEY_FILE not readable","file":"'"${AUTH_KEY_FILE}"'"}' >&2
  exit 1
fi
if [ ! -f "${CA_FILE}" ] || [ ! -r "${CA_FILE}" ]; then
  echo '{"error":"CA_FILE not readable","file":"'"${CA_FILE}"'"}' >&2
  exit 1
fi

# ---------------------------------------------------------------------------
# SSL_CERT_FILE so tailscaled trusts the mounted Caddy root CA
# ---------------------------------------------------------------------------
export SSL_CERT_FILE="${CA_FILE}"

# ---------------------------------------------------------------------------
# JSON result accumulator
# ---------------------------------------------------------------------------
RESULTS=()

_result() {
  local step="$1" ok="$2" detail="$3"
  RESULTS+=("{\"step\":\"${step}\",\"ok\":${ok},\"detail\":\"${detail}\"}")
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

# Wait for the proxy to bind
for _i in $(seq 1 20); do
  if 2>/dev/null bash -c "echo > /dev/tcp/127.0.0.1/${CONNECT_PROXY_PORT}"; then
    break
  fi
  sleep 0.2
done

# ---------------------------------------------------------------------------
# 2. Start tailscaled in userspace-networking mode.
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
# 3. Connect to Headscale via tailscale up
# ---------------------------------------------------------------------------
if [ "${SOCKET_READY}" = "true" ]; then
  if tailscale \
      --socket="${TS_SOCKET}" \
      up \
      --auth-key=file:"${AUTH_KEY_FILE}" \
      --login-server="${HEADSCALE_URL}" \
      --hostname="probe-$(hostname)" \
      2>&1; then
    _result "tailscale_up" true "connected"
  else
    _result "tailscale_up" false "tailscale up failed"
  fi
fi

# ---------------------------------------------------------------------------
# 4. Wait for tailscale status to show BackendState=Running
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
# 5. Ping WESLEY_IP over the Tailscale tunnel
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
# 6. curl WESLEY_URL through the SOCKS5 proxy
# ---------------------------------------------------------------------------
SOCKS5_PROXY="socks5://127.0.0.1:${TS_SOCKS5_PORT}"
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
# 7. Emit JSON summary
# ---------------------------------------------------------------------------
OVERALL="true"
for r in "${RESULTS[@]}"; do
  if echo "${r}" | grep -q '"ok":false'; then
    OVERALL="false"
    break
  fi
done

echo "{"
echo "  \"overall_ok\": ${OVERALL},"
echo "  \"results\": ["
LAST=$(( ${#RESULTS[@]} - 1 ))
for i in "${!RESULTS[@]}"; do
  if [ "${i}" -eq "${LAST}" ]; then
    echo "    ${RESULTS[$i]}"
  else
    echo "    ${RESULTS[$i]},"
  fi
done
echo "  ]"
echo "}"

if [ "${OVERALL}" = "false" ]; then
  exit 1
fi
