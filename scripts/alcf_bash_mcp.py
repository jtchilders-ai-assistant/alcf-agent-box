#!/usr/bin/env python3
"""ALCF bash MCP server — expose remote-bash as the ONE tool models know: `bash`.

WHY: the agent's open models (gemma-4, gpt-oss) are trained on a direct `bash`
tool with a single `command` argument. Driving compute nodes through the
`alcf_remote_bash.py run --account X --cmd "..."` CLI is an unfamiliar shape and
measurably error-prone for them (nested quoting, forgotten flags). This server
wraps the SAME Globus Compute machinery in an MCP stdio server exposing exactly
one tool:

    bash(command)        # + optional: account (sticky), confirm (destructive)

Account / queue / walltime / endpoint are bound server-side from env (rendered
into the Hermes `mcp_servers:` block by the entrypoint), so the model never
sees them. The server also:

  * holds ONE warm Executor for its whole lifetime, so every call after the
    first reuses the same PBS block (~1 s warm vs ~1 min cold), and
  * passes a per-endpoint session id to remote_bash, so `cd` / `export` /
    `module load` persist across calls (see alcf_remote_bash.remote_bash).

Net effect: the model gets what looks and behaves like a persistent login shell
on an ALCF compute node. The session id defaults to the endpoint name — the
SAME default the CLI uses — so MCP calls and CLI calls see one shared "shell".

Config (env):
    ALCF_BASH_ACCOUNT       default ALCF project to charge. If unset, the model
                            must pass account='<project>' once; it then sticks.
    ALCF_BASH_ENDPOINT      polaris (default) | crux | sophia | edith
    ALCF_BASH_QUEUE         PBS queue (default: debug)
    ALCF_BASH_WALLTIME      HH:MM:SS (default: 1:00:00)
    ALCF_BASH_RUN_DIR       initial working dir on the cluster (default: $HOME)
    ALCF_BASH_VENV          cluster venv to activate per command (default: none)
    ALCF_BASH_TIMEOUT       seconds to wait per command result (default: 1200)
    ALCF_BASH_MAX_OUTPUT    returned-output byte budget (default: 20000)
    ALCF_BASH_SESSION       state session id (default: the endpoint name)
    ALCF_ENABLE_GLOBUS_COMPUTE=0   hard-disable (calls return an error)
    ALCF_BASH_LOCAL_TEST=1  run commands LOCALLY via subprocess instead of
                            Globus Compute — for unit tests ONLY.

Protocol: MCP over stdio — newline-delimited JSON-RPC 2.0 (initialize,
tools/list, tools/call, ping). Deliberately dependency-free: no `mcp` package
to pin, and the surface we need is ~four methods. stdout carries protocol
frames ONLY; all diagnostics go to stderr.

Written 2026-08-20; protocol + state behavior covered by tests/test_layer0.py
(local-exec mode). Not yet verified against a live Hermes MCP client or the
live MEP.
"""
from __future__ import annotations

import json
import os
import sys
import time

# Reuse the CLI helper's machinery (MEPS, destructive gate, combined-auth
# client, and the remote_bash function itself) instead of duplicating it.
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import alcf_remote_bash as rb  # noqa: E402

SERVER_INFO = {"name": "alcf-bash", "version": "0.1.0"}
PROTOCOL_DEFAULT = "2024-11-05"


def _log(msg: str) -> None:
    print(f"[alcf-bash-mcp] {msg}", file=sys.stderr, flush=True)


class BashSession:
    """Server-side binding of all the knobs the model should never see, plus
    the one warm Executor reused across tool calls."""

    def __init__(self) -> None:
        env = os.environ.get
        self.account = (env("ALCF_BASH_ACCOUNT", "") or "").strip()
        self.endpoint = env("ALCF_BASH_ENDPOINT", "polaris")
        if self.endpoint not in rb.MEPS:
            _log(f"unknown ALCF_BASH_ENDPOINT={self.endpoint!r}; using polaris")
            self.endpoint = "polaris"
        self.queue = env("ALCF_BASH_QUEUE", "debug")
        self.walltime = env("ALCF_BASH_WALLTIME", "1:00:00")
        self.run_dir = env("ALCF_BASH_RUN_DIR", "$HOME")
        self.venv = env("ALCF_BASH_VENV", "")
        self.timeout = int(env("ALCF_BASH_TIMEOUT", "1200"))
        self.max_output = int(env("ALCF_BASH_MAX_OUTPUT", "20000"))
        # Default session = endpoint name, matching the CLI default, so the
        # model's MCP shell and any CLI-driven commands share one state.
        self.session_id = env("ALCF_BASH_SESSION", "") or self.endpoint
        self.local = env("ALCF_BASH_LOCAL_TEST", "") == "1"
        self._executor = None

    # -- executor lifecycle ---------------------------------------------------
    def _get_executor(self):
        if self._executor is None:
            from globus_compute_sdk import Executor
            from globus_compute_sdk.serialize import (
                AllCodeStrategies,
                ComputeSerializer,
            )
            self._executor = Executor(
                endpoint_id=rb.MEPS[self.endpoint],
                serializer=ComputeSerializer(strategy_code=AllCodeStrategies()),
                client=rb._make_compute_client(),
                user_endpoint_config={
                    "account": self.account,
                    "queue": self.queue,
                    "walltime": self.walltime,
                },
            )
            _log(f"executor created: endpoint={self.endpoint} "
                 f"account={self.account} queue={self.queue} walltime={self.walltime}")
        return self._executor

    def _drop_executor(self) -> None:
        ex, self._executor = self._executor, None
        if ex is not None:
            try:
                ex.shutdown(wait=False, cancel_futures=True)
            except Exception:
                pass

    def set_account(self, account: str) -> None:
        if account != self.account:
            self.account = account
            self._drop_executor()  # UEC changed -> next call builds a fresh one
            _log(f"account set to {account!r} (sticky for this session)")

    def close(self) -> None:
        self._drop_executor()

    # -- execution -------------------------------------------------------------
    def run(self, command: str):
        """Returns remote_bash's (rc, out, err, host, meta) 5-tuple."""
        if self.local:
            return rb.remote_bash(command, self.run_dir, self.venv,
                                  self.session_id, False, self.max_output)
        try:
            fut = self._get_executor().submit(
                rb.remote_bash, command, self.run_dir, self.venv,
                self.session_id, False, self.max_output)
            return fut.result(timeout=self.timeout)
        except Exception:
            # Anything from an idled-out AMQP connection to a bad UEC: drop the
            # executor so the NEXT call starts clean, then surface the error.
            self._drop_executor()
            raise

    # -- tool surface -----------------------------------------------------------
    def tool_spec(self) -> dict:
        desc = (
            f"Run a shell command on an ALCF {self.endpoint} compute node "
            f"(a PBS job under your project, queue={self.queue}). Behaves like a "
            "persistent shell: cd, exported variables, and `module load` persist "
            "across calls, and files persist on the cluster filesystems. The "
            "first call can take ~1 minute while a node boots; later calls are "
            f"~1 s. Output beyond ~{self.max_output} bytes is returned "
            "head+tail with the full log saved on the node (path shown in the "
            "truncation marker) — grep/tail that file in a follow-up command "
            "instead of re-running."
        )
        props = {
            "command": {
                "type": "string",
                "description": "The bash command to run on the compute node.",
            },
            "account": {
                "type": "string",
                "description": (
                    "ALCF project to charge. Only needed once, and only if the "
                    "server has no default; it sticks for the whole session."
                ),
            },
            "confirm": {
                "type": "boolean",
                "description": (
                    "Set true ONLY after the user explicitly approved a command "
                    "that was refused as destructive-looking."
                ),
            },
        }
        return {
            "name": "bash",
            "description": desc,
            "inputSchema": {
                "type": "object",
                "properties": props,
                "required": ["command"],
            },
        }


def _text_result(text: str, is_error: bool = False) -> dict:
    return {"content": [{"type": "text", "text": text}], "isError": is_error}


def _format_result(rc: int, out: str, err: str, host: str, dt: float) -> str:
    parts = []
    if out:
        parts.append(out.rstrip("\n"))
    if err.strip():
        parts.append("--- stderr ---\n" + err.rstrip("\n"))
    parts.append(f"[exit {rc} · {host} · {dt:.1f}s]")
    return "\n".join(parts)


def handle_call(session: BashSession, args: dict) -> dict:
    command = args.get("command")
    if not isinstance(command, str) or not command.strip():
        return _text_result("missing required argument: command", is_error=True)

    if not rb._enabled():
        return _text_result(
            "Remote execution is disabled (ALCF_ENABLE_GLOBUS_COMPUTE=0). Tell "
            "the user it was turned off at container start and how to re-enable "
            "(start the container WITHOUT -e ALCF_ENABLE_GLOBUS_COMPUTE=0).",
            is_error=True)

    acct = args.get("account")
    if isinstance(acct, str) and acct.strip():
        session.set_account(acct.strip())
    if not session.account and not session.local:
        return _text_result(
            "No ALCF project is set to charge the PBS job. Pass "
            "account='<project>' once (it sticks for the session). Look the "
            "project up via IRI GET /account/projects, or ask the user. The "
            "container can also set a default with -e ALCF_BASH_ACCOUNT=<project>.",
            is_error=True)

    if not session.local and not rb._has_tokens():
        return _text_result(
            "No Globus Compute login. Ask the user to run on their host:\n"
            "  docker exec -it <container> /opt/hermes/.venv/bin/python "
            "/opt/alcf/alcf_remote_bash.py authenticate",
            is_error=True)

    bad_paths = rb._container_path_refs(command)
    if bad_paths:
        return _text_result(
            f"REFUSED: command references container-only path(s) {bad_paths} "
            "which do not exist on any ALCF node. Likely cause: a container "
            "path (e.g. $HOME expanded in the agent container to /opt/data) "
            "leaked into a command meant for the cluster. Rewrite it with "
            "cluster paths (node $HOME is /home/<user>; probe with "
            "`whoami && echo $HOME && pwd`).",
            is_error=True)

    hit = rb._looks_destructive(command)
    if hit and not args.get("confirm"):
        return _text_result(
            f"REFUSED: command matches a destructive pattern ({hit!r}). This "
            "runs on ALCF under the user's allocation. Confirm with the user in "
            "plain language what will run and where; only then re-call with "
            "confirm=true.",
            is_error=True)

    t0 = time.time()
    try:
        rc, out, err, host, meta = session.run(command)
    except Exception as exc:
        return _text_result(
            f"submission/result FAILED: {type(exc).__name__}: {exc}. Common "
            "causes: bad account/queue, endpoint warming up (retry once), or a "
            "result timeout. If jobs loop-fail, clean up on the cluster: "
            "rm ~/.globus_compute/*/daemon.pid",
            is_error=True)
    return _text_result(_format_result(rc, out, err, host, time.time() - t0))


def _write(msg: dict) -> None:
    sys.stdout.write(json.dumps(msg) + "\n")
    sys.stdout.flush()


def _reply(mid, result: dict) -> None:
    _write({"jsonrpc": "2.0", "id": mid, "result": result})


def _reply_err(mid, code: int, message: str) -> None:
    _write({"jsonrpc": "2.0", "id": mid,
            "error": {"code": code, "message": message}})


def main() -> int:
    session = BashSession()
    _log(f"ready: endpoint={session.endpoint} queue={session.queue} "
         f"session={session.session_id} account={session.account or '(unset)'}"
         f"{' LOCAL-TEST MODE' if session.local else ''}")
    try:
        for raw in sys.stdin:
            raw = raw.strip()
            if not raw:
                continue
            try:
                msg = json.loads(raw)
            except json.JSONDecodeError:
                _log(f"skipping non-JSON line ({len(raw)} bytes)")
                continue
            method = msg.get("method")
            mid = msg.get("id")
            if method == "initialize":
                proto = ((msg.get("params") or {}).get("protocolVersion")
                         or PROTOCOL_DEFAULT)
                _reply(mid, {"protocolVersion": proto,
                             "capabilities": {"tools": {}},
                             "serverInfo": SERVER_INFO})
            elif method in ("notifications/initialized", "notifications/cancelled"):
                pass  # notifications: no response
            elif method == "ping":
                _reply(mid, {})
            elif method == "tools/list":
                _reply(mid, {"tools": [session.tool_spec()]})
            elif method == "tools/call":
                params = msg.get("params") or {}
                if params.get("name") != "bash":
                    _reply_err(mid, -32602, f"unknown tool: {params.get('name')!r}")
                else:
                    _reply(mid, handle_call(session, params.get("arguments") or {}))
            elif mid is not None:
                _reply_err(mid, -32601, f"method not found: {method}")
    except KeyboardInterrupt:
        pass
    finally:
        session.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
