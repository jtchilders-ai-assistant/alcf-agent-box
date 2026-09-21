#!/usr/bin/env bash
set -euo pipefail
umask 077

BRIDGE_DIR="${RED_SHIRT_BRIDGE_DIR:?RED_SHIRT_BRIDGE_DIR is required}"
ATTEMPT_ROOT="${RED_SHIRT_ATTEMPT_ROOT:-$(dirname "$BRIDGE_DIR")}" 
REQUEST="$BRIDGE_DIR/request.json"
RESPONSE="$BRIDGE_DIR/response.json"
RESPONSE_TMP="$BRIDGE_DIR/response.json.tmp.$$"
LOCK="$BRIDGE_DIR/request.lock"

exec 9>"$LOCK"
if command -v flock >/dev/null 2>&1; then
  flock -n 9 || { printf 'bridge request already active\n' >&2; exit 75; }
fi

request_env="$BRIDGE_DIR/request.env.$$"
set +e
python3 - "$REQUEST" "$ATTEMPT_ROOT" >"$request_env" <<'PY'
import json
import pathlib
import shlex
import sys

request_path = pathlib.Path(sys.argv[1])
root = pathlib.Path(sys.argv[2]).resolve()
request = json.loads(request_path.read_text())
if not isinstance(request, dict):
    raise SystemExit("request must be a JSON object")
if request.get("version") != 1:
    raise SystemExit("unsupported bridge protocol version")
action = request.get("action")
allowed_fields = {
    "env_report": {"version", "action", "nonce", "output_dir"},
    "run_script": {"version", "action", "nonce", "script", "output_dir"},
    "run8": {"version", "action", "nonce", "executable", "output_dir"},
}
if action not in allowed_fields:
    raise SystemExit("unsupported bridge action")
unknown = set(request) - allowed_fields[action]
if unknown:
    raise SystemExit("unsupported request fields: %s" % ", ".join(sorted(unknown)))
if not isinstance(request.get("nonce"), str) or not request["nonce"]:
    raise SystemExit("missing bridge nonce")
for key in ("script", "executable", "output_dir"):
    value = request.get(key)
    if value is None:
        continue
    path = pathlib.Path(value).resolve()
    try:
        path.relative_to(root)
    except ValueError:
        raise SystemExit("%s escapes attempt root: %s" % (key, path))
for key, value in request.items():
    if isinstance(value, (str, int, float)):
        print("REQ_%s=%s" % (key.upper(), shlex.quote(str(value))))
PY
parse_rc=$?
set -e

if [ "$parse_rc" -eq 0 ]; then
  # shellcheck disable=SC1090
  source "$request_env"
else
  # Preserve correlation metadata even when validation rejects action paths.
  # Malformed JSON still falls back to explicit unknown values.
  correlation_env="$BRIDGE_DIR/correlation.env.$$"
  python3 - "$REQUEST" >"$correlation_env" <<'PY' || true
import json
import shlex
import sys
try:
    request = json.load(open(sys.argv[1]))
except Exception:
    request = {}
print("REQ_ACTION=%s" % shlex.quote(str(request.get("action", "unknown"))))
print("REQ_NONCE=%s" % shlex.quote(str(request.get("nonce", "unknown"))))
PY
  # shellcheck disable=SC1090
  source "$correlation_env"
  rm -f "$correlation_env"
fi
rm -f "$request_env"
: "${REQ_SCRIPT:=}"
: "${REQ_EXECUTABLE:=}"
: "${REQ_OUTPUT_DIR:=$BRIDGE_DIR}"

started_at="$(python3 -c 'import time; print(time.time())')"
profile_id="${RED_SHIRT_ENV_PROFILE_ID:-unconfigured}"
stdout_path="$REQ_OUTPUT_DIR/${REQ_ACTION}.stdout"
stderr_path="$REQ_OUTPUT_DIR/${REQ_ACTION}.stderr"
script_sha256=""
rc="$parse_rc"
message=""

if [ "$parse_rc" -eq 0 ]; then
  mkdir -p "$REQ_OUTPUT_DIR"
  case "$REQ_ACTION" in
    env_report)
      stdout_path="$REQ_OUTPUT_DIR/env_report.stdout"
      stderr_path="$REQ_OUTPUT_DIR/env_report.stderr"
      python3 - "$REQ_OUTPUT_DIR/environment.json" <<'PY' >"$stdout_path" 2>"$stderr_path"
import json
import os
import pathlib
import platform
import sys

output = pathlib.Path(sys.argv[1])
record = {
    "schema_version": 1,
    "profile_id": os.environ.get("RED_SHIRT_ENV_PROFILE_ID", "unconfigured"),
    "host": platform.node(),
    "python": platform.python_version(),
    "pbs_job_id": os.environ.get("PBS_JOBID"),
    "pbs_nodefile": os.environ.get("PBS_NODEFILE"),
}
output.write_text(json.dumps(record, indent=2, sort_keys=True) + "\n")
print(json.dumps(record, sort_keys=True))
PY
      rc=$?
      ;;
    run_script)
      stdout_path="$REQ_OUTPUT_DIR/run_script.stdout"
      stderr_path="$REQ_OUTPUT_DIR/run_script.stderr"
      if [ ! -f "$REQ_SCRIPT" ]; then
        printf 'requested script is not a regular file\n' >"$stderr_path"
        : >"$stdout_path"
        rc=66
      else
        script_sha256="$(python3 - "$REQ_SCRIPT" <<'PY'
import hashlib
import sys
with open(sys.argv[1], "rb") as stream:
    print(hashlib.sha256(stream.read()).hexdigest())
PY
)"
        set +e
        bash "$REQ_SCRIPT" >"$stdout_path" 2>"$stderr_path"
        rc=$?
        set -e
      fi
      ;;
    run8)
      stdout_path="$REQ_OUTPUT_DIR/run8.stdout"
      stderr_path="$REQ_OUTPUT_DIR/run8.stderr"
      if [ -z "$REQ_EXECUTABLE" ] || [ ! -x "$REQ_EXECUTABLE" ]; then
        printf 'requested executable is absent or not executable\n' >"$stderr_path"
        : >"$stdout_path"
        rc=66
      elif [ -z "${RED_SHIRT_RUN8_COMMAND:-}" ]; then
        printf 'RED_SHIRT_RUN8_COMMAND is not configured\n' >"$stderr_path"
        : >"$stdout_path"
        rc=78
      else
        set +e
        RED_SHIRT_EXECUTABLE="$REQ_EXECUTABLE" RED_SHIRT_OUTPUT_DIR="$REQ_OUTPUT_DIR" \
          bash -c "$RED_SHIRT_RUN8_COMMAND" >"$stdout_path" 2>"$stderr_path"
        rc=$?
        set -e
      fi
      ;;
  esac
else
  stdout_path="$BRIDGE_DIR/unknown.stdout"
  stderr_path="$BRIDGE_DIR/unknown.stderr"
  : >"$stdout_path"
  printf 'invalid bridge request\n' >"$stderr_path"
  message="invalid bridge request"
fi

finished_at="$(python3 -c 'import time; print(time.time())')"
python3 - "$REQUEST" "$RESPONSE_TMP" "$REQ_ACTION" "$REQ_NONCE" "$rc" \
  "$started_at" "$finished_at" "$profile_id" "$stdout_path" "$stderr_path" \
  "$script_sha256" "$message" <<'PY'
import json
import os
import pathlib
import sys

(_, request_path, response_path, action, nonce, exit_code, started_at,
 finished_at, profile_id, stdout_path, stderr_path, script_sha256, message) = sys.argv
payload = {
    "version": 1,
    "action": action,
    "nonce": nonce,
    "exit_code": int(exit_code),
    "started_at": float(started_at),
    "finished_at": float(finished_at),
    "environment_profile_id": profile_id,
    "stdout_path": stdout_path,
    "stderr_path": stderr_path,
    "script_sha256": script_sha256 or None,
    "bridge_error": bool(int(exit_code)) and (action == "unknown" or bool(message)),
}
if message:
    payload["message"] = message
pathlib.Path(response_path).write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n")
os.chmod(response_path, 0o600)
PY
mv "$RESPONSE_TMP" "$RESPONSE"
exit "$rc"
