#!/usr/bin/env bash
# Minimal Red Shirt process-supervision entrypoint (Task 5 slice).
#
# Scope: own a small set of child PIDs (the Hermes child plus an optional
# required support child), wait in the foreground, forward TERM/INT to the
# owned children, escalate to KILL after a bounded timeout, remove only the
# job-local root on exit (never touching a persistent home), and emit a
# non-secret terminal JSON record. No networking, no readiness gates, no
# content seeding.
set -euo pipefail
umask 077

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
