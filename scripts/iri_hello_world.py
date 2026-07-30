#!/usr/bin/env python3
"""One-shot ALCF IRI hello-world (and general job) submitter for the agent.

Purpose: give the agent a SINGLE reliable command instead of hand-rolling dozens
of urllib one-liners. Uses the bundled auth script + iri_api_client (which sets
the User-Agent Cloudflare requires). Verified recipe: duration is INT seconds,
stdout/stderr go to HOME or EAGLE (never Polaris fs), one Globus token covers
compute+account+filesystem.

Usage (run with the bundled python):
    /opt/hermes/.venv/bin/python /opt/alcf/iri_hello_world.py \
        --project datascience --home /home/<username>

    # or a custom command:
    ... --executable /bin/echo --args "Hello World" --queue debug --seconds 300

It will: resolve the project id, submit to Polaris, poll to a terminal state,
and print the job id, final state, and the stdout file contents. Exit code 0 on
success.
"""
from __future__ import annotations

import argparse
import os
import sys
import time

# Make the bundled client importable regardless of cwd.
_SKILL_SCRIPTS = "/opt/data/skills/research/alcf-iri-facility-api/scripts"
_ALT_SCRIPTS = "/opt/alcf/skills/alcf-iri-facility-api/scripts"
for p in (_SKILL_SCRIPTS, _ALT_SCRIPTS):
    if os.path.isdir(p) and p not in sys.path:
        sys.path.insert(0, p)

try:
    from iri_api_client import IRI  # type: ignore
except Exception as exc:  # pragma: no cover
    print(f"ERROR: could not import iri_api_client: {exc}", file=sys.stderr)
    sys.exit(2)

AUTH_SCRIPT = "/opt/alcf/alcf_facility_api_globus_token.py"
PYTHON = "/opt/hermes/.venv/bin/python"


def get_token() -> str:
    import subprocess
    out = subprocess.check_output([PYTHON, AUTH_SCRIPT, "get_access_token"], text=True)
    tok = out.strip()
    if not tok or tok.lower().startswith(("error", "traceback")):
        print("ERROR: no valid IRI token. Have the user run:\n"
              f"  docker exec -it <container> {PYTHON} {AUTH_SCRIPT} authenticate",
              file=sys.stderr)
        sys.exit(3)
    return tok


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--project", required=True, help="ALCF project/account name, e.g. datascience")
    ap.add_argument("--home", required=True, help="your ALCF home dir, e.g. /home/parton")
    ap.add_argument("--executable", default="/bin/echo")
    ap.add_argument("--args", default="Hello World")
    ap.add_argument("--name", default="iri_hello")
    ap.add_argument("--queue", default="debug")
    ap.add_argument("--seconds", type=int, default=600,
                    help="walltime in SECONDS (int). NOTE: the Polaris 'debug' "
                         "queue rejects very short walltimes with HTTP 500 "
                         "'Job violates queue ... limits' — keep >= ~300s.")
    ap.add_argument("--nodes", type=int, default=1)
    ap.add_argument("--poll", type=int, default=10, help="seconds between status polls")
    ap.add_argument("--timeout", type=int, default=900, help="max seconds to wait")
    a = ap.parse_args()

    api = IRI(token=get_token())

    out_path = f"{a.home.rstrip('/')}/{a.name}.out"
    err_path = f"{a.home.rstrip('/')}/{a.name}.err"

    body = {
        "executable": a.executable,
        "arguments": a.args.split() if a.args else [],
        "name": a.name,
        "stdout_path": out_path,
        "stderr_path": err_path,
        "resources": {"node_count": a.nodes},
        "attributes": {
            "duration": int(a.seconds),          # INTEGER SECONDS — not HH:MM:SS
            "queue_name": a.queue,
            "account": a.project,
            "custom_attributes": {"filesystems": "home:eagle"},
        },
    }

    status, resp = api._req("POST", f"/compute/job/{IRI.POLARIS}", body=body)
    if status != 200:
        print(f"ERROR: submit failed HTTP {status}: {resp}", file=sys.stderr)
        return 4
    job_id = resp.get("id")
    print(f"submitted: {job_id}  initial state={resp.get('status', {}).get('state')}")

    # Poll to terminal state.
    deadline = time.time() + a.timeout
    state = None
    while time.time() < deadline:
        s, js = api._req("GET", f"/compute/status/{IRI.POLARIS}/{job_id}", params={"historical": "true"})
        state = (js.get("status") or {}).get("state") if isinstance(js, dict) else None
        print(f"  state={state}")
        if state in ("completed", "failed", "cancelled"):
            break
        time.sleep(a.poll)

    print(f"final state: {state}")
    # Fetch stdout (home path). filesystem view is async in the client.
    try:
        home_res = IRI.HOME
        view = api.view(home_res, out_path, size=4000) if hasattr(api, "view") else None
        print("---- stdout ----")
        print(view if view is not None else "(use api.view to read; see iri_api_client)")
    except Exception as exc:  # pragma: no cover
        print(f"(could not auto-read stdout: {exc}; file is at {out_path})")
    return 0 if state == "completed" else 5


if __name__ == "__main__":
    sys.exit(main())
