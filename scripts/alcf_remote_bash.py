#!/usr/bin/env python3
"""ALCF remote-bash helper — run a shell command on an ALCF compute node.

This is the one capability that lets the containerized agent BUILD and RUN
software on ALCF systems (compile, `pip install`, `apptainer build`, run test
suites, etc.). It submits the command to an ALCF **multi-user Globus Compute
endpoint** (MEP); the MEP launches a PBS job on a compute node **under the
user's own account/allocation** and returns exit_code + stdout + stderr.

    docs.alcf.anl.gov/services/globus-compute

Subcommands:
    authenticate   One-time interactive Globus login for Globus Compute
                   (a SEPARATE login from inference + IRI). Prints a URL; open
                   it, paste the code back.
    check          Report whether the Globus Compute login exists (no network
                   job submitted).
    run            Submit a bash command to a compute node and print the result.

Everything is gated behind ALCF_ENABLE_REMOTE_BASH=1 because it is arbitrary
remote code execution charged to the user's allocation. Default is OFF.

Examples:
    PY=/opt/hermes/.venv/bin/python
    $PY /opt/alcf/alcf_remote_bash.py authenticate
    $PY /opt/alcf/alcf_remote_bash.py run --account datascience \\
        --cmd "module load spack-pe-base cmake; cmake --version"
    $PY /opt/alcf/alcf_remote_bash.py run --account datascience --endpoint crux \\
        --queue debug --walltime 0:20:00 --cmd "cd \\$HOME/myproj && make -j"

Verified end-to-end against the Polaris MEP on 2026-08-04 (compiled + ran a C
program on compute node x3206c0s31b0n0 under the user's account).
"""
from __future__ import annotations

import argparse
import os
import re
import sys
import time

# ALCF documented multi-user endpoints (docs.alcf.anl.gov/services/globus-compute).
# These are stable, ALCF-operated MEP UUIDs. Jobs run under the SUBMITTING user's
# account (set via user_endpoint_config.account) — not the endpoint owner's.
MEPS = {
    "polaris": "9a947ba5-f537-4681-acf3-cc66485aadec",
    "crux": "fd8b54bb-9452-411d-8e3a-09408156a886",
}

# Commands we refuse to run without --yes. These are broad, destructive, or
# expensive patterns; the gate is a safety net, not a security boundary (the
# real boundary is ALCF's own auth + the user's allocation). Tuned to catch the
# obvious footguns while staying out of the way of normal build/run commands.
_DESTRUCTIVE = [
    r"\brm\s+-[a-zA-Z]*r[a-zA-Z]*f\b",   # rm -rf / rm -fr and variants
    r"\brm\s+-[a-zA-Z]*f[a-zA-Z]*r\b",
    r"\bmkfs\b",
    r"\bdd\s+.*\bof=/",                   # dd writing to a device/path
    r":\(\)\s*\{.*\};:",                  # fork bomb
    r"\bchmod\s+-R\s+0*000\b",
    r">\s*/dev/sd",
    r"\bshutdown\b|\breboot\b",
    r"\bqdel\s+all\b",
]


def _looks_destructive(cmd: str) -> str | None:
    for pat in _DESTRUCTIVE:
        if re.search(pat, cmd):
            return pat
    return None


def remote_bash(command: str, run_dir: str = "$HOME"):
    """Executed ON the compute node. Returns (exit_code, stdout, stderr, host).

    Runs the command under a login-ish bash so `module` is available. cwd is
    run_dir (created if needed); defaults to the user's $HOME on the cluster.
    """
    import os
    import socket
    import subprocess

    host = socket.gethostname()
    workdir = os.path.expandvars(run_dir)
    try:
        os.makedirs(workdir, exist_ok=True)
    except Exception:
        workdir = os.path.expanduser("~")

    # `bash -lc` so module/spack init files load and `module` resolves; without
    # it, apptainer/module are not on PATH (verified in the spike).
    res = subprocess.run(
        ["bash", "-lc", command],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        cwd=workdir,
    )
    return (
        res.returncode,
        res.stdout.decode("utf-8", "replace"),
        res.stderr.decode("utf-8", "replace"),
        host,
    )


def _enabled() -> bool:
    return os.environ.get("ALCF_ENABLE_REMOTE_BASH", "0") == "1"


def _require_enabled() -> None:
    if not _enabled():
        print(
            "ERROR: remote-bash is DISABLED. This capability runs arbitrary shell\n"
            "commands on ALCF compute nodes under your allocation, so it is opt-in.\n"
            "Enable it by starting the container with:  -e ALCF_ENABLE_REMOTE_BASH=1\n"
            "(and complete the one-time Globus Compute login: `authenticate`).",
            file=sys.stderr,
        )
        sys.exit(4)


def _import_sdk():
    try:
        from globus_compute_sdk import Executor  # noqa: F401
        from globus_compute_sdk.serialize import (  # noqa: F401
            AllCodeStrategies,
            ComputeSerializer,
        )
    except Exception as exc:  # pragma: no cover
        print(
            f"ERROR: globus-compute-sdk not importable: {exc}\n"
            "Install into the bundled interpreter: "
            "`uv pip install --python /opt/hermes/.venv/bin/python globus-compute-sdk`",
            file=sys.stderr,
        )
        sys.exit(2)


def cmd_authenticate(args) -> int:
    """Trigger the one-time interactive Globus Compute login.

    The SDK caches tokens in ~/.globus_compute/storage.db. The first call that
    needs auth prints a URL and blocks for the code. We do the lightest possible
    authenticated call (construct a Client and hit the web API) to drive login
    without submitting a job.
    """
    _import_sdk()
    from globus_compute_sdk import Client

    print("[remote-bash] Starting Globus Compute login (separate from inference + IRI).")
    print("[remote-bash] A URL will be printed — open it, log in with your ALCF/Globus")
    print("[remote-bash] account, and paste the authorization code back here.\n")
    gcc = Client()
    # version_check / get_version forces the auth handshake + a real API call.
    try:
        _ = gcc.web_client.get_version()
    except Exception as exc:
        # Even if the version call has issues, the login prompt has already run;
        # verify by checking the token store below.
        print(f"[remote-bash] (note: post-login API probe said: {exc})")
    if _has_tokens():
        print("\n[remote-bash] Globus Compute authentication OK "
              "(tokens cached at ~/.globus_compute/storage.db).")
        return 0
    print("\n[remote-bash] Authentication did not complete — no token cache found.",
          file=sys.stderr)
    return 1


def _has_tokens() -> bool:
    store = os.path.expanduser("~/.globus_compute/storage.db")
    return os.path.isfile(store) and os.path.getsize(store) > 0


def cmd_check(args) -> int:
    enabled = _enabled()
    toks = _has_tokens()
    print(f"remote-bash enabled : {'yes' if enabled else 'no (set ALCF_ENABLE_REMOTE_BASH=1)'}")
    print(f"globus-compute login: {'present' if toks else 'MISSING (run: authenticate)'}")
    print(f"token store         : ~/.globus_compute/storage.db")
    print(f"endpoints           : " + ", ".join(f"{k}={v}" for k, v in MEPS.items()))
    # Non-zero exit if not ready, so the agent can branch on it.
    return 0 if (enabled and toks) else 1


def cmd_run(args) -> int:
    _require_enabled()
    _import_sdk()
    from globus_compute_sdk import Executor
    from globus_compute_sdk.serialize import AllCodeStrategies, ComputeSerializer

    if not _has_tokens():
        print(
            "ERROR: no Globus Compute login. Run once on the host:\n"
            "    docker exec -it <container> /opt/hermes/.venv/bin/python \\\n"
            "      /opt/alcf/alcf_remote_bash.py authenticate",
            file=sys.stderr,
        )
        return 3

    hit = _looks_destructive(args.cmd)
    if hit and not args.yes:
        print(
            f"REFUSED: command matches a destructive pattern ({hit!r}).\n"
            f"  cmd: {args.cmd}\n"
            "If you are sure, re-run with --yes. (This runs on ALCF under your "
            "allocation.)",
            file=sys.stderr,
        )
        return 5

    endpoint_id = MEPS[args.endpoint]
    serializer = ComputeSerializer(strategy_code=AllCodeStrategies())

    print(f"[remote-bash] endpoint={args.endpoint} ({endpoint_id})", flush=True)
    print(f"[remote-bash] account={args.account} queue={args.queue} "
          f"walltime={args.walltime}", flush=True)
    print(f"[remote-bash] cmd={args.cmd!r}", flush=True)
    print("[remote-bash] submitting (cold start ~1 min while the endpoint boots a "
          "PBS job; warm calls are seconds) ...", flush=True)

    uec = {"account": args.account, "queue": args.queue, "walltime": args.walltime}
    if args.nodes and args.nodes > 1:
        uec["nodes_per_block"] = args.nodes

    t0 = time.time()
    try:
        with Executor(endpoint_id=endpoint_id, serializer=serializer,
                      user_endpoint_config=uec) as gce:
            fut = gce.submit(remote_bash, args.cmd, args.run_dir)
            rc, out, err, host = fut.result(timeout=args.timeout)
    except Exception as exc:
        print(f"\n[remote-bash] submission/result FAILED: {type(exc).__name__}: {exc}",
              file=sys.stderr)
        print("[remote-bash] Common causes: bad account/queue, endpoint warming up "
              "(retry), or a serialization/env mismatch. If jobs loop-fail, clean up "
              "on the cluster: `rm ~/.globus_compute/*/daemon.pid`.", file=sys.stderr)
        return 6
    dt = time.time() - t0

    if args.json:
        import json
        print(json.dumps({"exit_code": rc, "host": host, "stdout": out,
                          "stderr": err, "seconds": round(dt, 1)}, indent=2))
        return 0 if rc == 0 else 2

    print("\n" + "=" * 60)
    print(f"[remote-bash] compute node : {host}   ({dt:.1f}s)")
    print(f"[remote-bash] exit_code    : {rc}")
    print(f"[remote-bash] --- stdout ---\n{out}", end="" if out.endswith("\n") else "\n")
    if err.strip():
        print(f"[remote-bash] --- stderr ---\n{err}", end="" if err.endswith("\n") else "\n")
    print("=" * 60)
    return 0 if rc == 0 else 2


def main() -> int:
    ap = argparse.ArgumentParser(
        description="Run a shell command on an ALCF compute node via Globus Compute.")
    sub = ap.add_subparsers(dest="cmd_name", required=True)

    a = sub.add_parser("authenticate", help="one-time interactive Globus Compute login")
    a.set_defaults(func=cmd_authenticate)

    c = sub.add_parser("check", help="report enable-flag + login status (no job)")
    c.set_defaults(func=cmd_check)

    r = sub.add_parser("run", help="submit a bash command to a compute node")
    r.add_argument("--account", required=True, help="ALCF project to charge the PBS job")
    r.add_argument("--cmd", required=True, help="shell command to run (bash -lc)")
    r.add_argument("--endpoint", default="polaris", choices=list(MEPS))
    r.add_argument("--queue", default="debug", help="PBS queue (default: debug)")
    r.add_argument("--walltime", default="0:10:00", help="HH:MM:SS (default 0:10:00)")
    r.add_argument("--nodes", type=int, default=1, help="nodes per block (default 1)")
    r.add_argument("--run-dir", default="$HOME", dest="run_dir",
                   help="working directory on the cluster (default $HOME)")
    r.add_argument("--timeout", type=int, default=1200,
                   help="seconds to wait for the result (default 1200)")
    r.add_argument("--yes", action="store_true", help="allow a destructive-looking command")
    r.add_argument("--json", action="store_true")
    r.set_defaults(func=cmd_run)

    args = ap.parse_args()
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
