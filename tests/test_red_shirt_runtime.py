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

import hashlib
import http.server
import json
import os
import re
import shlex
import signal
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


class TokenReflectingA2AServer(_JSONHandler):
    """A malicious/buggy A2A peer that echoes the received Authorization
    header verbatim inside a JSON-RPC error message.

    Simulates the round-3 review finding: an authenticated request's
    bearer token must never be reflected into our own stdout/stderr/JSON
    output just because a remote peer chose to put it in free-text error
    content we did not sanitize.
    """

    def do_POST(self):  # noqa: N802
        raw = self._read_body()
        try:
            body = json.loads(raw.decode("utf-8"))
        except Exception:
            body = {}
        auth = self.headers.get("Authorization", "")
        self._send_json(200, {
            "jsonrpc": "2.0", "id": body.get("id"),
            "error": {"code": -32000, "message": f"forbidden: saw header {auth!r}"},
        })


class TokenReflectingSuccessA2AServer(_JSONHandler):
    """A malicious/buggy A2A peer that echoes the received Authorization
    header verbatim inside a *successful* JSON-RPC artifact/status reply.

    Simulates the round-4 review finding: it is not only JSON-RPC
    ``error.message`` that is untrusted peer-controlled free text — the
    successful artifact/status ``text`` field is exactly as untrusted, and
    a peer can reflect the bearer token there just as easily.
    """

    def do_POST(self):  # noqa: N802
        raw = self._read_body()
        try:
            body = json.loads(raw.decode("utf-8"))
        except Exception:
            body = {}
        auth = self.headers.get("Authorization", "")
        params = body.get("params", {}) or {}
        message = params.get("message", {}) or {}
        task = {
            "id": "task-fake", "contextId": message.get("contextId", "ctx"),
            "status": {"state": "TASK_STATE_COMPLETED", "timestamp": "2026-09-16T00:00:00.000Z"},
            "artifacts": [{"artifactId": "a1", "parts": [{"text": auth, "mediaType": "text/plain"}]}],
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


class _ForwardingProxyBase(http.server.BaseHTTPRequestHandler):
    """A real HTTP forward proxy for absolute-form requests.

    Ignores the (possibly unresolvable, fake tailnet) requested hostname and
    always forwards to ``backend_addr`` — this simulates a userspace
    outbound HTTP proxy resolving an authority that the test process's real
    DNS cannot resolve, while letting the test assert exactly which
    authority the client asked the proxy to reach.

    Subclassed (not shared) per logical proxy so that two independently
    running fake proxies in the same test (e.g. a Tailscale-style proxy and
    a distinct ALCF-style proxy) each keep their own ``backend_addr`` /
    ``received_authorities`` class state instead of clobbering each other.
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


class ForwardingHTTPProxy(_ForwardingProxyBase):
    """Simulates the Tailscale userspace outbound HTTP proxy used for
    authenticated Wesley A2A traffic."""

    backend_addr: str = ""
    received_authorities: list[str] = []


class AlcfForwardHTTPProxy(_ForwardingProxyBase):
    """Simulates the distinct ALCF forward proxy used for inference.

    A separate class (own ``backend_addr``/``received_authorities``) from
    ``ForwardingHTTPProxy`` so a test can run both simultaneously and prove
    each carries only its own traffic.
    """

    backend_addr: str = ""
    received_authorities: list[str] = []


class RedirectingHTTPProxy(http.server.BaseHTTPRequestHandler):
    """A real HTTP forward proxy that, instead of forwarding, answers every
    absolute-form request with an HTTP 302 pointing at ``redirect_target``
    (a caller-controlled, typically off-authority, URL).

    Simulates a compromised/misbehaving Tailscale-side peer trying to smuggle
    a follow-up request to an arbitrary authority via a redirect — the exact
    scenario the round-2 review finding demonstrated urllib's default
    redirect handling did not block.
    """

    redirect_target: str = ""
    received_authorities: list[str] = []

    def log_message(self, *a):
        pass

    def _redirect(self) -> None:
        parsed = urllib.parse.urlsplit(self.path)
        type(self).received_authorities.append(parsed.netloc)
        self.send_response(302)
        self.send_header("Location", self.redirect_target)
        self.send_header("Content-Length", "0")
        self.end_headers()

    def do_GET(self):  # noqa: N802
        self._redirect()

    def do_POST(self):  # noqa: N802
        self._redirect()


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


@pytest.fixture
def alcf_forward_proxy():
    """A second, independent fake forward proxy simulating the distinct ALCF
    forward proxy used for inference — separate class/state from
    ``forward_proxy`` (the Tailscale-style proxy) so a test can prove
    simultaneous, non-overlapping routing."""
    AlcfForwardHTTPProxy.received_authorities = []
    srv = _start(AlcfForwardHTTPProxy)
    yield srv
    srv.shutdown()


@pytest.fixture
def redirecting_proxy():
    """A fake forward proxy that answers every request with a 302 to an
    attacker-controlled Location — used to prove redirect hops cannot
    smuggle a request to an off-authority target."""
    RedirectingHTTPProxy.received_authorities = []
    srv = _start(RedirectingHTTPProxy)
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

        result = run_cli(["card", "--url", fake_wesley_url, "--proxy", proxy_url,
                          "--proxy-authority", "red-shirt-fake-wesley.invalid:9900"])
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

    def test_card_probe_via_proxy_without_authority_allowlist_rejected(self, card_server, forward_proxy):
        """--proxy without --proxy-authority must fail closed and issue NO
        network call — the Tailscale proxy must never be usable for an
        unconstrained authority."""
        ForwardingHTTPProxy.backend_addr = f"127.0.0.1:{card_server.server_port}"
        fake_wesley_url = "http://red-shirt-fake-wesley.invalid:9900"
        proxy_url = _free_addr(forward_proxy)

        result = run_cli(["card", "--url", fake_wesley_url, "--proxy", proxy_url])
        assert result.returncode != 0
        payload = json.loads(result.stdout)
        assert payload["ok"] is False
        assert ForwardingHTTPProxy.received_authorities == [], \
            "no request should reach the proxy when --proxy-authority is missing"

    def test_card_probe_via_proxy_wrong_authority_rejected(self, card_server, forward_proxy):
        """A caller-supplied URL for a DIFFERENT authority than the exact
        allowlisted --proxy-authority must be rejected before any network
        call — proving the Tailscale proxy can't be used to reach an
        arbitrary authority."""
        ForwardingHTTPProxy.backend_addr = f"127.0.0.1:{card_server.server_port}"
        arbitrary_url = "http://some-other-arbitrary-host.invalid:12345"
        proxy_url = _free_addr(forward_proxy)

        result = run_cli(["card", "--url", arbitrary_url, "--proxy", proxy_url,
                          "--proxy-authority", "red-shirt-fake-wesley.invalid:9900"])
        assert result.returncode != 0
        payload = json.loads(result.stdout)
        assert payload["ok"] is False
        assert ForwardingHTTPProxy.received_authorities == [], \
            "no request should reach the proxy for a non-allowlisted authority"

    def test_card_probe_rejects_off_authority_redirect(self, redirecting_proxy):
        """Round-2 review finding: the initial-URL authority check alone is
        not enough — a peer reachable via the allowlisted authority can
        respond with a 3xx redirecting to an arbitrary authority, and
        urllib's default redirect handling would follow it unconditionally,
        smuggling the SECOND request through the same Tailscale proxy to an
        authority never allowlisted. Reproduces the reviewer's exact
        exploit shape (a proxy answering the allowlisted request with a
        redirect to an off-authority target) and requires it to fail closed
        with the second request never issued.
        """
        fake_wesley_url = "http://red-shirt-fake-wesley.invalid:9900"
        RedirectingHTTPProxy.redirect_target = "http://arbitrary.invalid:1234/redirected"
        proxy_url = _free_addr(redirecting_proxy)

        result = run_cli(["card", "--url", fake_wesley_url, "--proxy", proxy_url,
                          "--proxy-authority", "red-shirt-fake-wesley.invalid:9900"])
        assert result.returncode != 0, result.stdout
        payload = json.loads(result.stdout)
        assert payload["ok"] is False
        # Exactly the one (allowlisted) request may reach the proxy; the
        # redirect must never be followed into a second request.
        assert RedirectingHTTPProxy.received_authorities == \
            ["red-shirt-fake-wesley.invalid:9900"], \
            RedirectingHTTPProxy.received_authorities

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
            "--proxy-authority", "red-shirt-fake-wesley.invalid:9900",
        ])
        assert result.returncode == 0, result.stderr
        assert any("red-shirt-fake-wesley.invalid:9900" in a
                   for a in ForwardingHTTPProxy.received_authorities)

    def test_send_via_proxy_wrong_authority_rejected_no_network_call(self, a2a_server, forward_proxy, tmp_path):
        ForwardingHTTPProxy.backend_addr = f"127.0.0.1:{a2a_server.server_port}"
        token_file = _write_token(tmp_path, FakeA2AServer.expected_token)
        arbitrary_url = "http://some-other-arbitrary-host.invalid:12345"
        proxy_url = _free_addr(forward_proxy)

        result = run_cli([
            "a2a-send", "--url", arbitrary_url, "--token-file", str(token_file),
            "--message", "ping", "--proxy", proxy_url,
            "--proxy-authority", "red-shirt-fake-wesley.invalid:9900",
        ])
        assert result.returncode != 0
        payload = json.loads(result.stdout)
        assert payload["ok"] is False
        assert ForwardingHTTPProxy.received_authorities == []

    def test_send_rejects_off_authority_redirect(self, redirecting_proxy, tmp_path):
        """Same round-2 finding as the card probe, applied to the
        authenticated A2A POST path: a redirect off the allowlisted
        authority must be rejected before a second request is issued,
        even though the bearer token is present."""
        token_file = _write_token(tmp_path, "wesley-inbound-token-1234567890")
        fake_wesley_url = "http://red-shirt-fake-wesley.invalid:9900"
        RedirectingHTTPProxy.redirect_target = "http://arbitrary.invalid:1234/redirected"
        proxy_url = _free_addr(redirecting_proxy)

        result = run_cli([
            "a2a-send", "--url", fake_wesley_url, "--token-file", str(token_file),
            "--message", "ping", "--proxy", proxy_url,
            "--proxy-authority", "red-shirt-fake-wesley.invalid:9900",
        ])
        assert result.returncode != 0, result.stdout
        payload = json.loads(result.stdout)
        assert payload["ok"] is False
        assert RedirectingHTTPProxy.received_authorities == \
            ["red-shirt-fake-wesley.invalid:9900"], \
            RedirectingHTTPProxy.received_authorities

    def test_token_never_appears_in_argv(self, a2a_server, tmp_path):
        """The bearer token must be read from a file, never passed as a CLI arg."""
        token_file = _write_token(tmp_path, FakeA2AServer.expected_token)
        url = _free_addr(a2a_server)
        args = ["a2a-send", "--url", url, "--token-file", str(token_file), "--message", "ping"]
        assert FakeA2AServer.expected_token not in " ".join(args)
        result = run_cli(args)
        assert result.returncode == 0, result.stderr

    def test_reflected_token_in_peer_error_never_reaches_output(self, tmp_path):
        """Round-3 review finding: a peer's JSON-RPC error.message is
        untrusted free text. If the peer reflects our own Authorization
        header back at us in an error, the sent bearer token must never
        appear in stdout or stderr — it must be redacted before it is
        placed in the result dict."""
        srv = _start(TokenReflectingA2AServer)
        try:
            secret_token = "review-secret-token-1234567890"
            token_file = _write_token(tmp_path, secret_token)
            url = _free_addr(srv)
            result = run_cli([
                "a2a-send", "--url", url, "--token-file", str(token_file),
                "--message", "ping",
            ])
            assert result.returncode != 0
            payload = json.loads(result.stdout)
            assert payload["ok"] is False
            assert secret_token not in result.stdout, result.stdout
            assert secret_token not in result.stderr, result.stderr
            assert secret_token not in json.dumps(payload)
        finally:
            srv.shutdown()

    def test_reflected_token_in_successful_reply_never_reaches_output(self, tmp_path):
        """Round-4 review finding: a peer's *successful* artifact/status
        text is untrusted free text just like an error message. If the
        peer reflects our own Authorization header back at us in a
        successful JSON-RPC result, the sent bearer token must never
        appear in stdout, stderr, or serialized JSON — it must be
        redacted before it is placed in the result dict."""
        srv = _start(TokenReflectingSuccessA2AServer)
        try:
            secret_token = "review-success-reflection-token-987654321"
            token_file = _write_token(tmp_path, secret_token)
            url = _free_addr(srv)
            result = run_cli([
                "a2a-send", "--url", url, "--token-file", str(token_file),
                "--message", "ping",
            ])
            payload = json.loads(result.stdout)
            assert secret_token not in result.stdout, result.stdout
            assert secret_token not in result.stderr, result.stderr
            assert secret_token not in json.dumps(payload)
        finally:
            srv.shutdown()


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

    def test_inference_routes_through_explicit_alcf_proxy(
        self, inference_server, alcf_forward_proxy, tmp_path
    ):
        """--proxy on the inference subcommand must actually be used — this
        is the distinct ALCF forward proxy, never the Tailscale proxy."""
        AlcfForwardHTTPProxy.backend_addr = f"127.0.0.1:{inference_server.server_port}"
        token_file = _write_token(tmp_path, FakeInferenceServer.expected_token)
        fake_alcf_url = "http://red-shirt-fake-alcf.invalid:8000"

        result = run_cli([
            "inference", "--base-url", fake_alcf_url,
            "--model", "argonne/AuroraGPT-IT-v4-0125", "--token-file", str(token_file),
            "--proxy", _free_addr(alcf_forward_proxy),
        ])
        assert result.returncode == 0, result.stderr
        payload = json.loads(result.stdout)
        assert payload["ok"] is True
        assert any("red-shirt-fake-alcf.invalid:8000" in a
                   for a in AlcfForwardHTTPProxy.received_authorities), \
            AlcfForwardHTTPProxy.received_authorities


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
        self, card_server, a2a_server, inference_server, forward_proxy,
        alcf_forward_proxy, tmp_path
    ):
        """card + a2a-send (Wesley, tailnet-only) are routed through the
        Tailscale-style forward proxy for the exact Wesley authority;
        inference is simultaneously routed through a DISTINCT ALCF-style
        forward proxy. Each fake proxy records only its own traffic,
        proving the two paths never cross."""
        ForwardingHTTPProxy.backend_addr = f"127.0.0.1:{a2a_server.server_port}"
        AlcfForwardHTTPProxy.backend_addr = f"127.0.0.1:{inference_server.server_port}"
        a2a_token_file = _write_token(tmp_path, FakeA2AServer.expected_token, "a2a.token")
        inference_token_file = _write_token(tmp_path, FakeInferenceServer.expected_token, "inference.token")
        output = tmp_path / "ready.json"
        fake_wesley = "http://red-shirt-fake-wesley.invalid:9900"
        fake_alcf = "http://red-shirt-fake-alcf.invalid:8000"
        wesley_proxy = _free_addr(forward_proxy)
        alcf_proxy = _free_addr(alcf_forward_proxy)

        args = [
            "ready-record", "--output", str(output),
            "--card-url", _free_addr(card_server),
            "--a2a-negative-url", _free_addr(a2a_server),
            "--a2a-send-url", fake_wesley, "--a2a-proxy", wesley_proxy,
            "--a2a-proxy-authority", "red-shirt-fake-wesley.invalid:9900",
            "--a2a-token-file", str(a2a_token_file),
            "--a2a-message", "readiness ping",
            "--inference-base-url", fake_alcf, "--inference-proxy", alcf_proxy,
            "--inference-model", "argonne/AuroraGPT-IT-v4-0125",
            "--inference-token-file", str(inference_token_file),
        ]
        result = run_cli(args)
        assert result.returncode == 0, result.stderr
        payload = json.loads(result.stdout)
        assert payload["overall_ok"] is True

        # The authenticated A2A send must have gone through the Tailscale
        # proxy for the exact Wesley authority ...
        assert any("red-shirt-fake-wesley.invalid:9900" in a
                   for a in ForwardingHTTPProxy.received_authorities), \
            ForwardingHTTPProxy.received_authorities
        # ... and inference must have gone through the distinct ALCF proxy
        # for the ALCF authority, at the same time ...
        assert any("red-shirt-fake-alcf.invalid:8000" in a
                   for a in AlcfForwardHTTPProxy.received_authorities), \
            AlcfForwardHTTPProxy.received_authorities
        # ... and neither authority ever crossed into the other proxy.
        assert not any("red-shirt-fake-alcf.invalid:8000" in a
                       for a in ForwardingHTTPProxy.received_authorities)
        assert not any("red-shirt-fake-wesley.invalid:9900" in a
                       for a in AlcfForwardHTTPProxy.received_authorities)


# ---------------------------------------------------------------------------
# Task 5: process-supervision entrypoint (scripts/red_shirt_entrypoint.sh)
#
# Design: docs/superpowers/specs/2026-09-16-red-shirt-polaris-design.md
# Plan:   docs/superpowers/plans/2026-09-16-red-shirt-polaris.md (Task 5)
#
# This card is deliberately scoped to process lifecycle only: owned-PID
# tracking (never pgrep/pkill), foreground wait on the Hermes child,
# detection of a required support child dying, TERM/INT forwarding, bounded
# TERM-then-KILL escalation, job-local vs persistent state handling, and
# non-secret JSON evidence records. The ordered network/auth/readiness
# pipeline is explicitly out of scope for this increment.
# ---------------------------------------------------------------------------

ENTRYPOINT = SCRIPTS / "red_shirt_entrypoint.sh"


def _write_exec_script(path: Path, body: str) -> Path:
    path.write_text(body, encoding="utf-8")
    path.chmod(0o700)
    return path


def _run_entrypoint(env_overrides: dict, timeout: float = 20) -> subprocess.CompletedProcess:
    full_env = os.environ.copy()
    full_env.update(env_overrides)
    return subprocess.run(
        ["bash", str(ENTRYPOINT)],
        capture_output=True, text=True, timeout=timeout, env=full_env,
    )


def _start_entrypoint(env_overrides: dict) -> subprocess.Popen:
    full_env = os.environ.copy()
    full_env.update(env_overrides)
    return subprocess.Popen(
        ["bash", str(ENTRYPOINT)],
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, env=full_env,
    )


class TestEntrypointStatic:
    def test_entrypoint_exists_and_syntax_ok(self):
        assert ENTRYPOINT.is_file(), f"missing {ENTRYPOINT}"
        result = subprocess.run(["bash", "-n", str(ENTRYPOINT)],
                                 capture_output=True, text=True)
        assert result.returncode == 0, result.stderr

    def test_entrypoint_uses_strict_mode_and_umask(self):
        text = ENTRYPOINT.read_text(encoding="utf-8")
        assert "set -euo pipefail" in text
        assert "umask 077" in text

    def test_entrypoint_never_uses_pgrep_or_pkill(self):
        text = ENTRYPOINT.read_text(encoding="utf-8")
        assert "pgrep" not in text
        assert "pkill" not in text

    def test_entrypoint_does_not_trace_execution(self):
        text = ENTRYPOINT.read_text(encoding="utf-8")
        assert "set -x" not in text


class TestEntrypointLifecycle:
    def test_requires_hermes_cmd_env_var(self, tmp_path):
        job_root = tmp_path / "job"
        result = _run_entrypoint({
            "RED_SHIRT_JOB_ROOT": str(job_root),
            "RED_SHIRT_TEST_MODE": "1",
        })
        assert result.returncode != 0
        assert "RED_SHIRT_HERMES_CMD" in result.stderr

    def test_waits_foreground_and_propagates_hermes_exit_code(self, tmp_path):
        hermes = _write_exec_script(tmp_path / "fake_hermes.sh",
                                     "#!/usr/bin/env bash\nexit 7\n")
        job_root = tmp_path / "job"
        term_out = tmp_path / "terminal.json"
        result = _run_entrypoint({
            "RED_SHIRT_HERMES_CMD": str(hermes),
            "RED_SHIRT_JOB_ROOT": str(job_root),
            "RED_SHIRT_TERMINAL_OUTPUT": str(term_out),
            "RED_SHIRT_TEST_MODE": "1",
        })
        assert result.returncode == 7, result.stderr
        record = json.loads(term_out.read_text())
        assert record["exit_code"] == 7
        assert record["ok"] is False

    def test_removes_job_local_state_but_preserves_persistent_home(self, tmp_path):
        hermes = _write_exec_script(tmp_path / "fake_hermes.sh",
                                     "#!/usr/bin/env bash\nexit 0\n")
        job_root = tmp_path / "job"
        job_root.mkdir()
        (job_root / "ephemeral.marker").write_text("job-local")
        home = tmp_path / "home"
        home.mkdir()
        (home / "persistent.marker").write_text("keep-me")
        result = _run_entrypoint({
            "RED_SHIRT_HERMES_CMD": str(hermes),
            "RED_SHIRT_JOB_ROOT": str(job_root),
            "RED_SHIRT_HOME": str(home),
            "RED_SHIRT_TERMINAL_OUTPUT": str(tmp_path / "terminal.json"),
            "RED_SHIRT_TEST_MODE": "1",
        })
        assert result.returncode == 0, result.stderr
        assert not job_root.exists(), "job-local root must be removed on exit"
        assert (home / "persistent.marker").is_file(), \
            "persistent HERMES_HOME must never be touched"

    def test_writes_ready_record_before_hermes_exits(self, tmp_path):
        hermes = _write_exec_script(
            tmp_path / "fake_hermes.sh",
            "#!/usr/bin/env bash\nsleep 2\nexit 0\n",
        )
        ready_out = tmp_path / "ready.json"
        job_root = tmp_path / "job"
        proc = _start_entrypoint({
            "RED_SHIRT_HERMES_CMD": str(hermes),
            "RED_SHIRT_JOB_ROOT": str(job_root),
            "RED_SHIRT_READY_OUTPUT": str(ready_out),
            "RED_SHIRT_TERMINAL_OUTPUT": str(tmp_path / "terminal.json"),
            "RED_SHIRT_TEST_MODE": "1",
        })
        try:
            deadline = time.time() + 10
            while time.time() < deadline and not ready_out.exists():
                time.sleep(0.1)
            assert ready_out.exists(), "ready record must be written while hermes is still running"
            record = json.loads(ready_out.read_text())
            assert record["ok"] is True
        finally:
            proc.wait(timeout=10)
        assert proc.returncode == 0

    def test_detects_required_support_child_death_and_tears_down(self, tmp_path):
        """A required support child (e.g. the stand-in for tailscaled) dying
        while Hermes is still running must be detected and must terminate
        the whole supervised run rather than leaving an orphaned Hermes."""
        marker = tmp_path / "hermes-still-running.marker"
        hermes = _write_exec_script(
            tmp_path / "fake_hermes.sh",
            f"#!/usr/bin/env bash\n"
            f"trap 'rm -f {shlex.quote(str(marker))}; exit 143' TERM\n"
            f"touch {shlex.quote(str(marker))}\n"
            f"sleep 60 >/dev/null 2>&1 & wait $!\n",
        )
        support = _write_exec_script(
            tmp_path / "fake_support.sh",
            "#!/usr/bin/env bash\nsleep 1\nexit 1\n",
        )
        job_root = tmp_path / "job"
        result = _run_entrypoint({
            "RED_SHIRT_HERMES_CMD": str(hermes),
            "RED_SHIRT_SUPPORT_CMD": str(support),
            "RED_SHIRT_JOB_ROOT": str(job_root),
            "RED_SHIRT_TERM_TIMEOUT": "5",
            "RED_SHIRT_TERMINAL_OUTPUT": str(tmp_path / "terminal.json"),
            "RED_SHIRT_TEST_MODE": "1",
        }, timeout=25)
        assert result.returncode != 0, \
            "a required support child dying must be treated as a failure"
        assert not marker.exists(), \
            "Hermes must have been torn down after the required child died"

    def test_forwards_term_signal_to_owned_children(self, tmp_path):
        marker = tmp_path / "got-term.marker"
        hermes = _write_exec_script(
            tmp_path / "fake_hermes.sh",
            f"#!/usr/bin/env bash\n"
            f"trap 'touch {shlex.quote(str(marker))}; exit 143' TERM\n"
            f"sleep 60 >/dev/null 2>&1 & wait $!\n",
        )
        job_root = tmp_path / "job"
        proc = _start_entrypoint({
            "RED_SHIRT_HERMES_CMD": str(hermes),
            "RED_SHIRT_JOB_ROOT": str(job_root),
            "RED_SHIRT_TERMINAL_OUTPUT": str(tmp_path / "terminal.json"),
            "RED_SHIRT_TEST_MODE": "1",
        })
        try:
            time.sleep(1.0)
            proc.send_signal(signal.SIGTERM)
            proc.wait(timeout=15)
        finally:
            if proc.poll() is None:
                proc.kill()
                proc.wait(timeout=5)
        assert marker.exists(), "owned Hermes child must receive forwarded TERM"

    def test_escalates_to_kill_after_bounded_timeout(self, tmp_path):
        """A child that ignores TERM must be forcibly killed within the
        configured bounded timeout, not left running indefinitely."""
        survived_marker = tmp_path / "survived.marker"
        hermes = _write_exec_script(
            tmp_path / "fake_hermes.sh",
            f"#!/usr/bin/env bash\n"
            f"trap '' TERM\n"
            f"touch {shlex.quote(str(survived_marker))}\n"
            f"sleep 60 >/dev/null 2>&1 & wait $!\n",
        )
        job_root = tmp_path / "job"
        started = time.time()
        proc = _start_entrypoint({
            "RED_SHIRT_HERMES_CMD": str(hermes),
            "RED_SHIRT_JOB_ROOT": str(job_root),
            "RED_SHIRT_TERM_TIMEOUT": "2",
            "RED_SHIRT_TERMINAL_OUTPUT": str(tmp_path / "terminal.json"),
            "RED_SHIRT_TEST_MODE": "1",
        })
        try:
            deadline = time.time() + 10
            while time.time() < deadline and not survived_marker.exists():
                time.sleep(0.05)
            assert survived_marker.exists()
            proc.send_signal(signal.SIGTERM)
            proc.wait(timeout=20)
        finally:
            if proc.poll() is None:
                proc.kill()
                proc.wait(timeout=5)
        elapsed = time.time() - started
        assert elapsed < 15, f"KILL escalation took too long: {elapsed:.1f}s"

    def test_no_secret_values_in_stdout_stderr_or_records(self, tmp_path):
        secret = "unique-ambient-secret-value-12345"
        hermes = _write_exec_script(tmp_path / "fake_hermes.sh",
                                     "#!/usr/bin/env bash\nexit 0\n")
        job_root = tmp_path / "job"
        term_out = tmp_path / "terminal.json"
        ready_out = tmp_path / "ready.json"
        result = _run_entrypoint({
            "RED_SHIRT_HERMES_CMD": str(hermes),
            "RED_SHIRT_JOB_ROOT": str(job_root),
            "RED_SHIRT_TERMINAL_OUTPUT": str(term_out),
            "RED_SHIRT_READY_OUTPUT": str(ready_out),
            "RED_SHIRT_TEST_AMBIENT_SECRET": secret,
            "RED_SHIRT_TEST_MODE": "1",
        })
        assert secret not in result.stdout
        assert secret not in result.stderr
        assert secret not in term_out.read_text()
        assert secret not in ready_out.read_text()


# ---------------------------------------------------------------------------
# Task 7A: production job-root cleanup path guard (hostile paths)
#
# Reviewer-reproduced finding: scripts/red_shirt_entrypoint.sh accepted
# RED_SHIRT_JOB_ROOT verbatim and `rm -rf`'d it, so RED_SHIRT_JOB_ROOT equal
# to persistent HERMES_HOME deleted that home with rc=0. These tests exercise
# the PRODUCTION path (no RED_SHIRT_TEST_MODE) and prove the fail-closed
# guard rejects every hostile input before any child is launched or any path
# is created/chmod/removed, while a genuinely valid root still works end to
# end and only the validated root is removed.
# ---------------------------------------------------------------------------

class TestEntrypointJobRootProductionGuard:
    def test_valid_production_root_succeeds_and_only_root_is_removed(self, tmp_path, monkeypatch):
        """The positive production-guard case must run through the FULL fake
        production environment (not just the job-root guard in isolation) so
        it proves the validated root is removed, the parent directory
        survives, and HERMES_HOME markers are preserved end to end -- not
        merely that some minimal invocation happens to return 0."""
        job_parent = tmp_path / "parent"
        job_parent.mkdir()
        job_root = job_parent / "job-123"
        a2a_port = _free_port()

        env, inference_srv = _fake_runtime_env(tmp_path)
        env["RED_SHIRT_A2A_PORT"] = str(a2a_port)
        env["RED_SHIRT_JOB_PARENT"] = str(job_parent)
        env["RED_SHIRT_JOB_ROOT"] = str(job_root)
        home = Path(env["RED_SHIRT_HOME"])
        (home / "persistent.marker").write_text("keep-me")
        env["HERMES_HOME"] = str(home)

        FakeCardServer.received_paths = []
        card_srv = http.server.HTTPServer(("127.0.0.1", a2a_port), FakeCardServer)
        threading.Thread(target=card_srv.serve_forever, daemon=True).start()
        try:
            # Hermes exits on its own with rc=0 once the earlier startup
            # gates and both Agent Card checks have had time to complete,
            # so this exercises a genuine end-to-end successful run (not a
            # SIGTERM-driven teardown like the ordering tests below).
            hermes = tmp_path / "bin" / "hermes"
            hermes.write_text("#!/usr/bin/env bash\nsleep 8\nexit 0\n")
            hermes.chmod(0o755)
            env["RED_SHIRT_HERMES_BIN"] = str(hermes)

            result = _run_entrypoint_production(env, timeout=40)
        finally:
            card_srv.shutdown()
            Path(env["RED_SHIRT_TS_SOCKET"]).unlink(missing_ok=True)

        assert result.returncode == 0, result.stderr
        assert not job_root.exists(), "validated job root must be removed on exit"
        assert job_parent.is_dir(), "the parent directory itself must survive"
        assert (home / "persistent.marker").is_file(), \
            "HERMES_HOME must never be touched by a valid run"

    def test_root_equal_to_hermes_home_fails_before_hermes_and_preserves_home(self, tmp_path):
        job_parent = tmp_path / "parent"
        job_parent.mkdir()
        home = job_parent / "home"
        home.mkdir()
        (home / "marker").write_text("keep-me")
        hermes_marker = tmp_path / "hermes-launched.marker"
        hermes = _write_exec_script(
            tmp_path / "fake_hermes.sh",
            f"#!/usr/bin/env bash\ntouch {shlex.quote(str(hermes_marker))}\nexit 0\n",
        )
        result = _run_entrypoint({
            "RED_SHIRT_HERMES_CMD": str(hermes),
            "RED_SHIRT_JOB_PARENT": str(job_parent),
            "RED_SHIRT_JOB_ROOT": str(home),
            "HERMES_HOME": str(home),
            "RED_SHIRT_TERMINAL_OUTPUT": str(tmp_path / "terminal.json"),
        })
        assert result.returncode != 0, \
            "RED_SHIRT_JOB_ROOT == HERMES_HOME must be rejected"
        assert not hermes_marker.exists(), "Hermes must never be launched"
        assert home.is_dir(), "HERMES_HOME must survive"
        assert (home / "marker").is_file(), "HERMES_HOME contents must survive"

    def test_root_outside_parent_fails_before_hermes(self, tmp_path):
        job_parent = tmp_path / "parent"
        job_parent.mkdir()
        job_root = tmp_path / "elsewhere" / "job"
        home = tmp_path / "home"
        home.mkdir()
        hermes_marker = tmp_path / "hermes-launched.marker"
        hermes = _write_exec_script(
            tmp_path / "fake_hermes.sh",
            f"#!/usr/bin/env bash\ntouch {shlex.quote(str(hermes_marker))}\nexit 0\n",
        )
        result = _run_entrypoint({
            "RED_SHIRT_HERMES_CMD": str(hermes),
            "RED_SHIRT_JOB_PARENT": str(job_parent),
            "RED_SHIRT_JOB_ROOT": str(job_root),
            "HERMES_HOME": str(home),
            "RED_SHIRT_TERMINAL_OUTPUT": str(tmp_path / "terminal.json"),
        })
        assert result.returncode != 0, \
            "a root outside the declared parent must be rejected"
        assert not hermes_marker.exists(), "Hermes must never be launched"
        assert not job_root.exists(), "the rejected root must never be created"

    def test_symlinked_root_fails_before_hermes(self, tmp_path):
        job_parent = tmp_path / "parent"
        job_parent.mkdir()
        real_target = tmp_path / "real-target"
        real_target.mkdir()
        job_root = job_parent / "job-symlink"
        job_root.symlink_to(real_target, target_is_directory=True)
        home = tmp_path / "home"
        home.mkdir()
        hermes_marker = tmp_path / "hermes-launched.marker"
        hermes = _write_exec_script(
            tmp_path / "fake_hermes.sh",
            f"#!/usr/bin/env bash\ntouch {shlex.quote(str(hermes_marker))}\nexit 0\n",
        )
        result = _run_entrypoint({
            "RED_SHIRT_HERMES_CMD": str(hermes),
            "RED_SHIRT_JOB_PARENT": str(job_parent),
            "RED_SHIRT_JOB_ROOT": str(job_root),
            "HERMES_HOME": str(home),
            "RED_SHIRT_TERMINAL_OUTPUT": str(tmp_path / "terminal.json"),
        })
        assert result.returncode != 0, "a symlinked job root must be rejected"
        assert not hermes_marker.exists(), "Hermes must never be launched"
        assert real_target.is_dir(), "the symlink target must never be touched"

    def test_non_current_uid_parent_fails_before_hermes(self, tmp_path):
        job_parent = tmp_path / "parent"
        job_parent.mkdir()
        job_root = job_parent / "job-123"
        home = tmp_path / "home"
        home.mkdir()
        hermes_marker = tmp_path / "hermes-launched.marker"
        hermes = _write_exec_script(
            tmp_path / "fake_hermes.sh",
            f"#!/usr/bin/env bash\ntouch {shlex.quote(str(hermes_marker))}\nexit 0\n",
        )
        env = {
            "RED_SHIRT_HERMES_CMD": str(hermes),
            "RED_SHIRT_JOB_PARENT": str(job_parent),
            "RED_SHIRT_JOB_ROOT": str(job_root),
            "HERMES_HOME": str(home),
            "RED_SHIRT_TERMINAL_OUTPUT": str(tmp_path / "terminal.json"),
        }
        fake_stat_src = (
            "import os as _os\n"
            "_real_stat = _os.stat\n"
            "def _fake_stat(path, *a, **k):\n"
            "    st = _real_stat(path, *a, **k)\n"
            f"    if _os.path.abspath(str(path)) == {str(job_parent)!r}:\n"
            "        st = _os.stat_result((st.st_mode, st.st_ino, st.st_dev,\n"
            "            st.st_nlink, st.st_uid + 1, st.st_gid, st.st_size,\n"
            "            st.st_atime, st.st_mtime, st.st_ctime))\n"
            "    return st\n"
            "_os.stat = _fake_stat\n"
        )
        sitecustomize = tmp_path / "sitecustomize.py"
        sitecustomize.write_text(fake_stat_src)
        env["PYTHONPATH"] = str(tmp_path)
        result = _run_entrypoint(env)
        assert result.returncode != 0, \
            "a parent not owned by the current uid must be rejected"
        assert not hermes_marker.exists(), "Hermes must never be launched"
        assert not job_root.exists(), "the rejected root must never be created"

    def test_symlinked_parent_component_fails_before_hermes(self, tmp_path):
        real_parent = tmp_path / "real-parent"
        real_parent.mkdir()
        parent_link = tmp_path / "parent-link"
        parent_link.symlink_to(real_parent, target_is_directory=True)
        job_root = parent_link / "job"
        home = tmp_path / "home"
        home.mkdir()
        hermes_marker = tmp_path / "hermes-launched.marker"
        hermes = _write_exec_script(
            tmp_path / "fake_hermes.sh",
            f"#!/usr/bin/env bash\ntouch {shlex.quote(str(hermes_marker))}\nexit 0\n",
        )
        result = _run_entrypoint({
            "RED_SHIRT_HERMES_CMD": str(hermes),
            "RED_SHIRT_JOB_PARENT": str(parent_link),
            "RED_SHIRT_JOB_ROOT": str(job_root),
            "HERMES_HOME": str(home),
            "RED_SHIRT_TERMINAL_OUTPUT": str(tmp_path / "terminal.json"),
        })
        assert result.returncode != 0, \
            "a symlinked path component in RED_SHIRT_JOB_PARENT must be rejected"
        assert not hermes_marker.exists(), "Hermes must never be launched"
        assert not job_root.exists(), "the rejected root must never be created"


# ---------------------------------------------------------------------------
# Task 7B: production runtime orchestration (scripts/red_shirt_entrypoint.sh
# without RED_SHIRT_TEST_MODE=1)
#
# Design: docs/superpowers/specs/2026-09-16-red-shirt-polaris-design.md
# Plan:   docs/superpowers/plans/2026-09-16-red-shirt-polaris.md (Task 5/6)
#
# All external binaries (tailscaled, tailscale, hermes, the token helper) are
# fake executables/sockets/HTTP that append events to a shared log file so
# exact gate order can be asserted. connect_proxy.py and red_shirt_probe.py
# are the REAL scripts under test (not faked) so this exercises real
# sockets/HTTP for the readiness gates this card owns.
# ---------------------------------------------------------------------------

FAKE_BIN_TEMPLATE = """#!/usr/bin/env bash
echo "$FAKE_EVENT" >> "$FAKE_LOG"
{body}
"""


def _fake_runtime_env(tmp_path: Path, *, hermes_body: str = None,
                       tailscaled_body: str = None, tailscale_body: str = None,
                       token_helper_ok: bool = True,
                       inference_ok: bool = True) -> dict:
    """Build a full production-path environment with fake external binaries.

    Real scripts (connect_proxy.py, red_shirt_probe.py, red_shirt_config.py)
    are used unmodified; only tailscaled/tailscale/hermes/token-helper are
    faked, plus a fake inference HTTP server the fake token-helper/renderer
    point at via a catalog/jobs fixture pair.
    """
    events_log = tmp_path / "events.log"
    events_log.write_text("")

    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()

    job_root = tmp_path / "job"
    home = tmp_path / "home"
    home.mkdir()

    # macOS caps AF_UNIX sun_path at 104 bytes (including the NUL
    # terminator), and pytest's tmp_path is already a long, deeply nested
    # path -- the default $JOB_ROOT/tailscaled.sock derived from it
    # routinely overflows that limit. Use a short, collision-resistant
    # name under /tmp instead, keyed off a hash of tmp_path so parallel
    # test runs don't collide.
    ts_socket = "/tmp/rs-" + hashlib.sha1(str(tmp_path).encode()).hexdigest()[:12] + ".sock"
    assert len(ts_socket.encode()) < 90, f"fake tailscaled socket path too long: {ts_socket!r}"

    headscale_key = _write_token(tmp_path, "fake-headscale-join-key-value", "headscale.key")
    inbound_a2a = _write_token(tmp_path, "inbound-a2a-token-1234567890", "inbound.token")
    outbound_a2a = _write_token(tmp_path, "outbound-a2a-token-1234567890", "outbound.token")

    # Fake inference server + catalog/jobs fixtures for the renderer, reused
    # for the entrypoint's own pre-Hermes inference smoke test.
    FakeInferenceServer.content = "pong" if inference_ok else ""
    FakeInferenceServer.status = 200
    FakeInferenceServer.expected_token = ""  # accept-all: token-helper output not asserted here
    inference_srv = _start(FakeInferenceServer)

    catalog_fixture = tmp_path / "catalog.json"
    catalog_fixture.write_text(json.dumps([
        {"id": "argonne/AuroraGPT-IT-v4-0125", "framework": "vllm", "max_model_len": 128000},
    ]))
    jobs_fixture = tmp_path / "jobs.json"
    jobs_fixture.write_text(json.dumps({
        "running": [{"Models": "argonne/AuroraGPT-IT-v4-0125", "Model Status": "running"}],
        "queued": [],
    }))

    token_helper = tmp_path / "fake_token_helper.py"
    if token_helper_ok:
        token_helper.write_text(
            "#!/usr/bin/env python3\nimport sys\nprint('fake-inference-access-token')\n"
        )
    else:
        token_helper.write_text(
            "#!/usr/bin/env python3\nimport sys\nsys.exit('token helper failed')\n"
        )
    token_helper.chmod(0o755)

    def _write_fake(name: str, body: str) -> Path:
        p = bin_dir / name
        p.write_text(FAKE_BIN_TEMPLATE.format(body=body))
        p.chmod(0o755)
        return p

    tailscaled = _write_fake("tailscaled", tailscaled_body or (
        f"SOCK=\"\"\n"
        f"OUTPORT=\"\"\n"
        f"for a in \"$@\"; do case \"$a\" in\n"
        f"  --socket=*) SOCK=\"${{a#--socket=}}\";;\n"
        f"  --outbound-http-proxy-listen=*) OUTPORT=\"${{a##*:}}\";;\n"
        f"esac; done\n"
        # The Unix-socket helper must model a long-running daemon: bind+listen
        # then BLOCK in accept() (nothing ever connects) so SOCK_PID stays
        # alive for the whole fake-tailscaled lifetime. Previously listen()
        # returned immediately and the helper process exited right after, so
        # the later `wait $SOCK_PID` returned instantly and tore the whole
        # fake tailscaled down right after startup -- racing the entrypoint's
        # required-child-death gate against READY.
        f"python3 -c \"import socket,sys; s=socket.socket(socket.AF_UNIX); s.bind(sys.argv[1]); s.listen(1); s.accept()\" \"$SOCK\" &\n"
        f"SOCK_PID=$!\n"
        # A real forwarding HTTP proxy on the outbound-http-proxy-listen port,
        # standing in for tailscaled's userspace outbound proxy: forwards any
        # absolute-form request (regardless of the fake tailnet hostname
        # asked for) to the real local A2A listener, so the tailnet-path
        # Agent Card gate has something real to hit.
        f"python3 -c \"\n"
        f"import http.server, os, sys, urllib.error, urllib.parse, urllib.request\n"
        f"port = int(sys.argv[1])\n"
        f"target_port = int(os.environ.get('RED_SHIRT_A2A_PORT', '9900'))\n"
        # The real userspace tailscaled outbound HTTP proxy delivers tailnet
        # traffic directly over WireGuard -- it is never itself routed
        # through the selective ALCF CONNECT proxy that the entrypoint sets
        # via HTTP_PROXY/http_proxy for tailscaled's OWN control-plane
        # traffic. This fake inherits that same env (same process tree) so
        # it must build an opener with an explicit empty ProxyHandler --
        # otherwise urlopen picks up the inherited HTTP_PROXY/http_proxy and
        # wrongly forwards through the CONNECT-only proxy, which rejects
        # plain GET.
        f"_opener = urllib.request.build_opener(urllib.request.ProxyHandler({{}}))\n"
        f"class H(http.server.BaseHTTPRequestHandler):\n"
        f"    def log_message(self, *a): pass\n"
        f"    def _fwd(self):\n"
        f"        parsed = urllib.parse.urlsplit(self.path)\n"
        f"        target = 'http://127.0.0.1:%d%s' % (target_port, parsed.path)\n"
        f"        if parsed.query: target += '?' + parsed.query\n"
        f"        try:\n"
        f"            with _opener.open(target, timeout=10) as resp:\n"
        f"                data = resp.read()\n"
        f"                self.send_response(resp.status)\n"
        f"                for k, v in resp.getheaders():\n"
        f"                    if k.lower() != 'transfer-encoding':\n"
        f"                        self.send_header(k, v)\n"
        f"                self.end_headers()\n"
        f"                self.wfile.write(data)\n"
        f"        except urllib.error.HTTPError as e:\n"
        f"            data = e.read()\n"
        f"            self.send_response(e.code)\n"
        f"            self.end_headers()\n"
        f"            self.wfile.write(data)\n"
        f"    def do_GET(self): self._fwd()\n"
        f"    def do_POST(self): self._fwd()\n"
        f"http.server.HTTPServer(('127.0.0.1', port), H).serve_forever()\n"
        f"\" \"$OUTPORT\" &\n"
        f"PROXY_PID=$!\n"
        f"trap 'kill $SOCK_PID $PROXY_PID 2>/dev/null; rm -f \"$SOCK\"' TERM INT EXIT\n"
        f"wait $SOCK_PID\n"
    ))
    tailscale = _write_fake("tailscale", tailscale_body or (
        "case \"$*\" in\n"
        "  *' up '*) exit 0 ;;\n"
        "  *' status --json'*) echo '{\"BackendState\": \"Running\"}' ;;\n"
        "  *' ip -4'*) echo '100.64.0.9' ;;\n"
        "  *' serve --bg --tcp='*) exit 0 ;;\n"
        "  *' serve reset'*) exit 0 ;;\n"
        "  *' logout'*) exit 0 ;;\n"
        "  *) exit 0 ;;\n"
        "esac\n"
    ))
    hermes = _write_fake("hermes", hermes_body or (
        "sleep 60 &\nCHILD=$!\ntrap 'kill $CHILD 2>/dev/null; exit 143' TERM\nwait $CHILD\n"
    ))

    env = {
        "RED_SHIRT_JOB_ROOT": str(job_root),
        "RED_SHIRT_JOB_PARENT": str(tmp_path),
        "RED_SHIRT_DIR": str(REPO),
        "RED_SHIRT_CONFIG_PY": str(SCRIPTS / "red_shirt_config.py"),
        "RED_SHIRT_PROBE_PY": str(PROBE),
        "RED_SHIRT_CONNECT_PROXY_PY": str(SCRIPTS / "connect_proxy.py"),
        "RED_SHIRT_PYTHON": sys.executable,
        "RED_SHIRT_TAILSCALED_BIN": str(tailscaled),
        "RED_SHIRT_TAILSCALE_BIN": str(tailscale),
        "RED_SHIRT_HERMES_BIN": str(hermes),
        "RED_SHIRT_TS_SOCKET": ts_socket,
        "RED_SHIRT_HEADSCALE_KEY_FILE": str(headscale_key),
        "RED_SHIRT_INBOUND_A2A_FILE": str(inbound_a2a),
        "RED_SHIRT_OUTBOUND_A2A_FILE": str(outbound_a2a),
        "RED_SHIRT_TOKEN_HELPER": str(token_helper),
        "RED_SHIRT_HOME": str(home),
        "RED_SHIRT_TEMPLATE": str(REPO / "config/red-shirt-polaris/config.template.yaml"),
        "RED_SHIRT_CLUSTER": "sophia",
        "RED_SHIRT_PREFERRED_MODEL": "argonne/AuroraGPT-IT-v4-0125",
        "RED_SHIRT_A2A_PORT": "0",  # overridden per-test below where needed
        "RED_SHIRT_WESLEY_URL": "http://100.64.0.2:9900/",
        "RED_SHIRT_HEADSCALE_URL": "https://headscale.invalid",
        "RED_SHIRT_HOSTNAME": "test-red-shirt",
        "RED_SHIRT_ALCF_PROXY": f"127.0.0.1:{inference_srv.server_port}",
        "RED_SHIRT_CATALOG_FIXTURE": str(catalog_fixture),
        "RED_SHIRT_JOBS_FIXTURE": str(jobs_fixture),
        "RED_SHIRT_ALCF_BASE_URL_OVERRIDE": f"http://127.0.0.1:{inference_srv.server_port}",
        "RED_SHIRT_CONNECT_PROXY_PORT": str(_free_port()),
        "RED_SHIRT_TS_OUTBOUND_HTTP_PORT": str(_free_port()),
        "RED_SHIRT_TS_UP_TIMEOUT": "10",
        "RED_SHIRT_TS_RUNNING_TIMEOUT": "10",
        "RED_SHIRT_CARD_TIMEOUT": "15",
        "RED_SHIRT_TERM_TIMEOUT": "5",
        "RED_SHIRT_READY_OUTPUT": str(tmp_path / "ready.json"),
        "RED_SHIRT_TERMINAL_OUTPUT": str(tmp_path / "terminal.json"),
    }
    return env, inference_srv


def _free_port() -> int:
    s = __import__("socket").socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


class TestProductionPathOrdering:
    """RED_SHIRT_TEST_MODE unset (production path) must run the full ordered
    startup sequence and refuse to start without RED_SHIRT_HERMES_CMD ever
    being consulted -- production never depends on that test-mode variable.
    """

    def test_exact_production_startup_order(self, tmp_path, monkeypatch):
        events = tmp_path / "startup-events.log"
        events.write_text("")

        def append_event(name: str) -> None:
            with events.open("a", encoding="utf-8") as stream:
                stream.write(name + "\n")

        original_inference_post = FakeInferenceServer.do_POST

        def instrumented_inference_post(handler):
            original_send_json = handler._send_json

            def send_json(code, payload):
                choices = payload.get("choices", [])
                content = (choices[0].get("message", {}).get("content")
                           if choices else None)
                if code == 200 and content:
                    append_event("inference_smoke")
                original_send_json(code, payload)

            handler._send_json = send_json
            original_inference_post(handler)

        monkeypatch.setattr(FakeInferenceServer, "do_POST", instrumented_inference_post)
        env, inference_srv = _fake_runtime_env(tmp_path)
        assert Path(env["RED_SHIRT_JOB_ROOT"]) not in events.parents

        real_config = SCRIPTS / "red_shirt_config.py"
        config_wrapper = tmp_path / "config-wrapper.py"
        config_wrapper.write_text(
            "#!/usr/bin/env python3\n"
            "import os, subprocess, sys\n"
            "rc = subprocess.run([sys.executable, os.environ['REAL_CONFIG'], *sys.argv[1:]]).returncode\n"
            "if rc == 0 and len(sys.argv) > 1:\n"
            "    event = {'validate-secrets': 'credentials_validated', 'render': 'config_rendered'}.get(sys.argv[1])\n"
            "    if event:\n"
            "        with open(os.environ['STARTUP_EVENTS'], 'a', encoding='utf-8') as f: f.write(event + '\\n')\n"
            "sys.exit(rc)\n"
        )
        config_wrapper.chmod(0o755)

        connect_proxy = tmp_path / "connect-proxy.py"
        connect_proxy.write_text(
            "#!/usr/bin/env python3\n"
            "import argparse, os, socket\n"
            "p = argparse.ArgumentParser(); p.add_argument('--listen-port', type=int); p.add_argument('--upstream'); a = p.parse_args()\n"
            "s = socket.socket(); s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1); s.bind(('127.0.0.1', a.listen_port)); s.listen()\n"
            "logged = False\n"
            "while True:\n"
            "    conn, _ = s.accept()\n"
            "    if not logged:\n"
            "        with open(os.environ['STARTUP_EVENTS'], 'a', encoding='utf-8') as f: f.write('connect_proxy_reachable\\n')\n"
            "        logged = True\n"
            "    conn.close()\n"
        )
        connect_proxy.chmod(0o755)

        tailscaled = tmp_path / "bin" / "tailscaled"
        tailscaled.write_text(
            "#!/usr/bin/env python3\n"
            "import http.server, os, signal, socket, sys, threading, urllib.error, urllib.parse, urllib.request\n"
            "sock_path = next(x.split('=', 1)[1] for x in sys.argv if x.startswith('--socket='))\n"
            "proxy_port = int(next(x.rsplit(':', 1)[1] for x in sys.argv if x.startswith('--outbound-http-proxy-listen=')))\n"
            "target_port = int(os.environ['RED_SHIRT_A2A_PORT'])\n"
            "sock = socket.socket(socket.AF_UNIX); sock.bind(sock_path); sock.listen(1)\n"
            "with open(os.environ['STARTUP_EVENTS'], 'a', encoding='utf-8') as f: f.write('tailscaled_started\\n')\n"
            "opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))\n"
            "class Handler(http.server.BaseHTTPRequestHandler):\n"
            "    def log_message(self, *args): pass\n"
            "    def do_GET(self):\n"
            "        parsed = urllib.parse.urlsplit(self.path)\n"
            "        target = 'http://127.0.0.1:%d%s' % (target_port, parsed.path)\n"
            "        if parsed.query: target += '?' + parsed.query\n"
            "        req = urllib.request.Request(target, headers={'X-Red-Shirt-Tailnet-Probe': '1'})\n"
            "        try:\n"
            "            with opener.open(req, timeout=10) as resp:\n"
            "                data = resp.read(); self.send_response(resp.status)\n"
            "                for key, value in resp.getheaders():\n"
            "                    if key.lower() != 'transfer-encoding': self.send_header(key, value)\n"
            "                self.end_headers(); self.wfile.write(data)\n"
            "        except urllib.error.HTTPError as exc:\n"
            "            data = exc.read(); self.send_response(exc.code); self.end_headers(); self.wfile.write(data)\n"
            "server = http.server.HTTPServer(('127.0.0.1', proxy_port), Handler)\n"
            "signal.signal(signal.SIGTERM, lambda *_: sys.exit(143))\n"
            "try: server.serve_forever()\n"
            "finally: server.server_close(); sock.close(); os.unlink(sock_path) if os.path.exists(sock_path) else None\n"
        )
        tailscaled.chmod(0o755)

        tailscale = tmp_path / "bin" / "tailscale"
        tailscale.write_text(
            "#!/usr/bin/env python3\n"
            "import json, os, sys\n"
            "args = ' '.join(sys.argv[1:])\n"
            "def log(name):\n"
            "    with open(os.environ['STARTUP_EVENTS'], 'a', encoding='utf-8') as f: f.write(name + '\\n')\n"
            "if ' up ' in ' ' + args + ' ': sys.exit(0)\n"
            "if ' status --json' in args: print(json.dumps({'BackendState': 'Running'})); log('tailscale_up')\n"
            "elif ' ip -4' in args: print('100.64.0.9')\n"
            "elif ' serve --bg --tcp=' in args: log('tailscale_serve')\n"
            "sys.exit(0)\n"
        )
        tailscale.chmod(0o755)

        hermes = tmp_path / "bin" / "hermes"
        hermes.write_text(
            "#!/usr/bin/env python3\n"
            "import os, signal, sys, time\n"
            "with open(os.environ['STARTUP_EVENTS'], 'a', encoding='utf-8') as f: f.write('hermes_started\\n')\n"
            "signal.signal(signal.SIGTERM, lambda *_: sys.exit(143))\n"
            "while True: time.sleep(1)\n"
        )
        hermes.chmod(0o755)

        class OrderedCardHandler(FakeCardServer):
            def do_GET(self):  # noqa: N802
                # The entrypoint launches Hermes before probing its card, but
                # the child may not be scheduled before the first probe. Model
                # a real gateway: it cannot serve a card until Hermes started.
                if "hermes_started" not in events.read_text().splitlines():
                    self._send_json(503, {"error": "gateway starting"})
                    return
                event = ("tailnet_card" if self.headers.get("X-Red-Shirt-Tailnet-Probe") == "1"
                         else "local_card")
                original_send_json = self._send_json

                def send_json(code, payload):
                    if code == 200:
                        append_event(event)
                    original_send_json(code, payload)

                self._send_json = send_json
                super().do_GET()

        a2a_port = _free_port()
        card_srv = http.server.HTTPServer(("127.0.0.1", a2a_port), OrderedCardHandler)
        threading.Thread(target=card_srv.serve_forever, daemon=True).start()
        env.update({
            "RED_SHIRT_A2A_PORT": str(a2a_port),
            "RED_SHIRT_CONFIG_PY": str(config_wrapper),
            "RED_SHIRT_CONNECT_PROXY_PY": str(connect_proxy),
            "RED_SHIRT_TAILSCALED_BIN": str(tailscaled),
            "RED_SHIRT_TAILSCALE_BIN": str(tailscale),
            "RED_SHIRT_HERMES_BIN": str(hermes),
            "REAL_CONFIG": str(real_config),
            "STARTUP_EVENTS": str(events),
        })
        full_env = os.environ.copy()
        full_env.update(env)
        proc = subprocess.Popen(["bash", str(ENTRYPOINT)], env=full_env,
                                stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        out = err = None
        try:
            ready_path = Path(env["RED_SHIRT_READY_OUTPUT"])
            deadline = time.time() + 40
            while time.time() < deadline and proc.poll() is None:
                if ready_path.exists():
                    record = json.loads(ready_path.read_text())
                    if record.get("ok") is True:
                        append_event("ready_observed")
                        break
                time.sleep(0.1)
            observed = events.read_text().splitlines()
            expected = [
                "credentials_validated", "connect_proxy_reachable", "tailscaled_started",
                "tailscale_up", "tailscale_serve", "config_rendered", "inference_smoke",
                "hermes_started", "local_card", "tailnet_card", "ready_observed",
            ]
            assert observed == expected, (
                f"startup order mismatch: observed={observed!r}, expected={expected!r}, "
                f"returncode={proc.poll()!r}"
            )
        finally:
            try:
                if proc.poll() is None:
                    proc.send_signal(signal.SIGTERM)
                try:
                    out, err = proc.communicate(timeout=20)
                except subprocess.TimeoutExpired:
                    proc.kill()
                    out, err = proc.communicate(timeout=5)
            finally:
                try:
                    card_srv.shutdown()
                    card_srv.server_close()
                finally:
                    try:
                        inference_srv.shutdown()
                        inference_srv.server_close()
                    finally:
                        Path(env["RED_SHIRT_TS_SOCKET"]).unlink(missing_ok=True)
        assert proc.returncode == 143, f"stdout={out!r}, stderr={err!r}"

    def test_production_path_does_not_require_hermes_cmd(self, tmp_path, monkeypatch):
        """Round-1 requirement: production works without RED_SHIRT_HERMES_CMD
        (the bug the card was opened to fix)."""
        a2a_port = _free_port()
        env, inference_srv = _fake_runtime_env(tmp_path)
        env["RED_SHIRT_A2A_PORT"] = str(a2a_port)
        # A real local HTTP server standing in for the Hermes A2A listener,
        # so the local + tailnet card gates have something real to hit.
        FakeCardServer.received_paths = []
        card_srv = http.server.HTTPServer(("127.0.0.1", a2a_port), FakeCardServer)
        t = threading.Thread(target=card_srv.serve_forever, daemon=True)
        t.start()
        try:
            hermes = tmp_path / "bin" / "hermes"
            hermes.write_text(
                "#!/usr/bin/env bash\nsleep 60 &\nCHILD=$!\n"
                "trap 'kill $CHILD 2>/dev/null; exit 143' TERM\nwait $CHILD\n"
            )
            hermes.chmod(0o755)
            env["RED_SHIRT_HERMES_BIN"] = str(hermes)

            full_env = os.environ.copy()
            full_env.update(env)
            assert "RED_SHIRT_HERMES_CMD" not in full_env

            proc = subprocess.Popen(["bash", str(ENTRYPOINT)], env=full_env,
                                     stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
            out = err = None
            try:
                ready_path = Path(env["RED_SHIRT_READY_OUTPUT"])
                deadline = time.time() + 40
                while (time.time() < deadline and not ready_path.exists()
                       and proc.poll() is None):
                    time.sleep(0.2)
                diag = ""
                if not ready_path.exists() and proc.poll() is not None:
                    # Process already exited without ever writing READY --
                    # grab its output once so the failure is diagnosable.
                    # The runtime is contractually non-secret, so stderr is
                    # safe to surface verbatim (no credential contents).
                    out, err = proc.communicate(timeout=5)
                    diag = (f" (process exited early: returncode={proc.returncode}, "
                             f"stderr={err!r})")
                assert ready_path.exists(), f"production path never reached READY{diag}"
                record = json.loads(ready_path.read_text())
                assert record["ok"] is True
                assert record["model"] == "argonne/AuroraGPT-IT-v4-0125"
                assert record["tailnet_ip"]
            finally:
                if proc.poll() is None:
                    proc.send_signal(signal.SIGTERM)
                if out is None:
                    out, err = proc.communicate(timeout=20)
        finally:
            card_srv.shutdown()
            # Belt-and-suspenders: the fake tailscaled's own TERM/INT/EXIT
            # trap removes this socket, but guard against residue if the
            # process was killed harder than SIGTERM allows for.
            Path(env["RED_SHIRT_TS_SOCKET"]).unlink(missing_ok=True)
        assert proc.returncode == 143, err

    def test_production_path_fails_before_hermes_if_inference_smoke_fails(self, tmp_path):
        """No Hermes launch may occur when the direct inference smoke test
        fails -- assert the fake hermes binary's marker file is absent."""
        marker = tmp_path / "hermes-was-launched.marker"
        env, inference_srv = _fake_runtime_env(tmp_path, inference_ok=False)
        env["RED_SHIRT_A2A_PORT"] = str(_free_port())
        hermes = tmp_path / "bin" / "hermes"
        hermes.write_text(f"#!/usr/bin/env bash\ntouch {shlex.quote(str(marker))}\nsleep 60\n")
        hermes.chmod(0o755)
        env["RED_SHIRT_HERMES_BIN"] = str(hermes)

        result = _run_entrypoint_production(env, timeout=40)
        assert result.returncode != 0
        assert not marker.exists(), "Hermes must never launch after a failed inference smoke test"
        term = json.loads(Path(env["RED_SHIRT_TERMINAL_OUTPUT"]).read_text())
        assert term["ok"] is False

    def test_production_path_no_ready_before_local_card_passes(self, tmp_path):
        """READY must never be written if the local Agent Card never comes up
        (nothing listens on the A2A port here -- hermes exits immediately)."""
        env, inference_srv = _fake_runtime_env(tmp_path)
        env["RED_SHIRT_A2A_PORT"] = str(_free_port())
        env["RED_SHIRT_CARD_TIMEOUT"] = "3"
        hermes = tmp_path / "bin" / "hermes"
        hermes.write_text("#!/usr/bin/env bash\nexit 0\n")  # exits immediately, nothing listens
        hermes.chmod(0o755)
        env["RED_SHIRT_HERMES_BIN"] = str(hermes)

        result = _run_entrypoint_production(env, timeout=40)
        assert result.returncode != 0
        assert not Path(env["RED_SHIRT_READY_OUTPUT"]).exists(), \
            "READY must not be written when the local card never comes up"

    def test_production_path_required_child_death_tears_down(self, tmp_path):
        """A required child (tailscaled) dying while Hermes is still running
        must be detected and tear the whole run down."""
        a2a_port = _free_port()
        env, inference_srv = _fake_runtime_env(tmp_path)
        env["RED_SHIRT_A2A_PORT"] = str(a2a_port)
        env["RED_SHIRT_TS_UP_TIMEOUT"] = "5"
        env["RED_SHIRT_TS_RUNNING_TIMEOUT"] = "5"

        FakeCardServer.received_paths = []
        card_srv = http.server.HTTPServer(("127.0.0.1", a2a_port), FakeCardServer)
        threading.Thread(target=card_srv.serve_forever, daemon=True).start()
        try:
            hermes_marker = tmp_path / "hermes-alive.marker"
            hermes = tmp_path / "bin" / "hermes"
            hermes.write_text(
                f"#!/usr/bin/env bash\n"
                f"trap 'rm -f {shlex.quote(str(hermes_marker))}; exit 143' TERM\n"
                f"touch {shlex.quote(str(hermes_marker))}\n"
                f"sleep 60 & wait $!\n"
            )
            hermes.chmod(0o755)
            env["RED_SHIRT_HERMES_BIN"] = str(hermes)

            # tailscaled that exits shortly after the socket appears, to
            # simulate the required child dying mid-run.
            tailscaled = tmp_path / "bin" / "tailscaled"
            tailscaled.write_text(
                "#!/usr/bin/env bash\n"
                "SOCK=\"\"\n"
                "for a in \"$@\"; do case \"$a\" in --socket=*) SOCK=\"${a#--socket=}\";; esac; done\n"
                "python3 -c \"import socket,sys; s=socket.socket(socket.AF_UNIX); s.bind(sys.argv[1]); s.listen(1)\" \"$SOCK\" &\n"
                "SOCK_PID=$!\n"
                "sleep 6\n"
                "kill $SOCK_PID 2>/dev/null\n"
                "exit 1\n"
            )
            tailscaled.chmod(0o755)
            env["RED_SHIRT_TAILSCALED_BIN"] = str(tailscaled)

            result = _run_entrypoint_production(env, timeout=40)
            assert result.returncode != 0
            assert not hermes_marker.exists(), \
                "Hermes must be torn down after the required tailscaled child died"
        finally:
            card_srv.shutdown()

    def test_production_path_cleanup_serve_reset_and_logout(self, tmp_path):
        """On teardown the entrypoint must invoke `tailscale serve reset` and
        `tailscale logout` -- observed via the fake tailscale binary's event
        log."""
        a2a_port = _free_port()
        events_log = tmp_path / "ts_events.log"
        events_log.write_text("")
        env, inference_srv = _fake_runtime_env(tmp_path)
        env["RED_SHIRT_A2A_PORT"] = str(a2a_port)

        FakeCardServer.received_paths = []
        card_srv = http.server.HTTPServer(("127.0.0.1", a2a_port), FakeCardServer)
        threading.Thread(target=card_srv.serve_forever, daemon=True).start()
        try:
            hermes = tmp_path / "bin" / "hermes"
            hermes.write_text(
                "#!/usr/bin/env bash\n"
                "trap 'exit 143' TERM\n"
                "sleep 60 & wait $!\n"
            )
            hermes.chmod(0o755)
            env["RED_SHIRT_HERMES_BIN"] = str(hermes)

            tailscale = tmp_path / "bin" / "tailscale"
            tailscale.write_text(
                f"#!/usr/bin/env bash\n"
                f"echo \"$*\" >> {shlex.quote(str(events_log))}\n"
                "case \"$*\" in\n"
                "  *' up '*) exit 0 ;;\n"
                "  *' status --json'*) echo '{\"BackendState\": \"Running\"}' ;;\n"
                "  *' ip -4'*) echo '100.64.0.9' ;;\n"
                "  *' serve --bg --tcp='*) exit 0 ;;\n"
                "  *' serve reset'*) exit 0 ;;\n"
                "  *' logout'*) exit 0 ;;\n"
                "  *) exit 0 ;;\n"
                "esac\n"
            )
            tailscale.chmod(0o755)
            env["RED_SHIRT_TAILSCALE_BIN"] = str(tailscale)

            full_env = os.environ.copy()
            full_env.update(env)
            proc = subprocess.Popen(["bash", str(ENTRYPOINT)], env=full_env,
                                     stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
            try:
                ready_path = Path(env["RED_SHIRT_READY_OUTPUT"])
                deadline = time.time() + 40
                while time.time() < deadline and not ready_path.exists():
                    time.sleep(0.2)
                assert ready_path.exists()
                proc.send_signal(signal.SIGTERM)
                out, err = proc.communicate(timeout=20)
            finally:
                if proc.poll() is None:
                    proc.kill()
                    proc.communicate(timeout=5)
        finally:
            card_srv.shutdown()

        events_text = events_log.read_text()
        assert "serve reset" in events_text
        assert "logout" in events_text

    def test_production_path_no_secret_in_stdout_stderr_or_records(self, tmp_path):
        """No mounted credential value or inference token may ever appear in
        stdout, stderr, ready.json, or terminal.json."""
        a2a_port = _free_port()
        env, inference_srv = _fake_runtime_env(tmp_path)
        env["RED_SHIRT_A2A_PORT"] = str(a2a_port)

        FakeCardServer.received_paths = []
        card_srv = http.server.HTTPServer(("127.0.0.1", a2a_port), FakeCardServer)
        threading.Thread(target=card_srv.serve_forever, daemon=True).start()
        try:
            hermes = tmp_path / "bin" / "hermes"
            hermes.write_text("#!/usr/bin/env bash\ntrap 'exit 143' TERM\nsleep 60 & wait $!\n")
            hermes.chmod(0o755)
            env["RED_SHIRT_HERMES_BIN"] = str(hermes)

            full_env = os.environ.copy()
            full_env.update(env)
            proc = subprocess.Popen(["bash", str(ENTRYPOINT)], env=full_env,
                                     stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
            try:
                ready_path = Path(env["RED_SHIRT_READY_OUTPUT"])
                deadline = time.time() + 40
                while time.time() < deadline and not ready_path.exists():
                    time.sleep(0.2)
                assert ready_path.exists()
                proc.send_signal(signal.SIGTERM)
                out, err = proc.communicate(timeout=20)
            finally:
                if proc.poll() is None:
                    proc.kill()
                    proc.communicate(timeout=5)
        finally:
            card_srv.shutdown()

        secrets = ["fake-headscale-join-key-value", "inbound-a2a-token-1234567890",
                   "outbound-a2a-token-1234567890", "fake-inference-access-token"]
        haystacks = [out, err,
                     Path(env["RED_SHIRT_READY_OUTPUT"]).read_text(),
                     Path(env["RED_SHIRT_TERMINAL_OUTPUT"]).read_text()]
        for secret in secrets:
            for h in haystacks:
                assert secret not in h, f"secret {secret!r} leaked"


def _run_entrypoint_production(env_overrides: dict, timeout: float = 40) -> subprocess.CompletedProcess:
    full_env = os.environ.copy()
    full_env.update(env_overrides)
    return subprocess.run(["bash", str(ENTRYPOINT)], env=full_env,
                          capture_output=True, text=True, timeout=timeout)


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-v"]))
