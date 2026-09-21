#!/usr/bin/env bash
set -euo pipefail
umask 077

BRIDGE_DIR="${RED_SHIRT_BRIDGE_DIR:?RED_SHIRT_BRIDGE_DIR is required}"
HOST_BRIDGE="${RED_SHIRT_HOST_BRIDGE:?RED_SHIRT_HOST_BRIDGE is required}"
REQUEST="$BRIDGE_DIR/request.json"
RESPONSE="$BRIDGE_DIR/response.json"
mkdir -p "$BRIDGE_DIR"
touch "$BRIDGE_DIR/READY"
last=""
worker_pid=""

shutdown() {
  local rc="$1"
  trap - EXIT INT TERM
  if [ -n "$worker_pid" ] && kill -0 "$worker_pid" 2>/dev/null; then
    kill -TERM "$worker_pid" 2>/dev/null || true
    wait "$worker_pid" 2>/dev/null || true
  fi
  exit "$rc"
}
trap 'shutdown 143' TERM
trap 'shutdown 130' INT

write_error_response() {
  local request="$1" code="$2" message="$3" tmp="$RESPONSE.tmp.$$"
  python3 - "$request" "$tmp" "$code" "$message" <<'PY'
import json
import os
import pathlib
import sys
import time

request_path, output_path, code, message = sys.argv[1:5]
try:
    request = json.load(open(request_path))
except Exception:
    request = {}
payload = {
    "version": 1,
    "action": request.get("action", "unknown"),
    "nonce": request.get("nonce", "unknown"),
    "exit_code": int(code),
    "finished_at": time.time(),
    "bridge_error": True,
    "message": message,
}
pathlib.Path(output_path).write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n")
os.chmod(output_path, 0o600)
PY
  mv "$tmp" "$RESPONSE"
}

while :; do
  if [ -e "$BRIDGE_DIR/STOP" ]; then
    exit 0
  fi
  if [ -s "$REQUEST" ]; then
    nonce="$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1])).get("nonce", ""))' "$REQUEST" 2>/dev/null || true)"
    if [ -n "$nonce" ] && [ "$nonce" != "$last" ]; then
      last="$nonce"
      set +e
      bash "$HOST_BRIDGE" &
      worker_pid=$!
      wait "$worker_pid"
      rc=$?
      worker_pid=""
      set -e
      if [ ! -s "$RESPONSE" ]; then
        write_error_response "$REQUEST" "$rc" "bridge worker exited without a response"
      fi
      printf '%s %s\n' "$nonce" "$rc" >"$BRIDGE_DIR/watcher.log.tmp.$$"
      mv "$BRIDGE_DIR/watcher.log.tmp.$$" "$BRIDGE_DIR/watcher.log"
    fi
  fi
  sleep 0.05
done
