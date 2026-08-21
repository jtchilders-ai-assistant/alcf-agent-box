#!/usr/bin/env python3
"""ALCF notify helper — push a notification to the user's phone/desktop via ntfy.

The TUI and web dashboard are "pull" surfaces: a cron job or a long-running
background task has no way to interrupt them (Hermes cron delivers only to
gateway chat platforms or to files). ntfy (https://ntfy.sh) fills that gap with
zero account setup: the user subscribes to a secret topic in the ntfy mobile or
web app, and anything POSTed to that topic arrives as an instant push.

Setup (user, once):
    1. Pick a hard-to-guess topic name (it is effectively a password), e.g.
       `alcf-<username>-x7k2m9`.
    2. Subscribe to it in the ntfy app (or https://ntfy.sh/<topic>).
    3. Start the container with `-e ALCF_NTFY_TOPIC=<topic>`
       (optionally `-e ALCF_NTFY_SERVER=https://your-ntfy.example` to self-host).

Usage:
    PY=/opt/hermes/.venv/bin/python
    $PY /opt/alcf/alcf_notify.py check
    $PY /opt/alcf/alcf_notify.py send "Polaris job 12345 is RUNNING"
    $PY /opt/alcf/alcf_notify.py send --title "Pepper build" --priority high \
        --tags white_check_mark "Build finished OK on x3108c0s1b0n0"

Notes:
    * The topic is a shared secret on a public relay (unless self-hosted).
      Keep messages to status-level facts — job ids, states, exit codes.
      NEVER include tokens, paths the user considers private, or file contents.
    * Exit codes: 0 sent, 3 not configured (no topic), 4 send failed. A cron
      poll script can therefore `alcf_notify.py send ... || true` safely.
"""
from __future__ import annotations

import argparse
import os
import sys


DEFAULT_SERVER = "https://ntfy.sh"

# ntfy priorities: https://docs.ntfy.sh/publish/#message-priority
PRIORITIES = ("min", "low", "default", "high", "urgent")


def build_request(message: str, topic: str | None = None, server: str | None = None,
                  title: str = "", priority: str = "", tags: str = "") -> tuple[str, bytes, dict]:
    """Return (url, body, headers) for the ntfy publish call.

    Pure function so it is unit-testable offline. Raises ValueError when the
    topic is missing/blank or the priority is invalid.
    """
    topic = (topic if topic is not None else os.environ.get("ALCF_NTFY_TOPIC", "")).strip()
    if not topic:
        raise ValueError(
            "no ntfy topic configured — start the container with "
            "-e ALCF_NTFY_TOPIC=<secret-topic> (see alcf-background-tasks skill)")
    server = (server if server is not None
              else os.environ.get("ALCF_NTFY_SERVER", DEFAULT_SERVER)).strip().rstrip("/")
    if priority and priority not in PRIORITIES:
        raise ValueError(f"invalid priority {priority!r}; choose from {PRIORITIES}")
    headers = {}
    # ntfy reads metadata from headers; non-ASCII must not crash the send, so
    # anything non-latin-1 falls back into the body instead of a Title header.
    if title:
        try:
            title.encode("latin-1")
            headers["Title"] = title
        except UnicodeEncodeError:
            message = f"{title}\n{message}"
    if priority:
        headers["Priority"] = priority
    if tags:
        headers["Tags"] = tags
    return f"{server}/{topic}", message.encode("utf-8"), headers


def cmd_check(args) -> int:
    topic = os.environ.get("ALCF_NTFY_TOPIC", "").strip()
    server = os.environ.get("ALCF_NTFY_SERVER", DEFAULT_SERVER).strip()
    if not topic:
        print("ntfy notifications: NOT CONFIGURED (set ALCF_NTFY_TOPIC at "
              "docker run to enable push notifications)")
        return 3
    print(f"ntfy notifications: configured (server={server}, "
          f"topic={topic[:4]}…{topic[-2:] if len(topic) > 6 else ''})")
    return 0


def cmd_send(args) -> int:
    try:
        url, body, headers = build_request(args.message, title=args.title,
                                           priority=args.priority, tags=args.tags)
    except ValueError as exc:
        print(f"[notify] {exc}", file=sys.stderr)
        return 3
    import requests
    try:
        r = requests.post(url, data=body, headers=headers, timeout=15)
        r.raise_for_status()
    except Exception as exc:
        print(f"[notify] send FAILED: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 4
    print("[notify] sent")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(
        description="Push a notification to the user via ntfy (ALCF_NTFY_TOPIC).")
    sub = ap.add_subparsers(dest="cmd_name", required=True)

    c = sub.add_parser("check", help="report whether ntfy is configured (no send)")
    c.set_defaults(func=cmd_check)

    s = sub.add_parser("send", help="send one notification")
    s.add_argument("message", help="notification body (status-level facts only; "
                                   "never tokens or private content)")
    s.add_argument("--title", default="", help="notification title")
    s.add_argument("--priority", default="", choices=("",) + PRIORITIES,
                   help="ntfy priority (default: server default)")
    s.add_argument("--tags", default="",
                   help="comma-separated ntfy tags/emoji (e.g. white_check_mark)")
    s.set_defaults(func=cmd_send)

    args = ap.parse_args()
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
