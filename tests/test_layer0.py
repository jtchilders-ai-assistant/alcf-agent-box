#!/usr/bin/env python3
"""Layer-0 unit tests: remote-bash session state, output budget, destructive
gate, and the alcf-bash MCP server protocol round-trip.

Everything runs LOCALLY (no Globus, no network): remote_bash() is a plain
function that shells out, so we exercise the real wrapper + state file + spill
logic under a throwaway $HOME; the MCP server is exercised as a subprocess in
ALCF_BASH_LOCAL_TEST=1 mode, which routes tool calls through the same
remote_bash() instead of Globus Compute.

Run:  python3 -m unittest discover -s tests -v
"""
from __future__ import annotations

import json
import os
import select
import shutil
import subprocess
import sys
import tempfile
import unittest

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SCRIPTS = os.path.join(REPO, "scripts")
sys.path.insert(0, SCRIPTS)

import alcf_remote_bash as rb  # noqa: E402


class _FakeHomeMixin(unittest.TestCase):
    """Point $HOME at a throwaway dir so state files + spill logs land there."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="alcf_layer0_")
        self.home = os.path.join(self.tmp, "home")
        os.makedirs(self.home)
        self._old_home = os.environ.get("HOME")
        os.environ["HOME"] = self.home

    def tearDown(self):
        if self._old_home is not None:
            os.environ["HOME"] = self._old_home
        shutil.rmtree(self.tmp, ignore_errors=True)

    def rbash(self, cmd, session="t", fresh=False, max_output=20000):
        return rb.remote_bash(cmd, "$HOME", "", session, fresh, max_output)


class SessionStateTests(_FakeHomeMixin):
    def test_cd_and_export_persist_across_calls(self):
        rc, *_ = self.rbash('mkdir -p "$HOME/w1" && cd "$HOME/w1" && export FOO=hello')
        self.assertEqual(rc, 0)
        rc, out, err, host, meta = self.rbash('echo "$FOO @ $(pwd)"')
        self.assertEqual(rc, 0, err)
        self.assertIn("hello @", out)
        self.assertTrue(out.strip().endswith("/w1"), out)

    def test_path_change_persists(self):
        # `module load` works by mutating exported env (PATH, LOADEDMODULES...),
        # so a persisting PATH edit is the local proxy for module persistence.
        rc, *_ = self.rbash('export PATH="$HOME/fakebin:$PATH"')
        self.assertEqual(rc, 0)
        rc, out, *_ = self.rbash('echo "$PATH"')
        self.assertTrue(out.startswith(f"{self.home}/fakebin:"), out)

    def test_unexported_var_does_not_persist(self):
        self.rbash("BAR=1")
        rc, out, *_ = self.rbash('echo "${BAR:-unset}"')
        self.assertEqual(out.strip(), "unset")

    def test_fresh_wipes_state(self):
        self.rbash("export FOO=stale")
        rc, out, *_ = self.rbash('echo "${FOO:-unset}"', fresh=True)
        self.assertEqual(out.strip(), "unset")

    def test_sessions_are_isolated(self):
        self.rbash("export FOO=inA", session="sA")
        rc, out, *_ = self.rbash('echo "${FOO:-unset}"', session="sB")
        self.assertEqual(out.strip(), "unset")

    def test_no_session_means_no_state(self):
        self.rbash("export FOO=x", session="")
        rc, out, *_ = self.rbash('echo "${FOO:-unset}"', session="")
        self.assertEqual(out.strip(), "unset")
        state_dir = os.path.join(self.home, ".alcf_remote_bash")
        states = [f for f in os.listdir(state_dir) if f.endswith(".state")]
        self.assertEqual(states, [])

    def test_per_job_identity_vars_not_saved(self):
        self.rbash("export PBS_JOBID=zzz KEEP=yes")
        rc, out, *_ = self.rbash('echo "${PBS_JOBID:-unset} ${KEEP:-unset}"')
        self.assertEqual(out.strip(), "unset yes")

    def test_exit_code_propagates(self):
        rc, *_ = self.rbash("exit 7")
        self.assertEqual(rc, 7)

    def test_failed_command_still_saves_state(self):
        rc, *_ = self.rbash("export FOO=survives; false")
        self.assertEqual(rc, 1)
        rc, out, *_ = self.rbash('echo "$FOO"')
        self.assertEqual(out.strip(), "survives")


class OutputBudgetTests(_FakeHomeMixin):
    def test_big_stdout_truncated_and_spilled(self):
        rc, out, err, host, meta = self.rbash(
            "python3 -c \"print('x' * 100000)\"", session="tr", max_output=2000)
        self.assertEqual(rc, 0, err)
        self.assertLess(len(out.encode()), 6000)
        self.assertIn("bytes omitted", out)
        # head+tail: both ends of the original stream survive
        self.assertTrue(out.startswith("x"))
        self.assertTrue(out.rstrip("\n").endswith("x"))
        self.assertTrue(meta["stdout_log"], meta)
        self.assertTrue(os.path.isfile(meta["stdout_log"]))
        self.assertGreaterEqual(os.path.getsize(meta["stdout_log"]), 100000)
        self.assertIn(meta["stdout_log"], out)  # marker names the spill file

    def test_big_stderr_truncated_separately(self):
        rc, out, err, host, meta = self.rbash(
            "python3 -c \"import sys; sys.stderr.write('e' * 50000)\"",
            session="tr2", max_output=2000)
        self.assertEqual(rc, 0)
        self.assertIn("bytes omitted", err)
        self.assertTrue(meta["stderr_log"])
        self.assertGreaterEqual(os.path.getsize(meta["stderr_log"]), 50000)

    def test_small_output_untouched(self):
        rc, out, err, host, meta = self.rbash("echo hi", max_output=2000)
        self.assertEqual(out, "hi\n")
        self.assertEqual(meta["stdout_log"], "")
        self.assertEqual(meta["stderr_log"], "")


class DestructiveGateTests(unittest.TestCase):
    def test_flags_the_obvious(self):
        for cmd in ("rm -rf /home/me", "rm -fr x", "mkfs /dev/sda",
                    "dd if=x of=/dev/sda", "shutdown now", "qdel all"):
            self.assertIsNotNone(rb._looks_destructive(cmd), cmd)

    def test_leaves_normal_build_commands_alone(self):
        for cmd in ("make -j8", "cmake --build build", "rm build/CMakeCache.txt",
                    "pip install numpy", "module load apptainer"):
            self.assertIsNone(rb._looks_destructive(cmd), cmd)


class QueueLimitExplainerTests(unittest.TestCase):
    """A per-user queue-limit rejection refers to the ENDPOINT's own PBS block,
    not to anything the agent submitted. Verbatim traceback from the live
    session that got this backwards."""

    REAL = (
        "TaskExecutionFailed: parsl.executors.errors.BadStateException: Executor "
        "GlobusComputeEngine-HighThroughputExecutor failed due to: Error 1:\n"
        "\tFailed to start block 0: Cannot launch job parsl.GlobusComputeEngine-"
        "HighThroughputExecutor.block-0.1787578168.8697793: Submit command "
        "'qsub  -q debug -A datascience /home/parton/.globus_compute/uep.9a947ba5/"
        "submit_scripts/parsl...block-0' failed; recode=None, stdout=, "
        "stderr=qsub: would exceed queue generic's per-user limit of jobs in "
        "'Q' state"
    )

    def test_recognised(self):
        self.assertIsNotNone(rb._explain_launch_failure(self.REAL))

    def test_attributes_the_qsub_to_the_endpoint(self):
        msg = rb._explain_launch_failure(self.REAL)
        self.assertIn("ENDPOINT", msg)
        self.assertIn("not a command you ran", msg)

    def test_names_the_debug_queue_limit_and_the_stale_block_cause(self):
        msg = rb._explain_launch_failure(self.REAL)
        self.assertIn("one running + one queued", msg)
        self.assertIn("timed out", msg)   # the actual root cause
        self.assertIn("still holds your slot", msg)

    def test_routes_cancellation_to_iri_not_qdel(self):
        msg = rb._explain_launch_failure(self.REAL)
        self.assertIn("/compute/cancel/", msg)
        self.assertIn("NOT available from here", msg)  # re: qdel

    def test_tells_the_agent_not_to_retry(self):
        # The looping retry is what burned the session; the guidance must be explicit.
        self.assertIn("do NOT re-run", rb._explain_launch_failure(self.REAL))

    def test_reports_the_state_that_was_exceeded(self):
        self.assertIn("Q (queued)", rb._explain_launch_failure(self.REAL))
        running = self.REAL.replace("'Q' state", "'R' state")
        self.assertIn("R (running)", rb._explain_launch_failure(running))

    def test_per_project_limit_also_recognised(self):
        self.assertIsNotNone(rb._explain_launch_failure(
            "stderr=qsub: would exceed queue prod's per-project limit"))

    def test_unrelated_failures_fall_through_to_generic_advice(self):
        for other in ("TimeoutError: result timeout after 1200s",
                      "SerializationError: cannot serialize function",
                      "EndpointNotFound: no such endpoint",
                      ""):
            self.assertIsNone(rb._explain_launch_failure(other), other)


class PbsClientGuardTests(unittest.TestCase):
    """PBS client commands cannot work on a compute node — remote-bash runs
    INSIDE a PBS job, where there is no scheduler to talk to. Guard must catch
    real invocations without tripping on incidental substrings."""

    def test_flags_pbs_client_commands(self):
        cases = {
            "qsub $HOME/run.pbs": ["qsub"],
            "qstat -u parton": ["qstat"],
            "qdel 12345": ["qdel"],
            "qalter -l walltime=1:00:00 123": ["qalter"],
            "qhold 1": ["qhold"],
            "qrls 1": ["qrls"],
            "qmove debug 1": ["qmove"],
            # after shell separators
            "cd $HOME && qsub run.pbs": ["qsub"],
            "module load x; qstat": ["qstat"],
            "cat f | qsub": ["qsub"],
            "false || qsub run.pbs": ["qsub"],
            "sudo qdel 1": ["qdel"],
            # multiline scripts
            "cd $HOME\nqsub run.pbs": ["qsub"],
            # several at once, deduped + sorted
            "qsub a.pbs && qstat && qsub b.pbs": ["qstat", "qsub"],
        }
        for cmd, expected in cases.items():
            self.assertEqual(rb._pbs_client_refs(cmd), expected, cmd)

    def test_does_not_trip_on_incidental_mentions(self):
        for cmd in (
            # the word inside a quoted string / message
            'echo "run qsub later"',
            "printf 'use qstat to check\\n' > README",
            # substring of a longer token
            "ls $HOME/qsub_scripts/",
            "cat qstat_output.txt",
            "./qsubmit_helper.sh",
            "python qdel_wrapper.py",
            # normal build work
            "cmake --build build -j",
            "module load spack-pe-base cmake && make install",
        ):
            self.assertEqual(rb._pbs_client_refs(cmd), [], cmd)

    def test_guard_message_names_the_three_machines_and_iri(self):
        msg = rb._pbs_guard_message("qsub run.pbs", ["qsub"])
        self.assertIn("REFUSED", msg)
        self.assertIn("compute node", msg)
        self.assertIn("agent container", msg)
        self.assertIn("PBS scheduler", msg)
        self.assertIn("/compute/job/", msg)      # IRI submit route
        self.assertIn("/compute/cancel/", msg)   # IRI cancel route
        self.assertIn("no override flag", msg.lower())


class ContainerPathGuardTests(unittest.TestCase):
    def test_flags_container_only_paths(self):
        for cmd in ("cd /opt/data/pepper && make",
                    "cp -r /opt/data/pepper_repo $HOME/",
                    "source /opt/data/agent-in-a-box/setup_env.sh",
                    "/opt/hermes/.venv/bin/python x.py",
                    "cat /opt/alcf/docs/iri-api.md"):
            self.assertTrue(rb._container_path_refs(cmd), cmd)

    def test_leaves_cluster_paths_alone(self):
        for cmd in ("cd $HOME/pepper && make",
                    "ls /home/parton /eagle/datascience",
                    "module load spack-pe-base cmake",
                    "git clone https://gitlab.com/spice-mc/pepper"):
            self.assertEqual(rb._container_path_refs(cmd), [], cmd)


class ProxyInjectionTests(_FakeHomeMixin):
    def setUp(self):
        super().setUp()
        # Scrub inherited proxy vars so we observe the wrapper's own injection.
        self._saved_proxy = {}
        for k in ("http_proxy", "https_proxy", "HTTP_PROXY", "HTTPS_PROXY"):
            if k in os.environ:
                self._saved_proxy[k] = os.environ.pop(k)

    def tearDown(self):
        os.environ.update(self._saved_proxy)
        super().tearDown()

    def test_proxy_exported_by_default(self):
        rc, out, *_ = self.rbash('echo "$http_proxy | $https_proxy"')
        self.assertEqual(rc, 0)
        self.assertEqual(
            out.strip(),
            "http://proxy.alcf.anl.gov:3128 | http://proxy.alcf.anl.gov:3128")

    def test_preset_proxy_wins(self):
        rc, out, *_ = self.rbash(
            'http_proxy=http://mine:1 bash -c \'echo "$http_proxy"\'')
        self.assertEqual(out.strip(), "http://mine:1")

    def test_opt_out(self):
        rc, out, *_ = rb.remote_bash('echo "${http_proxy:-unset}"', "$HOME", "",
                                     "noproxy", False, 20000, proxy=False)
        self.assertEqual(out.strip(), "unset")


class McpServerTests(_FakeHomeMixin):
    """Round-trip the MCP stdio protocol against the real server subprocess in
    local-exec mode (same remote_bash, no Globus)."""

    def setUp(self):
        super().setUp()
        env = dict(os.environ)
        env["HOME"] = self.home
        env["ALCF_BASH_LOCAL_TEST"] = "1"
        env["ALCF_BASH_SESSION"] = "mcps"
        self.proc = subprocess.Popen(
            [sys.executable, os.path.join(SCRIPTS, "alcf_bash_mcp.py")],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE,
            stderr=subprocess.PIPE, text=True, env=env)
        self._id = 0

    def tearDown(self):
        try:
            self.proc.stdin.close()
            self.proc.wait(timeout=10)
        except Exception:
            self.proc.kill()
        finally:
            for pipe in (self.proc.stdout, self.proc.stderr):
                try:
                    pipe.close()
                except Exception:
                    pass
        super().tearDown()

    def _send(self, method, params=None, notify=False):
        msg = {"jsonrpc": "2.0", "method": method}
        if params is not None:
            msg["params"] = params
        if not notify:
            self._id += 1
            msg["id"] = self._id
        self.proc.stdin.write(json.dumps(msg) + "\n")
        self.proc.stdin.flush()
        if notify:
            return None
        r, _, _ = select.select([self.proc.stdout], [], [], 60)
        self.assertTrue(r, f"timeout waiting for response to {method}")
        line = self.proc.stdout.readline()
        self.assertTrue(line, f"server closed stdout during {method}")
        resp = json.loads(line)
        self.assertEqual(resp.get("id"), self._id)
        return resp

    def _call_bash(self, arguments):
        resp = self._send("tools/call", {"name": "bash", "arguments": arguments})
        self.assertIn("result", resp, resp)
        result = resp["result"]
        text = result["content"][0]["text"]
        return result.get("isError", False), text

    def _handshake(self):
        resp = self._send("initialize", {
            "protocolVersion": "2025-03-26",
            "capabilities": {}, "clientInfo": {"name": "test", "version": "0"}})
        self.assertEqual(resp["result"]["serverInfo"]["name"], "alcf-bash")
        self.assertEqual(resp["result"]["protocolVersion"], "2025-03-26")
        self._send("notifications/initialized", notify=True)

    def test_full_round_trip(self):
        self._handshake()

        resp = self._send("tools/list")
        tools = resp["result"]["tools"]
        self.assertEqual(len(tools), 1)
        self.assertEqual(tools[0]["name"], "bash")
        self.assertEqual(tools[0]["inputSchema"]["required"], ["command"])

        is_err, text = self._call_bash({"command": "export Z=9 && echo ready"})
        self.assertFalse(is_err, text)
        self.assertIn("ready", text)
        self.assertIn("[exit 0", text)

        # state persists across MCP tool calls (the whole point)
        is_err, text = self._call_bash({"command": 'echo "Z=$Z"'})
        self.assertFalse(is_err, text)
        self.assertIn("Z=9", text)

        # non-zero exit is a normal (non-error) result with the code visible
        is_err, text = self._call_bash({"command": "exit 3"})
        self.assertFalse(is_err)
        self.assertIn("[exit 3", text)

        # container-only paths are refused (no override via MCP)
        is_err, text = self._call_bash(
            {"command": "cp -r /opt/data/pepper_repo $HOME/"})
        self.assertTrue(is_err)
        self.assertIn("container-only", text)

        # destructive gate: refused without confirm, runs with it
        is_err, text = self._call_bash(
            {"command": "rm -rf /tmp/alcf_mcp_nonexistent_dir"})
        self.assertTrue(is_err)
        self.assertIn("REFUSED", text)
        is_err, text = self._call_bash(
            {"command": "rm -rf /tmp/alcf_mcp_nonexistent_dir", "confirm": True})
        self.assertFalse(is_err, text)
        self.assertIn("[exit 0", text)

        # PBS client commands are refused, and confirm=true does NOT override
        # (unlike the destructive gate) — there is no correct way to reach the
        # scheduler from a compute node.
        is_err, text = self._call_bash({"command": "qsub $HOME/run_test.pbs"})
        self.assertTrue(is_err)
        self.assertIn("REFUSED", text)
        self.assertIn("qsub", text)
        self.assertIn("/compute/job/", text)  # points at the IRI submit route
        is_err, text = self._call_bash(
            {"command": "qsub $HOME/run_test.pbs", "confirm": True})
        self.assertTrue(is_err, "confirm must NOT bypass the PBS guard")
        self.assertIn("REFUSED", text)

        # ...but a build command that merely mentions qsub in a string is fine
        is_err, text = self._call_bash({"command": 'echo "run qsub later"'})
        self.assertFalse(is_err, text)
        self.assertIn("run qsub later", text)

        # bad requests
        is_err, text = self._call_bash({})
        self.assertTrue(is_err)
        resp = self._send("tools/call", {"name": "nope", "arguments": {}})
        self.assertEqual(resp["error"]["code"], -32602)
        resp = self._send("no/such/method")
        self.assertEqual(resp["error"]["code"], -32601)

        resp = self._send("ping")
        self.assertEqual(resp["result"], {})


if __name__ == "__main__":
    unittest.main(verbosity=2)
