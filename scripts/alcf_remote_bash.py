#!/usr/bin/env python3
"""ALCF remote-bash helper — run shell commands on an ALCF compute node.

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
    run            Submit ONE bash command to a compute node and print the result.
    batch          Run SEVERAL commands through a SINGLE warm Executor session.
                   The first command pays the cold-start (~1 min while the MEP
                   boots a PBS job); every subsequent command reuses the SAME
                   warm node and returns in seconds. This is the fast path for an
                   agent doing many build steps (compile -> fix -> rebuild).

Everything is gated behind ALCF_ENABLE_GLOBUS_COMPUTE (default ON). Set it to 0
to hard-disable remote execution. Even when enabled, this runs arbitrary code on
ALCF charged to the user's allocation, so destructive commands require --yes.

WARM REUSE (important for latency):
    The node stays warm because the MEP keeps the PBS job block alive between
    submissions. Two ways to exploit it:
      * `batch` — multiple commands in one process/Executor (best; ~seconds/call
        after the first).
      * repeated `run` with IDENTICAL --endpoint/--account/--queue/--walltime —
        a later `run` lands on the still-running block if it hasn't idled out.
    Changing account/queue/walltime forces a new block (cold start again).

Examples:
    PY=/opt/hermes/.venv/bin/python
    $PY /opt/alcf/alcf_remote_bash.py authenticate
    $PY /opt/alcf/alcf_remote_bash.py run --account datascience \\
        --cmd "module load spack-pe-base cmake; cmake --version"
    # Interactive build loop on ONE warm node (commands from a file, one per line):
    $PY /opt/alcf/alcf_remote_bash.py batch --account datascience \\
        --run-dir '$HOME/nwchem' --cmds-file steps.txt
    # Activate a venv on the node for every command in the session:
    $PY /opt/alcf/alcf_remote_bash.py batch --account datascience \\
        --venv /eagle/datascience/<you>/myenv --cmds-file steps.txt

Verified end-to-end against the Polaris MEP on 2026-08-04 (compiled + ran a C
program on compute node x3206c0s31b0n0 under the user's account). MEP UUIDs
re-verified online 2026-08-06 (polaris, crux, sophia, edith).
"""
from __future__ import annotations

import argparse
import os
import re
import sys
import time

# The agent-in-a-box uses ONE combined Globus consent for all ALCF services
# (see alcf_combined_auth.py). Prefer driving Globus Compute from that shared
# consent so the user does not need a SEPARATE Globus Compute login. If the
# combined module or its tokens are unavailable (e.g. running this script
# standalone outside the box), we fall back to the SDK's own login flow.
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
try:
    import alcf_combined_auth as _combined  # noqa: E402
except Exception:  # pragma: no cover - standalone fallback
    _combined = None


def _make_compute_client():
    """Return a globus_compute_sdk.Client bound to the COMBINED consent, or None
    to let the SDK use its own login. Uses Client(app=<combined UserApp>) — the
    modern, supported way to hand the compute SDK an existing GlobusApp so it
    reuses our token instead of triggering a separate browser login."""
    if _combined is None or not _combined.compute_enabled():
        return None
    if not _combined.has_tokens():
        return None
    try:
        from globus_compute_sdk import Client
        app = _combined.build_user_app(interactive=False)
        return Client(app=app, do_version_check=False)
    except Exception:
        # Any problem building the shared client -> fall back to SDK's own login.
        return None

# ALCF documented multi-user endpoints (docs.alcf.anl.gov/services/globus-compute).
# These are stable, ALCF-operated MEP UUIDs. Jobs run under the SUBMITTING user's
# account (set via user_endpoint_config.account) — not the endpoint owner's.
# polaris/crux verified end-to-end in the 2026-08-04 spike; sophia/edith UUIDs
# contributed by ALCF colleagues and re-verified `status=online` 2026-08-06.
MEPS = {
    "polaris": "9a947ba5-f537-4681-acf3-cc66485aadec",
    "crux": "fd8b54bb-9452-411d-8e3a-09408156a886",
    "sophia": "fad4d968-8c9a-45ce-9fb4-60a9ab90be60",
    "edith": "a01b9350-e57d-4c8e-ad95-b4cb3c4cd1bb",
}

# Globus Compute caps a single result payload at ~10 MB. Keep combined
# stdout+stderr safely under that; oversized streams are truncated on the node
# (before serialization) so a chatty build can't blow up the RPC.
_RESULT_LIMIT = 9_500_000  # bytes, slightly below the 10 MB ceiling

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


def remote_bash(command: str, run_dir: str = "$HOME", venv: str = "",
                result_limit: int = _RESULT_LIMIT):
    """Executed ON the compute node. Returns (exit_code, stdout, stderr, host).

    Runs the command under a login-ish bash so `module` is available. cwd is
    run_dir (created if needed); defaults to the user's $HOME on the cluster.
    If ``venv`` is set, its activate script is sourced before the command.
    Output is truncated on the node to stay under Globus Compute's result cap.
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

    # Optionally activate a venv for this command (colleague's `config_key`
    # technique, generalized: source <venv>/bin/activate before the command).
    full_cmd = command
    if venv:
        activate = os.path.join(os.path.expandvars(venv).rstrip("/"), "bin", "activate")
        full_cmd = f"source {activate} && {command}"

    # `bash -lc` so module/spack init files load and `module` resolves; without
    # it, apptainer/module are not on PATH (verified in the spike).
    res = subprocess.run(
        ["bash", "-lc", full_cmd],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        cwd=workdir,
    )
    out = res.stdout.decode("utf-8", "replace")
    err = res.stderr.decode("utf-8", "replace")

    # Truncate on the node to respect the ~10 MB result limit. Split the budget
    # proportionally so a huge stdout doesn't starve a small stderr (or vice versa).
    def _truncate(s: str, max_bytes: int) -> str:
        b = s.encode("utf-8", "replace")
        if len(b) <= max_bytes:
            return s
        return b[:max_bytes].decode("utf-8", "ignore") + \
            f"\n[truncated — {len(b)} bytes total]"

    ob, eb = len(out.encode()), len(err.encode())
    total = ob + eb
    if total > result_limit:
        ratio = ob / total if total else 0.5
        out_budget = max(int(result_limit * ratio), min(ob, result_limit // 10))
        err_budget = max(result_limit - out_budget, min(eb, result_limit // 10))
        out = _truncate(out, out_budget)
        err = _truncate(err, err_budget)

    return (res.returncode, out, err, host)


def _enabled() -> bool:
    return os.environ.get("ALCF_ENABLE_GLOBUS_COMPUTE", "1") == "1"


def _require_enabled() -> None:
    if not _enabled():
        print(
            "ERROR: Globus Compute access is DISABLED "
            "(ALCF_ENABLE_GLOBUS_COMPUTE=0).\n"
            "remote-bash runs arbitrary shell commands on ALCF compute nodes under\n"
            "your allocation. It is ON by default; re-enable by starting the\n"
            "container WITHOUT `-e ALCF_ENABLE_GLOBUS_COMPUTE=0` (and complete the\n"
            "one-time Globus Compute login: `authenticate`).",
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
    """Trigger the Globus Compute login.

    In the agent-in-a-box the Globus Compute login is part of the ONE combined
    Globus consent (see alcf_combined_auth.py). So if the combined path is
    available we do NOT start a separate login here — we run (or confirm) the
    combined login, which covers inference + IRI + Globus Compute together.

    Standalone (no combined module), fall back to the compute SDK's own login:
    the first authenticated call prints a URL and blocks for the code.
    """
    # Preferred: combined single consent.
    if _combined is not None and _combined.compute_enabled():
        if _combined.has_tokens() and _has_tokens():
            print("[remote-bash] Globus Compute is already authenticated via the "
                  "combined ALCF login (one login covers inference + IRI + compute).")
            return 0
        print("[remote-bash] Globus Compute uses the COMBINED ALCF login (one login "
              "for all services). Running it now ...\n")
        return _combined._cli_authenticate()

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
    """True if Globus Compute is authenticated. Prefer the COMBINED consent
    (one login for all ALCF services); fall back to the compute SDK's own token
    store (~/.globus_compute/storage.db) for standalone use."""
    if _combined is not None and _combined.compute_enabled() and _combined.has_tokens():
        return True
    store = os.path.expanduser("~/.globus_compute/storage.db")
    return os.path.isfile(store) and os.path.getsize(store) > 0


def cmd_check(args) -> int:
    enabled = _enabled()
    toks = _has_tokens()
    print(f"globus compute      : {'enabled' if enabled else 'DISABLED (ALCF_ENABLE_GLOBUS_COMPUTE=0)'}")
    print(f"globus-compute login: {'present' if toks else 'MISSING (run: authenticate)'}")
    print(f"token store         : ~/.globus_compute/storage.db")
    print(f"endpoints           : " + ", ".join(f"{k}={v}" for k, v in MEPS.items()))
    # Non-zero exit if not ready, so the agent can branch on it.
    return 0 if (enabled and toks) else 1


def _preflight_run(args) -> int | None:
    """Shared gate for run/batch: enable flag + SDK + token cache. Returns an
    exit code to bail with, or None to proceed."""
    _require_enabled()
    _import_sdk()
    if not _has_tokens():
        print(
            "ERROR: no Globus Compute login. Run once on the host:\n"
            "    docker exec -it <container> /opt/hermes/.venv/bin/python \\\n"
            "      /opt/alcf/alcf_remote_bash.py authenticate",
            file=sys.stderr,
        )
        return 3
    return None


def _make_uec(args) -> dict:
    uec = {"account": args.account, "queue": args.queue, "walltime": args.walltime}
    if getattr(args, "nodes", 1) and args.nodes > 1:
        uec["nodes_per_block"] = args.nodes
    return uec


def _print_result(rc, out, err, host, dt, as_json):
    if as_json:
        import json
        print(json.dumps({"exit_code": rc, "host": host, "stdout": out,
                          "stderr": err, "seconds": round(dt, 1)}, indent=2))
        return
    print("\n" + "=" * 60)
    print(f"[remote-bash] compute node : {host}   ({dt:.1f}s)")
    print(f"[remote-bash] exit_code    : {rc}")
    print(f"[remote-bash] --- stdout ---\n{out}", end="" if out.endswith("\n") else "\n")
    if err.strip():
        print(f"[remote-bash] --- stderr ---\n{err}", end="" if err.endswith("\n") else "\n")
    print("=" * 60)


def cmd_run(args) -> int:
    bail = _preflight_run(args)
    if bail is not None:
        return bail
    from globus_compute_sdk import Executor
    from globus_compute_sdk.serialize import AllCodeStrategies, ComputeSerializer

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
    if args.venv:
        print(f"[remote-bash] venv={args.venv}", flush=True)
    print(f"[remote-bash] cmd={args.cmd!r}", flush=True)
    print("[remote-bash] submitting (cold start ~1 min while the endpoint boots a "
          "PBS job; warm calls are seconds) ...", flush=True)

    t0 = time.time()
    try:
        with Executor(endpoint_id=endpoint_id, serializer=serializer,
                      client=_make_compute_client(),
                      user_endpoint_config=_make_uec(args)) as gce:
            fut = gce.submit(remote_bash, args.cmd, args.run_dir, args.venv)
            rc, out, err, host = fut.result(timeout=args.timeout)
    except Exception as exc:
        print(f"\n[remote-bash] submission/result FAILED: {type(exc).__name__}: {exc}",
              file=sys.stderr)
        print("[remote-bash] Common causes: bad account/queue, endpoint warming up "
              "(retry), or a serialization/env mismatch. If jobs loop-fail, clean up "
              "on the cluster: `rm ~/.globus_compute/*/daemon.pid`.", file=sys.stderr)
        return 6
    dt = time.time() - t0

    _print_result(rc, out, err, host, dt, args.json)
    return 0 if rc == 0 else 2


def _load_cmds(args) -> list[str] | None:
    """Gather the batch command list from --cmds-file (one per line) or repeated
    --cmd. Blank lines and #-comments in the file are ignored."""
    cmds: list[str] = []
    if args.cmds_file:
        try:
            with open(args.cmds_file, encoding="utf-8") as fh:
                for line in fh:
                    s = line.rstrip("\n")
                    if s.strip() and not s.lstrip().startswith("#"):
                        cmds.append(s)
        except OSError as e:
            print(f"ERROR: cannot read --cmds-file {args.cmds_file!r}: {e}",
                  file=sys.stderr)
            return None
    cmds.extend(args.cmd or [])
    return cmds


def cmd_batch(args) -> int:
    """Run several commands through ONE warm Executor session.

    First command pays cold start; the rest reuse the same warm node. Stops on
    the first non-zero exit unless --keep-going. Emits a per-command summary.
    """
    bail = _preflight_run(args)
    if bail is not None:
        return bail
    from globus_compute_sdk import Executor
    from globus_compute_sdk.serialize import AllCodeStrategies, ComputeSerializer

    cmds = _load_cmds(args)
    if cmds is None:
        return 3
    if not cmds:
        print("ERROR: no commands given (use --cmds-file or one/more --cmd).",
              file=sys.stderr)
        return 3

    # Safety gate across the whole batch.
    if not args.yes:
        for c in cmds:
            hit = _looks_destructive(c)
            if hit:
                print(
                    f"REFUSED: a batch command matches a destructive pattern "
                    f"({hit!r}).\n  cmd: {c}\n"
                    "If you are sure, re-run the batch with --yes.",
                    file=sys.stderr,
                )
                return 5

    endpoint_id = MEPS[args.endpoint]
    serializer = ComputeSerializer(strategy_code=AllCodeStrategies())

    print(f"[remote-bash] BATCH of {len(cmds)} command(s) on one warm node",
          flush=True)
    print(f"[remote-bash] endpoint={args.endpoint} ({endpoint_id})", flush=True)
    print(f"[remote-bash] account={args.account} queue={args.queue} "
          f"walltime={args.walltime}", flush=True)
    if args.venv:
        print(f"[remote-bash] venv={args.venv}", flush=True)
    print("[remote-bash] first command pays cold start (~1 min); rest are warm ...",
          flush=True)

    results = []
    worst_rc = 0
    try:
        with Executor(endpoint_id=endpoint_id, serializer=serializer,
                      client=_make_compute_client(),
                      user_endpoint_config=_make_uec(args)) as gce:
            for i, c in enumerate(cmds, 1):
                t0 = time.time()
                fut = gce.submit(remote_bash, c, args.run_dir, args.venv)
                rc, out, err, host = fut.result(timeout=args.timeout)
                dt = time.time() - t0
                tag = "cold" if i == 1 else "warm"
                print(f"\n[remote-bash] === command {i}/{len(cmds)} "
                      f"({tag}, {dt:.1f}s, rc={rc}) on {host} ===", flush=True)
                print(f"  $ {c}", flush=True)
                _print_result(rc, out, err, host, dt, args.json)
                results.append((c, rc))
                if rc != 0:
                    worst_rc = rc
                    if not args.keep_going:
                        print(f"[remote-bash] command {i} failed (rc={rc}); "
                              "stopping batch (use --keep-going to continue).",
                              file=sys.stderr)
                        break
    except Exception as exc:
        print(f"\n[remote-bash] batch FAILED: {type(exc).__name__}: {exc}",
              file=sys.stderr)
        print("[remote-bash] If jobs loop-fail, clean up on the cluster: "
              "`rm ~/.globus_compute/*/daemon.pid`.", file=sys.stderr)
        return 6

    print(f"\n[remote-bash] batch done: "
          f"{sum(1 for _, rc in results if rc == 0)}/{len(results)} ok")
    return 0 if worst_rc == 0 else 2


def main() -> int:
    ap = argparse.ArgumentParser(
        description="Run shell command(s) on an ALCF compute node via Globus Compute.")
    sub = ap.add_subparsers(dest="cmd_name", required=True)

    a = sub.add_parser("authenticate", help="one-time interactive Globus Compute login")
    a.set_defaults(func=cmd_authenticate)

    c = sub.add_parser("check", help="report enable-flag + login status (no job)")
    c.set_defaults(func=cmd_check)

    # Shared submit args for run + batch.
    def _add_submit_args(p, multi=False):
        p.add_argument("--account", required=True, help="ALCF project to charge the PBS job")
        p.add_argument("--endpoint", default="polaris", choices=list(MEPS))
        p.add_argument("--queue", default="debug", help="PBS queue (default: debug)")
        p.add_argument("--walltime", default="0:10:00", help="HH:MM:SS (default 0:10:00)")
        p.add_argument("--nodes", type=int, default=1, help="nodes per block (default 1)")
        p.add_argument("--run-dir", default="$HOME", dest="run_dir",
                       help="working directory on the cluster (default $HOME)")
        p.add_argument("--venv", default="",
                       help="path to a venv on the cluster to `source .../bin/activate` "
                            "before each command (optional)")
        p.add_argument("--timeout", type=int, default=1200,
                       help="seconds to wait per command result (default 1200)")
        p.add_argument("--yes", action="store_true",
                       help="allow destructive-looking command(s)")
        p.add_argument("--json", action="store_true")

    r = sub.add_parser("run", help="submit ONE bash command to a compute node")
    _add_submit_args(r)
    r.add_argument("--cmd", required=True, help="shell command to run (bash -lc)")
    r.set_defaults(func=cmd_run)

    b = sub.add_parser("batch",
                       help="run SEVERAL commands through one warm node (fast path)")
    _add_submit_args(b)
    b.add_argument("--cmd", action="append",
                   help="a command to run; repeat for multiple (order preserved)")
    b.add_argument("--cmds-file", dest="cmds_file",
                   help="file with one command per line (# comments + blanks ignored)")
    b.add_argument("--keep-going", action="store_true",
                   help="continue after a non-zero exit instead of stopping")
    b.set_defaults(func=cmd_batch)

    args = ap.parse_args()
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
