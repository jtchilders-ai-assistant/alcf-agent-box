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
