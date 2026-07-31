#!/usr/bin/env python3
"""ALCF facility helper for the agent — read-only "state of MY work" commands.

One command with four subcommands, so the agent has a single reliable tool
instead of hand-rolling API calls:

    status        Live system up/down + recent maintenance/outage events (NO auth)
    jobs          List YOUR jobs on a cluster (auth) — queued/running/finished
    output        Fetch a job's stdout/stderr from Home/Eagle (auth)
    allocations   Your projects + node-hours allocated vs used (auth)

Run with the bundled python:
    /opt/hermes/.venv/bin/python /opt/alcf/alcf_facility.py status
    /opt/hermes/.venv/bin/python /opt/alcf/alcf_facility.py jobs --cluster polaris
    /opt/hermes/.venv/bin/python /opt/alcf/alcf_facility.py allocations
    /opt/hermes/.venv/bin/python /opt/alcf/alcf_facility.py output \
        --path /home/<user>/iri_hello.out --lines 40

Everything is READ-ONLY. `status` needs no token (works before login); the
others use the IRI Globus token via the bundled auth script. All requests go
through iri_api_client, which sets the User-Agent Cloudflare requires (avoids
the 403 "error code: 1010" bot-block).
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys

# Make the bundled client importable regardless of cwd (same pattern as
# iri_hello_world.py): the skill scripts land on the data volume at runtime, or
# in /opt/alcf for a bare image. _REPO_LOCAL supports running from a repo
# checkout (scripts/ and skills/ are siblings).
_HERE = os.path.dirname(os.path.abspath(__file__))
_SKILL_SCRIPTS = "/opt/data/skills/research/alcf-iri-facility-api/scripts"
_ALT_SCRIPTS = "/opt/alcf/skills/alcf-iri-facility-api/scripts"
_REPO_LOCAL = os.path.join(os.path.dirname(_HERE),
                           "skills", "alcf-iri-facility-api", "scripts")
_SIBLING_LOCAL = os.path.join(_HERE, "skills", "alcf-iri-facility-api", "scripts")
for p in (_SKILL_SCRIPTS, _ALT_SCRIPTS, _REPO_LOCAL, _SIBLING_LOCAL):
    if os.path.isdir(p) and p not in sys.path:
        sys.path.insert(0, p)

try:
    from iri_api_client import IRI  # type: ignore
except Exception as exc:  # pragma: no cover
    print(f"ERROR: could not import iri_api_client: {exc}", file=sys.stderr)
    sys.exit(2)

AUTH_SCRIPT = "/opt/alcf/alcf_facility_api_globus_token.py"
PYTHON = "/opt/hermes/.venv/bin/python"

CLUSTERS = ("polaris", "crux", "aurora", "sophia")


def _authed_client() -> IRI:
    """Build an IRI client with a token, or exit with a clear message if the
    IRI login is missing (the agent cannot complete the browser login itself)."""
    try:
        token = subprocess.check_output(
            [PYTHON, AUTH_SCRIPT, "get_access_token"], text=True,
            stderr=subprocess.DEVNULL,
        ).strip()
    except Exception:
        token = ""
    if not token:
        print(
            "ERROR: no IRI Facility API token. This command needs the IRI login "
            "(separate from the inference login). Ask the user to run once on "
            "the host:\n"
            "    docker exec -it <container> \\\n"
            "      /opt/hermes/.venv/bin/python "
            "/opt/alcf/alcf_facility_api_globus_token.py authenticate\n"
            "then retry.",
            file=sys.stderr,
        )
        sys.exit(3)
    return IRI(token=token)


# ---------------------------------------------------------------------------
# status  (no auth)
# ---------------------------------------------------------------------------
def cmd_status(args) -> int:
    api = IRI()
    resources = api.resources()
    print("ALCF system status (live):")
    for r in sorted(resources, key=lambda x: (x.get("resource_type", ""), x.get("name", ""))):
        st = r.get("current_status", "?")
        mark = {"up": "UP  ", "down": "DOWN", "unknown": "??  "}.get(st, "??  ")
        print(f"  [{mark}] {r.get('name',''):18s} {r.get('resource_type','')}")

    # Recent maintenance/outage context from events (human-readable descriptions).
    try:
        events = api.events()
    except Exception:
        events = []
    # newest first
    events = sorted(events, key=lambda e: e.get("occurred_at", "") or e.get("last_modified", ""),
                    reverse=True)
    show = events[: args.events]
    if show:
        print(f"\nRecent status events (most recent {len(show)}):")
        for e in show:
            when = (e.get("occurred_at") or e.get("last_modified") or "")[:19]
            print(f"  {when}  {e.get('description','') or e.get('name','')}")
    if args.json:
        print("\n" + json.dumps({"resources": resources, "events": show}, indent=2))
    return 0


# ---------------------------------------------------------------------------
# jobs  (auth)
# ---------------------------------------------------------------------------
def cmd_jobs(args) -> int:
    api = _authed_client()
    from iri_api_client import RESOURCES  # type: ignore
    rid = RESOURCES.get(args.cluster)
    if not rid:
        print(f"ERROR: unknown cluster '{args.cluster}' (try: {', '.join(CLUSTERS)})",
              file=sys.stderr)
        return 2
    resp = api.job_statuses(rid, historical=args.historical, limit=args.limit)
    if args.json:
        print(json.dumps(resp, indent=2))
        return 0
    jobs = resp if isinstance(resp, list) else resp.get("jobs") or resp.get("data") or []
    if not jobs:
        print(f"No jobs found on {args.cluster} "
              f"({'incl. finished' if args.historical else 'active only'}).")
        # surface an API error if that's why it's empty
        if isinstance(resp, dict) and resp.get("error"):
            print(f"  (API said: {str(resp['error'])[:200]})", file=sys.stderr)
        return 0
    # Real shape (verified 2026-07-31): each job = {"id": "<pbsid>.polaris-...",
    # "status": {"state": "...", "exit_code": N}}. Fall back gracefully if the
    # API adds richer fields later.
    print(f"Your jobs on {args.cluster}:")
    for j in jobs:
        jid = j.get("id") or j.get("job_id") or "?"
        st = j.get("status") if isinstance(j.get("status"), dict) else {}
        state = st.get("state") or j.get("state") or j.get("job_state") or "?"
        exit_code = st.get("exit_code")
        exit_s = "" if exit_code in (None, "") else f"exit={exit_code}"
        short = jid.split(".")[0]  # bare PBS id for readability
        print(f"  {short:12s} {state:10s} {exit_s:8s} {jid}")
    return 0


# ---------------------------------------------------------------------------
# output  (auth) — read a job's stdout/stderr from Home/Eagle
# ---------------------------------------------------------------------------
def cmd_output(args) -> int:
    api = _authed_client()
    store = IRI.EAGLE if args.storage == "eagle" else IRI.HOME
    if args.lines:
        # head N lines (there is no tail at ALCF; view-from-offset is the
        # workaround for the end of a big file — see SKILL.md).
        res = api.head(store, args.path, lines=args.lines)
    else:
        res = api.view(store, args.path, size=args.size, offset=args.offset)
    if args.json:
        print(json.dumps(res, indent=2))
        return 0
    if isinstance(res, dict) and res.get("status") in ("failed", "error"):
        print(f"Read failed: {json.dumps(res)[:400]}", file=sys.stderr)
        return 1
    # Real shape (verified 2026-07-31): the task result nests the text at
    #   result.output.content   (content_type 'lines' for head, 'bytes' for view)
    # with start/end position metadata. ls uses result.output = [entries].
    payload = res.get("result") if isinstance(res, dict) else res
    content = None
    if isinstance(payload, dict):
        out = payload.get("output")
        if isinstance(out, dict):
            content = out.get("content")
        else:
            content = out
    if content is not None:
        print(content if isinstance(content, str) else json.dumps(content, indent=2))
    else:
        # Unknown shape — show the raw task result so nothing is silently lost.
        print(json.dumps(payload if payload is not None else res, indent=2))
    return 0


# ---------------------------------------------------------------------------
# allocations  (auth)
# ---------------------------------------------------------------------------
def cmd_allocations(args) -> int:
    api = _authed_client()
    projects = api.projects()
    plist = projects if isinstance(projects, list) else projects.get("data") or []
    if not plist:
        print("No projects found for your account.")
        if isinstance(projects, dict) and projects.get("error"):
            print(f"  (API said: {str(projects['error'])[:200]})", file=sys.stderr)
        return 0
    # Filter to a named project if asked (allocations() is one API call per
    # project, so listing ALL of them is slow when you're in many projects).
    if args.project:
        want = args.project.lower()
        plist = [p for p in plist if want in (p.get("name", "").lower())]
        if not plist:
            print(f"No project matching '{args.project}'. "
                  f"Run without --project to see your project names.")
            return 0
    elif len(plist) > args.max_projects:
        names = ", ".join(p.get("name", "?") for p in plist)
        print(f"You're in {len(plist)} projects: {names}\n")
        print(f"Showing allocations for the first {args.max_projects} "
              f"(use --project <name> for a specific one, or --max-projects N).\n")
        plist = plist[: args.max_projects]

    print("Your ALCF projects & allocations:")
    for p in plist:
        pid = p.get("id")
        pname = p.get("name", "?")
        print(f"\n  Project: {pname}")
        allocs = api.allocations(pid)
        alist = allocs if isinstance(allocs, list) else allocs.get("data") or []
        if not alist:
            print("    (no allocations)")
            continue
        # Real shape (verified 2026-07-31): each allocation =
        # {"id","entries":[{"allocation","usage","unit"}],"capability_uri":".../<resource>"}.
        for a in alist:
            cap = (a.get("capability_uri") or "").rstrip("/").split("/")[-1] or a.get("id", "?")
            entries = a.get("entries") or []
            if not entries:
                print(f"    {cap:12s} (no entries)")
                continue
            for e in entries:
                total = e.get("allocation")
                used = e.get("usage")
                unit = e.get("unit", "")
                remaining = None
                try:
                    remaining = round(float(total) - float(used), 1)
                except (TypeError, ValueError):
                    pass
                rem_s = f" remaining={remaining}" if remaining is not None else ""
                # round usage for readability
                try:
                    used_disp = round(float(used), 1)
                except (TypeError, ValueError):
                    used_disp = used
                print(f"    {cap:12s} allocated={total} used={used_disp}{rem_s} {unit}")
    if args.json:
        print("\n" + json.dumps(plist, indent=2))
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description="ALCF facility read-only helper")
    sub = ap.add_subparsers(dest="cmd", required=True)

    s = sub.add_parser("status", help="live system up/down + recent events (no auth)")
    s.add_argument("--events", type=int, default=8, help="how many recent events to show")
    s.add_argument("--json", action="store_true")
    s.set_defaults(func=cmd_status)

    j = sub.add_parser("jobs", help="list YOUR jobs on a cluster (auth)")
    j.add_argument("--cluster", default="polaris", choices=CLUSTERS)
    j.add_argument("--historical", action="store_true", help="include finished jobs")
    j.add_argument("--limit", type=int, default=100)
    j.add_argument("--json", action="store_true")
    j.set_defaults(func=cmd_jobs)

    o = sub.add_parser("output", help="read a job's stdout/stderr from Home/Eagle (auth)")
    o.add_argument("--path", required=True, help="absolute path on Home or Eagle")
    o.add_argument("--storage", default="home", choices=("home", "eagle"))
    o.add_argument("--lines", type=int, default=0, help="first N lines (head); 0 = byte view")
    o.add_argument("--size", type=int, default=4000, help="bytes to read (view mode)")
    o.add_argument("--offset", type=int, default=0, help="byte offset (view mode; use for 'tail')")
    o.add_argument("--json", action="store_true")
    o.set_defaults(func=cmd_output)

    a = sub.add_parser("allocations", help="your projects + node-hours (auth)")
    a.add_argument("--project", help="only this project (substring match on name)")
    a.add_argument("--max-projects", type=int, default=5, dest="max_projects",
                   help="cap projects shown when not filtering (default 5)")
    a.add_argument("--json", action="store_true")
    a.set_defaults(func=cmd_allocations)

    args = ap.parse_args()
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
