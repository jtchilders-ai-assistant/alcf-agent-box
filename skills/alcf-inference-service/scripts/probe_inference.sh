#!/usr/bin/env bash
# Probe the ALCF Inference Service end-to-end: token -> discovery -> hot model -> chat.
# Prereq: a venv with `openai globus_sdk requests` and inference_auth_token.py present,
# and you have already run `python inference_auth_token.py authenticate` at least once.
#
# Usage:  ./probe_inference.sh [/path/to/dir/with/inference_auth_token.py]
# Exits non-zero if any stage fails. Prints a compact PASS/FAIL summary.
set -uo pipefail

DIR="${1:-.}"
API="https://inference-api.alcf.anl.gov/resource_server"
cd "$DIR" || { echo "FAIL: cannot cd to $DIR"; exit 1; }

echo "== 1. token =="
TOKEN=$(python inference_auth_token.py get_access_token 2>/dev/null)
if [ -z "${TOKEN:-}" ]; then
  echo "FAIL: no access token. Run: python inference_auth_token.py authenticate"; exit 1
fi
HRS=$(python inference_auth_token.py get_time_until_token_expiration --units hours 2>/dev/null)
echo "PASS: token len=${#TOKEN}, expires in ${HRS}h"

echo "== 2. list-endpoints =="
code=$(curl -s -o /tmp/probe_le.json -w '%{http_code}' --max-time 40 \
  -H "Authorization: Bearer $TOKEN" "$API/list-endpoints")
first=$(head -c1 /tmp/probe_le.json)
if [ "$code" != "200" ] || [ "$first" = "<" ]; then
  echo "FAIL: list-endpoints HTTP $code (first char '$first' — '<' means wrong host / got HTML)"; exit 1
fi
echo "PASS: list-endpoints HTTP 200 (JSON)"

echo "== 3. pick a HOT sophia model =="
curl -s --max-time 40 -H "Authorization: Bearer $TOKEN" "$API/sophia/jobs" -o /tmp/probe_jobs.json
MODEL=$(python3 - <<'PY'
import json
try:
    d=json.load(open('/tmp/probe_jobs.json'))
    run=d.get('running',[])
    # 'Models' may be comma-joined; take the first running model id
    for j in run:
        m=(j.get('Models') or '').split(',')[0].strip()
        if m: print(m); break
except Exception:
    pass
PY
)
if [ -z "${MODEL:-}" ]; then
  echo "WARN: no hot model found; falling back to openai/gpt-oss-120b (may cold-load 10-15min)"
  MODEL="openai/gpt-oss-120b"
else
  echo "PASS: hot model = $MODEL"
fi

echo "== 4. chat completion =="
# max_tokens generous: gpt-oss reasoning models spend the budget on hidden reasoning first.
code=$(curl -s -o /tmp/probe_chat.json -w '%{http_code}' --max-time 150 -X POST \
  "$API/sophia/vllm/v1/chat/completions" \
  -H "Authorization: Bearer $TOKEN" -H "Content-Type: application/json" \
  -d "{\"model\":\"$MODEL\",\"max_tokens\":400,\"messages\":[{\"role\":\"user\",\"content\":\"Reply with exactly: ALCF inference probe OK\"}]}")
python3 - "$code" <<'PY'
import json,sys
code=sys.argv[1]
if code!='200':
    print(f"FAIL: chat HTTP {code}"); sys.exit(1)
d=json.load(open('/tmp/probe_chat.json'))
ch=d['choices'][0]; m=ch['message']
print("PASS: chat HTTP 200")
print("  finish_reason:", ch.get('finish_reason'))
print("  content:", repr(m.get('content')))
if m.get('content') is None and ch.get('finish_reason')=='length':
    print("  NOTE: content=null + finish=length -> raise max_tokens (reasoning model ate the budget)")
print("  usage:", d.get('usage'))
PY
