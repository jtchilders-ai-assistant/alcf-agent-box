#!/usr/bin/env python3
"""Tests for scripts/red_shirt_probe.py — runtime readiness probes and
userspace A2A proxy routing.

Design: docs/superpowers/specs/2026-09-16-red-shirt-polaris-design.md
Plan:   docs/superpowers/plans/2026-09-16-red-shirt-polaris.md (Task 4)

All tests use REAL localhost sockets (http.server fake targets, a real
forwarding HTTP proxy) — no static regex-only assertions on wire behaviour.

Run: pytest -q tests/test_red_shirt_runtime.py
"""
from __future__ import annotations

import http.server
import json
import os
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
SCRIPTS = REPO / "scripts"
PROBE = SCRIPTS / "red_shirt_probe.py"


def run_cli(args: list, env: dict | None = None) -> subprocess.CompletedProcess:
    full_env = os.environ.copy()
    if env:
        full_env.update(env)
    return subprocess.run(
        [sys.executable, str(PROBE)] + args,
        capture_output=True, text=True, timeout=30, env=full_env,
    )


def _write_token(tmp_path: Path, token: str, name: str = "token") -> Path:
    p = tmp_path / name
    p.write_text(token, encoding="utf-8")
    p.chmod(0o600)
    return p


def _free_addr(srv: http.server.HTTPServer) -> str:
    host, port = srv.server_address[:2]
    return f"http://127.0.0.1:{port}"


# ---------------------------------------------------------------------------
# Fake target servers (real localhost sockets, real threads)
# ---------------------------------------------------------------------------

class _JSONHandler(http.server.BaseHTTPRequestHandler):
    def log_message(self, *a):  # silence default access log
        pass

    def _send_json(self, code: int, payload: dict) -> None:
        body = json.dumps(payload).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _read_body(self) -> bytes:
        length = int(self.headers.get("Content-Length", 0) or 0)
        return self.rfile.read(length) if length else b""


class FakeCardServer(_JSONHandler):
    """Serves a minimal A2A v1.0 Agent Card at /.well-known/agent-card.json."""

    card = {
        "name": "Red Shirt Polaris (fake)",
        "description": "fake card for probe testing",
        "url": "http://127.0.0.1:0/",
        "version": "1.0.0",
        "supportedInterfaces": [
            {"url": "http://127.0.0.1:0/", "protocolBinding": "JSONRPC", "protocolVersion": "1.0"}
        ],
        "capabilities": {"streaming": False, "pushNotifications": False},
        "skills": [],
    }
    received_paths: list[str] = []

    def do_GET(self):  # noqa: N802
        type(self).received_paths.append(self.path)
        if self.path.rstrip("/").endswith("/.well-known/agent-card.json"):
            self._send_json(200, self.card)
            return
        self._send_json(404, {"error": "not found"})


class FakeA2AServer(_JSONHandler):
    """Authenticated A2A JSON-RPC endpoint: 401 without a valid bearer token.

    Class attributes are configured per-test before starting the server;
    ``received`` accumulates one dict per POST for assertion.
    """

    expected_token: str = ""
    reply_text: str = "hello from wesley"
    received: list[dict] = []

    def _authed(self) -> bool:
        auth = self.headers.get("Authorization", "")
        return bool(self.expected_token) and auth == f"Bearer {self.expected_token}"

    def do_GET(self):  # noqa: N802
        # Not used by these tests, but keep a well-known card path answering
        # so a combined discovery+send probe wouldn't hang.
        self._send_json(404, {"error": "not found"})

    def do_POST(self):  # noqa: N802
        raw = self._read_body()
        try:
            body = json.loads(raw.decode("utf-8"))
        except Exception:
            body = {}
        if not self._authed():
            self._send_json(401, {"jsonrpc": "2.0", "id": body.get("id"),
                                   "error": {"code": -32050, "message": "unauthorized"}})
            return
        params = body.get("params", {}) or {}
        message = params.get("message", {}) or {}
        type(self).received.append({
            "context_id": message.get("contextId"),
            "message_id": message.get("messageId"),
            "text": next((p.get("text") for p in message.get("parts", []) if p.get("text")), ""),
        })
        result_text = self.reply_text
        if result_text == "":
            # simulate an empty/void reply — probe must reject this.
            task = {
                "id": "task-fake", "contextId": message.get("contextId", "ctx"),
                "status": {"state": "TASK_STATE_COMPLETED", "timestamp": "2026-09-16T00:00:00.000Z"},
            }
        else:
            task = {
                "id": "task-fake", "contextId": message.get("contextId", "ctx"),
                "status": {"state": "TASK_STATE_COMPLETED", "timestamp": "2026-09-16T00:00:00.000Z"},
                "artifacts": [{"artifactId": "a1", "parts": [{"text": result_text, "mediaType": "text/plain"}]}],
            }
        self._send_json(200, {"jsonrpc": "2.0", "id": body.get("id"), "result": {"task": task}})


class FakeInferenceServer(_JSONHandler):
    """OpenAI-compatible /chat/completions endpoint."""

    expected_token: str = ""
    content: "str | None" = "pong"
    status: int = 200

    def do_POST(self):  # noqa: N802
        raw = self._read_body()
        try:
            body = json.loads(raw.decode("utf-8"))
        except Exception:
            body = {}
        auth = self.headers.get("Authorization", "")
        if self.expected_token and auth != f"Bearer {self.expected_token}":
            self._send_json(401, {"error": "unauthorized"})
            return
        if self.status != 200:
            self._send_json(self.status, {"error": "boom"})
            return
        self._send_json(200, {
            "id": "cmpl-fake",
            "model": body.get("model"),
            "choices": [{"index": 0, "message": {"role": "assistant", "content": self.content},
                        "finish_reason": "stop"}],
        })


class MalformedJSONHandler(_JSONHandler):
    def do_POST(self):  # noqa: N802
        self._read_body()
        body = b"{not valid json"
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


class ForwardingHTTPProxy(http.server.BaseHTTPRequestHandler):
    """A real HTTP forward proxy for absolute-form requests.

    Ignores the (possibly unresolvable, fake tailnet) requested hostname and
    always forwards to ``backend_addr`` — this simulates Tailscale's
    userspace outbound HTTP proxy resolving a tailnet-only authority that
    the test process's real DNS cannot resolve, while letting the test
    assert exactly which authority the client asked the proxy to reach.
    """

    backend_addr: str = ""  # "127.0.0.1:PORT" — set per test
    received_authorities: list[str] = []

    def log_message(self, *a):
        pass

    def _forward(self, method: str) -> None:
        parsed = urllib.parse.urlsplit(self.path)
        authority = parsed.netloc
        type(self).received_authorities.append(authority)
        length = int(self.headers.get("Content-Length", 0) or 0)
        body = self.rfile.read(length) if length else b""
        target = f"http://{self.backend_addr}{parsed.path}"
        if parsed.query:
            target += f"?{parsed.query}"
        headers = {k: v for k, v in self.headers.items() if k.lower() not in ("host", "content-length")}
        req = urllib.request.Request(target, data=body or None, method=method, headers=headers)
        try:
            with urllib.request.urlopen(req, timeout=10) as resp:
                data = resp.read()
                self.send_response(resp.status)
                for k, v in resp.getheaders():
                    if k.lower() not in ("transfer-encoding",):
                        self.send_header(k, v)
                self.end_headers()
                self.wfile.write(data)
        except urllib.error.HTTPError as e:
            data = e.read()
            self.send_response(e.code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

    def do_GET(self):  # noqa: N802
        self._forward("GET")

    def do_POST(self):  # noqa: N802
        self._forward("POST")


def _start(handler_cls) -> http.server.HTTPServer:
    srv = http.server.HTTPServer(("127.0.0.1", 0), handler_cls)
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    return srv


@pytest.fixture
def card_server():
    FakeCardServer.received_paths = []
    srv = _start(FakeCardServer)
    yield srv
    srv.shutdown()


@pytest.fixture
def a2a_server():
    FakeA2AServer.received = []
    FakeA2AServer.reply_text = "hello from wesley"
    FakeA2AServer.expected_token = "wesley-inbound-token-1234567890"
    srv = _start(FakeA2AServer)
    yield srv
    srv.shutdown()


@pytest.fixture
def inference_server():
    FakeInferenceServer.content = "pong"
    FakeInferenceServer.status = 200
    FakeInferenceServer.expected_token = "inference-access-token-1234567890"
    srv = _start(FakeInferenceServer)
    yield srv
    srv.shutdown()


@pytest.fixture
def forward_proxy():
    ForwardingHTTPProxy.received_authorities = []
    srv = _start(ForwardingHTTPProxy)
    yield srv
    srv.shutdown()


# ---------------------------------------------------------------------------
# File existence / syntax
# ---------------------------------------------------------------------------

def test_probe_script_exists_and_compiles():
    assert PROBE.is_file(), f"missing {PROBE}"
    result = subprocess.run([sys.executable, "-m", "py_compile", str(PROBE)],
                             capture_output=True, text=True)
    assert result.returncode == 0, result.stderr


def test_probe_uses_only_stdlib():
    import ast
    tree = ast.parse(PROBE.read_text(encoding="utf-8"))
    stdlib = {
        "__future__", "argparse", "json", "os", "sys", "time", "uuid",
        "urllib", "http", "tempfile", "pathlib", "typing", "socket",
    }
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                assert alias.name.split(".")[0] in stdlib, alias.name
        elif isinstance(node, ast.ImportFrom):
            top = (node.module or "").split(".")[0]
            assert top in stdlib, node.module


# ---------------------------------------------------------------------------
# probe card — through an explicitly supplied proxy (real forward proxy)
# ---------------------------------------------------------------------------

class TestProbeCard:
    def test_card_probe_via_explicit_proxy_reaches_exact_authority(self, card_server, forward_proxy):
        ForwardingHTTPProxy.backend_addr = f"127.0.0.1:{card_server.server_port}"
        fake_wesley_url = "http://red-shirt-fake-wesley.invalid:9900"
        proxy_url = _free_addr(forward_proxy)

        result = run_cli(["card", "--url", fake_wesley_url, "--proxy", proxy_url])
        assert result.returncode == 0, result.stderr
        payload = json.loads(result.stdout)
        assert payload["ok"] is True
        assert payload["name"] == FakeCardServer.card["name"]

        # The proxy must have seen the EXACT fake authority requested — proof
        # that routing went through the proxy for that authority, not a
        # substituted/rewritten host.
        assert any("red-shirt-fake-wesley.invalid:9900" in a
                   for a in ForwardingHTTPProxy.received_authorities), \
            ForwardingHTTPProxy.received_authorities

    def test_card_probe_without_proxy_direct(self, card_server):
        url = _free_addr(card_server)
        result = run_cli(["card", "--url", url])
        assert result.returncode == 0, result.stderr
        payload = json.loads(result.stdout)
        assert payload["ok"] is True

    def test_card_probe_ignores_environment_proxy_when_not_supplied(self, card_server):
        """Fail-closed on proxy bypass: an ambient HTTP_PROXY env var must
        NOT silently redirect this call when --proxy is not given."""
        url = _free_addr(card_server)
        # Point env proxy at a port nothing listens on; if the probe honored
        # ambient env it would fail to connect (or hang/err) instead of
        # reaching the real card server directly.
        bogus_proxy = "http://127.0.0.1:1"
        result = run_cli(["card", "--url", url],
                         env={"http_proxy": bogus_proxy, "HTTP_PROXY": bogus_proxy})
        assert result.returncode == 0, result.stderr
        payload = json.loads(result.stdout)
        assert payload["ok"] is True

    def test_card_probe_unreachable_host_fails_closed(self):
        result = run_cli(["card", "--url", "http://127.0.0.1:1"])
        assert result.returncode != 0
        payload = json.loads(result.stdout)
        assert payload["ok"] is False


# ---------------------------------------------------------------------------
# probe a2a-negative — unauthenticated call must get HTTP 401
# ---------------------------------------------------------------------------

class TestProbeA2ANegative:
    def test_unauthenticated_request_returns_401(self, a2a_server):
        url = _free_addr(a2a_server)
        result = run_cli(["a2a-negative", "--url", url])
        assert result.returncode == 0, result.stderr
        payload = json.loads(result.stdout)
        assert payload["ok"] is True

    def test_negative_control_fails_if_server_allows_unauthenticated_access(self):
        class OpenHandler(_JSONHandler):
            def do_POST(self):  # noqa: N802
                self._read_body()
                self._send_json(200, {"jsonrpc": "2.0", "id": "1", "result": {"message": {"parts": []}}})

        srv = _start(OpenHandler)
        try:
            result = run_cli(["a2a-negative", "--url", _free_addr(srv)])
            assert result.returncode != 0
            payload = json.loads(result.stdout)
            assert payload["ok"] is False
        finally:
            srv.shutdown()


# ---------------------------------------------------------------------------
# probe a2a-send — authenticated SendMessage with generated ids
# ---------------------------------------------------------------------------

class TestProbeA2ASend:
    def test_authenticated_send_parses_nonempty_reply(self, a2a_server, tmp_path):
        token_file = _write_token(tmp_path, FakeA2AServer.expected_token)
        url = _free_addr(a2a_server)
        result = run_cli([
            "a2a-send", "--url", url, "--token-file", str(token_file),
            "--message", "ping from red shirt polaris",
        ])
        assert result.returncode == 0, result.stderr
        payload = json.loads(result.stdout)
        assert payload["ok"] is True
        assert payload["reply"] == "hello from wesley"
        assert FakeA2AServer.expected_token not in result.stdout
        assert FakeA2AServer.expected_token not in result.stderr

    def test_generated_ids_are_unique_per_call(self, a2a_server, tmp_path):
        token_file = _write_token(tmp_path, FakeA2AServer.expected_token)
        url = _free_addr(a2a_server)
        for _ in range(2):
            result = run_cli([
                "a2a-send", "--url", url, "--token-file", str(token_file),
                "--message", "ping",
            ])
            assert result.returncode == 0, result.stderr
        assert len(FakeA2AServer.received) == 2
        ctx_ids = {r["context_id"] for r in FakeA2AServer.received}
        msg_ids = {r["message_id"] for r in FakeA2AServer.received}
        assert len(ctx_ids) == 2, "contextId must be freshly generated per call"
        assert len(msg_ids) == 2, "messageId must be freshly generated per call"

    def test_wrong_token_rejected(self, a2a_server, tmp_path):
        token_file = _write_token(tmp_path, "definitely-the-wrong-token-value")
        url = _free_addr(a2a_server)
        result = run_cli([
            "a2a-send", "--url", url, "--token-file", str(token_file),
            "--message", "ping",
        ])
        assert result.returncode != 0
        payload = json.loads(result.stdout)
        assert payload["ok"] is False

    def test_empty_reply_rejected(self, a2a_server, tmp_path):
        FakeA2AServer.reply_text = ""
        token_file = _write_token(tmp_path, FakeA2AServer.expected_token)
        url = _free_addr(a2a_server)
        result = run_cli([
            "a2a-send", "--url", url, "--token-file", str(token_file),
            "--message", "ping",
        ])
        assert result.returncode != 0
        payload = json.loads(result.stdout)
        assert payload["ok"] is False

    def test_send_via_explicit_proxy_reaches_exact_wesley_authority(self, a2a_server, forward_proxy, tmp_path):
        ForwardingHTTPProxy.backend_addr = f"127.0.0.1:{a2a_server.server_port}"
        token_file = _write_token(tmp_path, FakeA2AServer.expected_token)
        fake_wesley_url = "http://red-shirt-fake-wesley.invalid:9900"
        proxy_url = _free_addr(forward_proxy)

        result = run_cli([
            "a2a-send", "--url", fake_wesley_url, "--token-file", str(token_file),
            "--message", "ping", "--proxy", proxy_url,
        ])
        assert result.returncode == 0, result.stderr
        assert any("red-shirt-fake-wesley.invalid:9900" in a
                   for a in ForwardingHTTPProxy.received_authorities)

    def test_token_never_appears_in_argv(self, a2a_server, tmp_path):
        """The bearer token must be read from a file, never passed as a CLI arg."""
        token_file = _write_token(tmp_path, FakeA2AServer.expected_token)
        url = _free_addr(a2a_server)
        args = ["a2a-send", "--url", url, "--token-file", str(token_file), "--message", "ping"]
        assert FakeA2AServer.expected_token not in " ".join(args)
        result = run_cli(args)
        assert result.returncode == 0, result.stderr


# ---------------------------------------------------------------------------
# probe inference — reject HTTP 200 with null/empty content
# ---------------------------------------------------------------------------

class TestProbeInference:
    def test_nonempty_content_accepted(self, inference_server, tmp_path):
        token_file = _write_token(tmp_path, FakeInferenceServer.expected_token)
        result = run_cli([
            "inference", "--base-url", _free_addr(inference_server),
            "--model", "argonne/AuroraGPT-IT-v4-0125", "--token-file", str(token_file),
        ])
        assert result.returncode == 0, result.stderr
        payload = json.loads(result.stdout)
        assert payload["ok"] is True

    def test_null_content_rejected(self, inference_server, tmp_path):
        FakeInferenceServer.content = None
        token_file = _write_token(tmp_path, FakeInferenceServer.expected_token)
        result = run_cli([
            "inference", "--base-url", _free_addr(inference_server),
            "--model", "argonne/AuroraGPT-IT-v4-0125", "--token-file", str(token_file),
        ])
        assert result.returncode != 0
        payload = json.loads(result.stdout)
        assert payload["ok"] is False

    def test_empty_string_content_rejected(self, inference_server, tmp_path):
        FakeInferenceServer.content = ""
        token_file = _write_token(tmp_path, FakeInferenceServer.expected_token)
        result = run_cli([
            "inference", "--base-url", _free_addr(inference_server),
            "--model", "argonne/AuroraGPT-IT-v4-0125", "--token-file", str(token_file),
        ])
        assert result.returncode != 0
        payload = json.loads(result.stdout)
        assert payload["ok"] is False

    def test_non_200_status_rejected(self, inference_server, tmp_path):
        FakeInferenceServer.status = 503
        token_file = _write_token(tmp_path, FakeInferenceServer.expected_token)
        result = run_cli([
            "inference", "--base-url", _free_addr(inference_server),
            "--model", "argonne/AuroraGPT-IT-v4-0125", "--token-file", str(token_file),
        ])
        assert result.returncode != 0
        payload = json.loads(result.stdout)
        assert payload["ok"] is False

    def test_malformed_json_rejected(self, tmp_path):
        srv = _start(MalformedJSONHandler)
        try:
            token_file = _write_token(tmp_path, "whatever-token-value-1234567890")
            result = run_cli([
                "inference", "--base-url", _free_addr(srv),
                "--model", "m", "--token-file", str(token_file),
            ])
            assert result.returncode != 0
            payload = json.loads(result.stdout)
            assert payload["ok"] is False
        finally:
            srv.shutdown()

    def test_missing_token_file_fails_closed(self, inference_server, tmp_path):
        result = run_cli([
            "inference", "--base-url", _free_addr(inference_server),
            "--model", "m", "--token-file", str(tmp_path / "does-not-exist"),
        ])
        assert result.returncode != 0

    def test_token_not_in_stdout_or_stderr(self, inference_server, tmp_path):
        token_file = _write_token(tmp_path, FakeInferenceServer.expected_token)
        result = run_cli([
            "inference", "--base-url", _free_addr(inference_server),
            "--model", "argonne/AuroraGPT-IT-v4-0125", "--token-file", str(token_file),
        ])
        assert FakeInferenceServer.expected_token not in result.stdout
        assert FakeInferenceServer.expected_token not in result.stderr


# ---------------------------------------------------------------------------
# probe ready-record — aggregate JSON record, no secrets, proxy split
# ---------------------------------------------------------------------------

class TestReadyRecord:
    def _base_args(self, tmp_path, card_url, a2a_neg_url, a2a_send_url,
                    a2a_token_file, inference_url, inference_token_file,
                    output, wesley_proxy=None) -> list:
        args = [
            "ready-record", "--output", str(output),
            "--card-url", card_url,
            "--a2a-negative-url", a2a_neg_url,
            "--a2a-send-url", a2a_send_url,
            "--a2a-token-file", str(a2a_token_file),
            "--a2a-message", "readiness ping",
            "--inference-base-url", inference_url,
            "--inference-model", "argonne/AuroraGPT-IT-v4-0125",
            "--inference-token-file", str(inference_token_file),
        ]
        if wesley_proxy:
            args += ["--card-proxy", wesley_proxy, "--a2a-proxy", wesley_proxy]
        return args

    def test_ready_record_all_ok_true_and_no_token_leak(
        self, card_server, a2a_server, inference_server, tmp_path
    ):
        a2a_token_file = _write_token(tmp_path, FakeA2AServer.expected_token, "a2a.token")
        inference_token_file = _write_token(tmp_path, FakeInferenceServer.expected_token, "inference.token")
        output = tmp_path / "ready.json"

        args = self._base_args(
            tmp_path,
            card_url=_free_addr(card_server),
            a2a_neg_url=_free_addr(a2a_server),
            a2a_send_url=_free_addr(a2a_server),
            a2a_token_file=a2a_token_file,
            inference_url=_free_addr(inference_server),
            inference_token_file=inference_token_file,
            output=output,
        )
        result = run_cli(args)
        assert result.returncode == 0, result.stderr
        assert FakeA2AServer.expected_token not in result.stdout
        assert FakeA2AServer.expected_token not in result.stderr
        assert FakeInferenceServer.expected_token not in result.stdout
        assert FakeInferenceServer.expected_token not in result.stderr

        payload = json.loads(result.stdout)
        assert payload["overall_ok"] is True

        on_disk = json.loads(output.read_text())
        assert on_disk["overall_ok"] is True
        assert FakeA2AServer.expected_token not in output.read_text()
        assert FakeInferenceServer.expected_token not in output.read_text()

    def test_ready_record_false_when_inference_empty(
        self, card_server, a2a_server, inference_server, tmp_path
    ):
        FakeInferenceServer.content = ""
        a2a_token_file = _write_token(tmp_path, FakeA2AServer.expected_token, "a2a.token")
        inference_token_file = _write_token(tmp_path, FakeInferenceServer.expected_token, "inference.token")
        output = tmp_path / "ready.json"

        args = self._base_args(
            tmp_path,
            card_url=_free_addr(card_server),
            a2a_neg_url=_free_addr(a2a_server),
            a2a_send_url=_free_addr(a2a_server),
            a2a_token_file=a2a_token_file,
            inference_url=_free_addr(inference_server),
            inference_token_file=inference_token_file,
            output=output,
        )
        result = run_cli(args)
        assert result.returncode != 0
        payload = json.loads(result.stdout)
        assert payload["overall_ok"] is False
        steps = {r["step"]: r["ok"] for r in payload["results"]}
        assert steps["inference"] is False
        assert steps["card"] is True
        assert steps["a2a_negative"] is True
        assert steps["a2a_send"] is True

    def test_ready_record_proxy_routes_only_wesley_calls(
        self, card_server, a2a_server, inference_server, forward_proxy, tmp_path
    ):
        """card + a2a-send go through the Tailscale-style forward proxy for
        the exact Wesley authority; inference is never routed through it."""
        ForwardingHTTPProxy.backend_addr = f"127.0.0.1:{card_server.server_port}"
        a2a_token_file = _write_token(tmp_path, FakeA2AServer.expected_token, "a2a.token")
        inference_token_file = _write_token(tmp_path, FakeInferenceServer.expected_token, "inference.token")
        output = tmp_path / "ready.json"
        fake_wesley = "http://red-shirt-fake-wesley.invalid:9900"

        # Route only the card check (proxy backend is the card server here);
        # a2a-send targets the real a2a_server directly (no proxy) so both
        # proxied and non-proxied calls are exercised together.
        args = [
            "ready-record", "--output", str(output),
            "--card-url", fake_wesley, "--card-proxy", _free_addr(forward_proxy),
            "--a2a-negative-url", _free_addr(a2a_server),
            "--a2a-send-url", _free_addr(a2a_server),
            "--a2a-token-file", str(a2a_token_file),
            "--a2a-message", "readiness ping",
            "--inference-base-url", _free_addr(inference_server),
            "--inference-model", "argonne/AuroraGPT-IT-v4-0125",
            "--inference-token-file", str(inference_token_file),
        ]
        result = run_cli(args)
        assert result.returncode == 0, result.stderr
        assert any("red-shirt-fake-wesley.invalid:9900" in a
                   for a in ForwardingHTTPProxy.received_authorities)
        # Inference must never appear as a requested authority on the
        # Tailscale-style forward proxy — it stays off that path entirely.
        assert not any(
            str(inference_server.server_port) in a
            for a in ForwardingHTTPProxy.received_authorities
        )


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-v"]))
