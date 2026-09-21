#!/usr/bin/env bash
set -euo pipefail
umask 077

BRIDGE_DIR="${RED_SHIRT_BRIDGE_DIR:?RED_SHIRT_BRIDGE_DIR is required}"
ACTION="${1:?usage: red_shirt_host_client.sh <env_report|run_script|run8> [request-json]}"
if [ "$#" -ge 2 ]; then
  REQUEST_JSON="$2"
else
  REQUEST_JSON='{}'
fi
TIMEOUT_SECONDS="${RED_SHIRT_BRIDGE_TIMEOUT:-1800}"
REQUEST="$BRIDGE_DIR/request.json"
RESPONSE="$BRIDGE_DIR/response.json"
REQUEST_TMP="$BRIDGE_DIR/request.json.tmp.$$"
CLIENT_LOCK="$BRIDGE_DIR/client.lock"

if ! mkdir "$CLIENT_LOCK" 2>/dev/null; then
  printf 'bridge client request already active\n' >&2
  exit 75
fi
cleanup_client_lock() {
  rmdir "$CLIENT_LOCK" 2>/dev/null || true
}
trap cleanup_client_lock EXIT INT TERM

python3 - "$REQUEST_TMP" "$ACTION" "$REQUEST_JSON" <<'PY'
import json
import os
import pathlib
import sys
import time

path = pathlib.Path(sys.argv[1])
action = sys.argv[2]
extra = json.loads(sys.argv[3])
if not isinstance(extra, dict):
    raise SystemExit("request JSON must be an object")
reserved = {"version", "action", "nonce"}
if reserved.intersection(extra):
    raise SystemExit("request JSON may not override protocol fields")
request = {
    "version": 1,
    "action": action,
    "nonce": "%d-%d" % (int(time.time() * 1000000), os.getpid()),
}
request.update(extra)
path.write_text(json.dumps(request, indent=2, sort_keys=True) + "\n")
PY
chmod 600 "$REQUEST_TMP"
rm -f "$RESPONSE"
mv "$REQUEST_TMP" "$REQUEST"
nonce="$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1]))["nonce"])' "$REQUEST")"

waited=0
while [ "$waited" -lt "$TIMEOUT_SECONDS" ]; do
  if [ -s "$RESPONSE" ]; then
    set +e
    python3 - "$RESPONSE" "$nonce" <<'PY'
import json
import sys
response = json.load(open(sys.argv[1]))
if response.get("nonce") != sys.argv[2]:
    raise SystemExit("stale bridge response")
raise SystemExit(int(response["exit_code"]))
PY
    rc=$?
    set -e
    exit "$rc"
  fi
  sleep 1
  waited=$((waited + 1))
done
printf 'host bridge response timeout after %s seconds\n' "$TIMEOUT_SECONDS" >&2
exit 124
