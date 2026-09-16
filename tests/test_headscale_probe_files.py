#!/usr/bin/env python3
"""
Test suite for the Headscale probe OCI image components:
  - scripts/connect_proxy.py  (CONNECT rewrite proxy, stdlib-only)
  - scripts/headscale_probe.sh
  - Dockerfile.headscale-probe
  - .github/workflows/build.yml  (headscale-probe image job)

Tests are split into:
  1. Behavioural tests that actually exercise connect_proxy.py with a fake
     upstream CONNECT forwarder (real sockets, real threading).
  2. Static analysis tests (regex / AST / shell syntax) for all three files.

No network calls are made outside localhost; no privileged ops; no Docker
daemon required.

Run:
    pytest tests/test_headscale_probe_files.py -v
"""
from __future__ import annotations

import importlib.util
import os
import re
import socket
import subprocess
import sys
import threading
import time

import pytest

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CONNECT_PROXY = os.path.join(REPO, "scripts", "connect_proxy.py")
PROBE_SCRIPT  = os.path.join(REPO, "scripts", "headscale_probe.sh")
DOCKERFILE    = os.path.join(REPO, "Dockerfile.headscale-probe")
BUILD_YML     = os.path.join(REPO, ".github", "workflows", "build.yml")

# Well-known constants that the implementation must match exactly.
HEADSCALE_HOST = "143.198.112.69.sslip.io"
HEADSCALE_IP   = "143.198.112.69"
HEADSCALE_PORT = 443

# ============================================================================
# Helpers
# ============================================================================

def _read(path: str) -> str:
    assert os.path.isfile(path), f"Missing: {path}"
    with open(path) as fh:
        return fh.read()


def _free_port() -> int:
    """Bind to an ephemeral port, release it, return the port number."""
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _load_connect_proxy_module():
    """Import connect_proxy.py as a module (without executing __main__)."""
    spec = importlib.util.spec_from_file_location("connect_proxy", CONNECT_PROXY)
    assert spec is not None and spec.loader is not None, \
        f"Could not create module spec for {CONNECT_PROXY}"
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)  # type: ignore[attr-defined]
    return mod


# ============================================================================
# SECTION 1: File existence
# ============================================================================

class TestFilesExist:
    def test_connect_proxy_exists(self):
        assert os.path.isfile(CONNECT_PROXY), f"Missing: {CONNECT_PROXY}"

    def test_headscale_probe_exists(self):
        assert os.path.isfile(PROBE_SCRIPT), f"Missing: {PROBE_SCRIPT}"

    def test_dockerfile_exists(self):
        assert os.path.isfile(DOCKERFILE), f"Missing: {DOCKERFILE}"

    def test_dockerfile_is_not_empty(self):
        content = _read(DOCKERFILE)
        assert len(content.strip()) > 50, "Dockerfile.headscale-probe appears empty"

    def test_connect_proxy_is_python3(self):
        content = _read(CONNECT_PROXY)
        assert content.startswith("#!/usr/bin/env python3") or \
               content.startswith("#!/usr/bin/python3"), \
               "connect_proxy.py must start with a python3 shebang"

    def test_probe_script_is_bash(self):
        content = _read(PROBE_SCRIPT)
        assert content.startswith("#!/usr/bin/env bash") or \
               content.startswith("#!/bin/bash"), \
               "headscale_probe.sh must start with a bash shebang"


# ============================================================================
# SECTION 2: connect_proxy.py — static analysis
# ============================================================================

class TestConnectProxyStatic:
    """Verify key design properties via source inspection."""

    def test_no_third_party_imports(self):
        """connect_proxy.py must use only stdlib — no pip deps in the image."""
        content = _read(CONNECT_PROXY)
        import ast
        tree = ast.parse(content)
        stdlib = {
            "__future__", "socket", "threading", "logging", "signal", "sys", "os",
            "argparse", "time", "errno", "select", "struct", "re",
            "contextlib", "io", "queue", "functools", "typing",
        }
        for node in ast.walk(tree):
            if isinstance(node, (ast.Import,)):
                for alias in node.names:
                    top = alias.name.split(".")[0]
                    assert top in stdlib, (
                        f"Non-stdlib import '{alias.name}' found in connect_proxy.py. "
                        "Only stdlib modules are permitted (no pip in the image)."
                    )
            elif isinstance(node, ast.ImportFrom):
                top = (node.module or "").split(".")[0]
                assert top in stdlib, (
                    f"Non-stdlib import 'from {node.module}' found in connect_proxy.py."
                )

    def test_only_connect_method_accepted(self):
        """Proxy must only accept CONNECT — any other HTTP method is rejected."""
        content = _read(CONNECT_PROXY)
        # Verify the source mentions CONNECT and rejects other methods.
        assert re.search(r"\bCONNECT\b", content), \
            "CONNECT keyword not found in connect_proxy.py"
        # Must reject non-CONNECT (look for 405 or method check logic)
        assert re.search(r"405|method not allowed|unsupported method", content, re.IGNORECASE), \
            "connect_proxy.py must return 405 or reject non-CONNECT methods"

    def test_rewrite_headscale_to_numeric_ip(self):
        """Proxy must rewrite the exact Headscale authority to numeric IP."""
        content = _read(CONNECT_PROXY)
        assert HEADSCALE_HOST in content, \
            f"Headscale hostname '{HEADSCALE_HOST}' not found in connect_proxy.py"
        assert HEADSCALE_IP in content, \
            f"Numeric IP '{HEADSCALE_IP}' not found in connect_proxy.py"

    def test_reject_non_headscale_by_default(self):
        """Proxy must reject (fail-closed) destinations other than Headscale."""
        content = _read(CONNECT_PROXY)
        # Should have a 403 or 'forbidden' response for unknown destinations
        assert re.search(r"403|forbidden", content, re.IGNORECASE), \
            "connect_proxy.py must return 403 Forbidden for non-whitelisted destinations"

    def test_chains_to_upstream_proxy(self):
        """Proxy must forward the rewritten CONNECT to an upstream proxy."""
        content = _read(CONNECT_PROXY)
        # upstream proxy env var or arg
        assert re.search(r"upstream|alcf.*proxy|proxy.*upstream", content, re.IGNORECASE), \
            "connect_proxy.py must chain to an upstream proxy (ALCF proxy)"

    def test_does_not_terminate_tls(self):
        """Proxy must tunnel TLS bytes as-is — ssl module must not be imported or used."""
        import ast
        content = _read(CONNECT_PROXY)
        tree = ast.parse(content)
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    assert alias.name != "ssl", (
                        "connect_proxy.py must not import the ssl module — "
                        "TLS must be end-to-end (proxy tunnels raw bytes)"
                    )
            elif isinstance(node, ast.ImportFrom):
                assert node.module != "ssl", (
                    "connect_proxy.py must not import from ssl — "
                    "TLS must be end-to-end"
                )

    def test_no_secret_logging(self):
        """connect_proxy.py must not log request headers or bodies."""
        content = _read(CONNECT_PROXY)
        # Must not log full headers which could contain auth tokens
        # We check that no logging call concatenates a full header block
        assert not re.search(r'log.*headers.*auth|log.*Authorization', content, re.IGNORECASE), \
            "connect_proxy.py must not log Authorization headers"

    def test_listen_host_is_loopback(self):
        """Default listen address must be 127.0.0.1 (not 0.0.0.0)."""
        content = _read(CONNECT_PROXY)
        assert re.search(r"127\.0\.0\.1", content), \
            "connect_proxy.py default listen host must be 127.0.0.1 (loopback)"


# ============================================================================
# SECTION 3: connect_proxy.py — behavioural tests (live sockets)
# ============================================================================

class FakeUpstreamProxy:
    """
    A minimal fake 'upstream' CONNECT proxy that records the CONNECT line it
    received and then either tunnels bytes or closes.  Runs in a thread.
    """

    def __init__(self, host: str = "127.0.0.1", port: int = 0,
                 respond_ok: bool = True):
        self.host = host
        self._sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self._sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self._sock.bind((host, port))
        self._sock.listen(5)
        self.port = self._sock.getsockname()[1]
        self.respond_ok = respond_ok
        self.received_connect_lines: list[str] = []
        self._thread = threading.Thread(target=self._serve, daemon=True)
        self._thread.start()

    def _serve(self):
        try:
            self._sock.settimeout(5)
            conn, _ = self._sock.accept()
            with conn:
                data = b""
                while b"\r\n\r\n" not in data:
                    chunk = conn.recv(4096)
                    if not chunk:
                        break
                    data += chunk
                header_block = data.decode(errors="replace")
                first_line = header_block.splitlines()[0] if header_block else ""
                self.received_connect_lines.append(first_line)
                if self.respond_ok:
                    conn.sendall(b"HTTP/1.1 200 Connection established\r\n\r\n")
                    # echo any subsequent bytes back (tunnel simulation)
                    conn.settimeout(1)
                    try:
                        while True:
                            b = conn.recv(4096)
                            if not b:
                                break
                            conn.sendall(b)
                    except (socket.timeout, OSError):
                        pass
                else:
                    conn.sendall(b"HTTP/1.1 503 Bad Gateway\r\n\r\n")
        except (socket.timeout, OSError):
            pass
        finally:
            self._sock.close()

    def stop(self):
        self._sock.close()


def _start_connect_proxy(listen_port: int, upstream_host: str,
                          upstream_port: int) -> subprocess.Popen:
    """Launch connect_proxy.py as a subprocess."""
    env = os.environ.copy()
    env["UPSTREAM_PROXY"] = f"http://{upstream_host}:{upstream_port}"
    proc = subprocess.Popen(
        [sys.executable, CONNECT_PROXY,
         "--listen-port", str(listen_port),
         "--upstream", f"{upstream_host}:{upstream_port}"],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        env=env,
    )
    # Give the proxy a moment to bind
    time.sleep(0.3)
    return proc


def _send_connect(proxy_port: int, authority: str,
                   timeout: float = 3.0) -> tuple[str, bytes]:
    """
    Open a raw socket to the local proxy, send CONNECT <authority> HTTP/1.1,
    return (status_line, remaining_bytes).
    """
    with socket.create_connection(("127.0.0.1", proxy_port), timeout=timeout) as s:
        request = (
            f"CONNECT {authority} HTTP/1.1\r\n"
            f"Host: {authority}\r\n"
            f"\r\n"
        ).encode()
        s.sendall(request)
        s.settimeout(timeout)
        response = b""
        while b"\r\n\r\n" not in response:
            chunk = s.recv(4096)
            if not chunk:
                break
            response += chunk
        header_part = response.split(b"\r\n\r\n")[0]
        status_line = header_part.decode(errors="replace").splitlines()[0]
        return status_line, response


@pytest.fixture
def fake_upstream():
    """Fixture: a fake upstream proxy that accepts CONNECT and responds 200."""
    proxy = FakeUpstreamProxy()
    yield proxy
    proxy.stop()


@pytest.mark.skipif(
    not os.path.isfile(CONNECT_PROXY),
    reason="connect_proxy.py not yet implemented"
)
class TestConnectProxyBehaviour:
    """Live-socket tests against a running connect_proxy.py subprocess."""

    def test_headscale_connect_is_rewritten_to_numeric_ip(self, fake_upstream):
        """
        CONNECT 143.198.112.69.sslip.io:443 must be forwarded to the upstream
        as CONNECT 143.198.112.69:443 (numeric IP rewrite).
        """
        listen_port = _free_port()
        proc = _start_connect_proxy(listen_port, "127.0.0.1", fake_upstream.port)
        try:
            status_line, _ = _send_connect(
                listen_port, f"{HEADSCALE_HOST}:{HEADSCALE_PORT}"
            )
            assert "200" in status_line, (
                f"Expected 200 from proxy for Headscale CONNECT, got: {status_line!r}"
            )
            # Give fake upstream time to record the line
            time.sleep(0.1)
            assert fake_upstream.received_connect_lines, \
                "Fake upstream never received a CONNECT — proxy did not forward"
            connect_sent = fake_upstream.received_connect_lines[0]
            # Rewrite check: upstream sees numeric IP, not the sslip hostname
            assert HEADSCALE_IP + ":443" in connect_sent, (
                f"Upstream did not receive numeric-IP CONNECT. Got: {connect_sent!r}\n"
                f"Expected 'CONNECT {HEADSCALE_IP}:443' to reach the upstream."
            )
            assert HEADSCALE_HOST not in connect_sent, (
                f"sslip.io hostname leaked to upstream: {connect_sent!r}\n"
                "Proxy must rewrite to numeric IP before forwarding."
            )
        finally:
            proc.terminate()
            proc.wait(timeout=5)

    def test_non_headscale_connect_is_rejected(self, fake_upstream):
        """
        CONNECT to a non-whitelisted host must be rejected with 403 Forbidden.
        The fake upstream must NOT receive any connection.
        """
        listen_port = _free_port()
        proc = _start_connect_proxy(listen_port, "127.0.0.1", fake_upstream.port)
        try:
            status_line, _ = _send_connect(
                listen_port, "evil.example.com:443"
            )
            assert "403" in status_line or "400" in status_line, (
                f"Expected 403 for non-whitelisted host, got: {status_line!r}"
            )
            time.sleep(0.1)
            assert not fake_upstream.received_connect_lines, (
                "Fake upstream received a CONNECT for a non-whitelisted host — "
                "proxy must reject before forwarding (fail-closed)."
            )
        finally:
            proc.terminate()
            proc.wait(timeout=5)

    def test_non_connect_method_rejected(self, fake_upstream):
        """
        A plain GET or POST must be rejected with 405 Method Not Allowed.
        """
        listen_port = _free_port()
        proc = _start_connect_proxy(listen_port, "127.0.0.1", fake_upstream.port)
        try:
            with socket.create_connection(("127.0.0.1", listen_port), timeout=3) as s:
                s.sendall(b"GET http://example.com/ HTTP/1.1\r\nHost: example.com\r\n\r\n")
                s.settimeout(3)
                response = b""
                while b"\r\n\r\n" not in response:
                    chunk = s.recv(4096)
                    if not chunk:
                        break
                    response += chunk
            status = response.split(b"\r\n")[0].decode(errors="replace")
            assert "405" in status, (
                f"Expected 405 Method Not Allowed for GET request, got: {status!r}"
            )
        finally:
            proc.terminate()
            proc.wait(timeout=5)

    def test_upstream_failure_returns_502(self, fake_upstream):
        """
        If upstream returns 503 (or refuses), proxy must return 502/503 to client.
        """
        bad_upstream = FakeUpstreamProxy(respond_ok=False)
        listen_port = _free_port()
        proc = _start_connect_proxy(listen_port, "127.0.0.1", bad_upstream.port)
        try:
            status_line, _ = _send_connect(
                listen_port, f"{HEADSCALE_HOST}:{HEADSCALE_PORT}"
            )
            assert "502" in status_line or "503" in status_line, (
                f"Expected 502/503 when upstream fails, got: {status_line!r}"
            )
        finally:
            proc.terminate()
            proc.wait(timeout=5)
            bad_upstream.stop()


# ============================================================================
# SECTION 4: headscale_probe.sh — static analysis
# ============================================================================

class TestHeadscaleProbeStatic:
    """Static checks on scripts/headscale_probe.sh."""

    def test_bash_syntax_valid(self):
        """bash -n must succeed."""
        result = subprocess.run(
            ["bash", "-n", PROBE_SCRIPT],
            capture_output=True, text=True
        )
        assert result.returncode == 0, (
            f"bash -n reported syntax errors:\n{result.stderr}"
        )

    def test_runs_as_non_root_uid(self):
        """Script must NOT use 'su -' or 'sudo' — it runs as uid 10000."""
        content = _read(PROBE_SCRIPT)
        assert not re.search(r"\bsudo\b|\bsu\s+-\b", content), \
            "headscale_probe.sh must not use sudo/su (runs as uid 10000)"

    def test_no_privileged_networking(self):
        """Script must not request --net=host or privileged mode."""
        content = _read(PROBE_SCRIPT)
        assert not re.search(r"--net=host|--privileged", content), \
            "Probe must not use host networking or privileged mode"

    def test_no_insecure_tls(self):
        """Neither -k nor --insecure must appear in any curl call."""
        content = _read(PROBE_SCRIPT)
        assert not re.search(r"\bcurl\b[^\n]*(-k\b|--insecure)", content), \
            "curl -k / --insecure must not appear in headscale_probe.sh"

    def test_auth_key_from_file(self):
        """tailscale up must use --auth-key=file:... (never log secret)."""
        content = _read(PROBE_SCRIPT)
        assert re.search(r"--auth-key=file:", content), \
            "tailscale up must use --auth-key=file:<path> not --auth-key=<literal>"

    def test_login_server_flag(self):
        """tailscale up must specify --login-server."""
        content = _read(PROBE_SCRIPT)
        assert re.search(r"--login-server", content), \
            "tailscale up must specify --login-server"

    def test_userspace_networking(self):
        """tailscaled must use userspace networking (no TUN device)."""
        content = _read(PROBE_SCRIPT)
        assert re.search(r"--tun=userspace-networking|userspace.networking", content), \
            "tailscaled must use --tun=userspace-networking (no TUN fd required)"

    def test_ssl_cert_file_env(self):
        """SSL_CERT_FILE must be set so tailscaled trusts the Caddy root CA."""
        content = _read(PROBE_SCRIPT)
        assert re.search(r"SSL_CERT_FILE", content), \
            "SSL_CERT_FILE must be exported so tailscaled trusts the mounted CA"

    def test_socks5_proxy_wait(self):
        """Script must wait for SOCKS5 proxy to be available before curling."""
        content = _read(PROBE_SCRIPT)
        assert re.search(r"socks5?|socks-proxy|SOCKS", content, re.IGNORECASE), \
            "Probe must use SOCKS5 proxy from tailscaled for curl (--socks5 or similar)"

    def test_json_summary_output(self):
        """Probe must produce JSON summary output."""
        content = _read(PROBE_SCRIPT)
        assert re.search(r'json|JSON|\{.*"result"|echo.*\{', content, re.IGNORECASE), \
            "headscale_probe.sh must emit a JSON summary"

    def test_cleanup_trap(self):
        """Script must have a cleanup trap to stop tailscaled on exit."""
        content = _read(PROBE_SCRIPT)
        assert re.search(r"\btrap\b.*(?:EXIT|SIGTERM|INT)", content), \
            "headscale_probe.sh must have a trap for cleanup on exit"

    def test_set_euo_pipefail(self):
        """set -euo pipefail (or equivalent) must appear for robustness."""
        content = _read(PROBE_SCRIPT)
        assert re.search(
            r"set\s+.*-[a-zA-Z]*e[a-zA-Z]*|set\s+-o\s+errexit", content
        ), "set -e / set -o errexit not found in headscale_probe.sh"
        assert re.search(
            r"set\s+.*-[a-zA-Z]*u[a-zA-Z]*|set\s+-o\s+nounset", content
        ), "set -u / set -o nounset not found in headscale_probe.sh"

    def test_auth_key_file_readable_check(self):
        """Script must verify auth-key file is readable before use."""
        content = _read(PROBE_SCRIPT)
        # Accept any of: [ -r "$AUTH_KEY_FILE" ] OR [ ! -r ... ] OR test -r ...
        assert re.search(
            r'\[\s*!?\s*-[rf]\s+["\$\{]*AUTH_KEY_FILE',
            content
        ), "headscale_probe.sh must check that the auth-key file is readable"

    def test_ca_file_readable_check(self):
        """Script must verify CA cert file is readable before use."""
        content = _read(PROBE_SCRIPT)
        assert re.search(
            r'\[\s*!?\s*-[rf]\s+["\$\{]*CA_FILE',
            content
        ), "headscale_probe.sh must check that the CA cert file is readable"

    def test_headscale_url_referenced(self):
        """Probe must reference the Headscale URL or login server."""
        content = _read(PROBE_SCRIPT)
        assert re.search(
            r"143\.198\.112\.69\.sslip\.io|HEADSCALE_URL|LOGIN_SERVER",
            content
        ), "Headscale URL / login server not found in headscale_probe.sh"

    def test_ping_wesley_ip(self):
        """Probe must ping WESLEY_IP (default 100.64.0.2) over Tailscale."""
        content = _read(PROBE_SCRIPT)
        assert re.search(r"100\.64\.0\.2|WESLEY_IP", content), \
            "Probe must ping WESLEY_IP (default 100.64.0.2)"

    def test_curl_through_socks(self):
        """Probe must curl WESLEY_URL through the SOCKS proxy."""
        content = _read(PROBE_SCRIPT)
        assert re.search(r"WESLEY_URL|--socks5", content), \
            "Probe must curl WESLEY_URL through SOCKS (--socks5 ...)"

    def test_no_secret_in_log(self):
        """No bare $AUTHKEY or auth key literal must appear in any echo/log."""
        content = _read(PROBE_SCRIPT)
        # Acceptable: auth-key=file:..., but NOT: echo "$AUTHKEY" or similar
        assert not re.search(r'echo\s+["\$]*AUTH_KEY|log.*AUTH_KEY\b', content), \
            "Auth key must never be echoed/logged — use file: reference only"

    def test_env_proxy_points_to_local_rewrite_proxy(self):
        """tailscaled's http_proxy must point to the local connect_proxy port."""
        content = _read(PROBE_SCRIPT)
        # Should set http_proxy/HTTP_PROXY to 127.0.0.1:... for tailscaled
        assert re.search(r"http_proxy.*127\.0\.0\.1|HTTP_PROXY.*127\.0\.0\.1", content), \
            "tailscaled http_proxy must point to local connect_proxy (127.0.0.1:...)"


# ============================================================================
# SECTION 5: Dockerfile.headscale-probe — static analysis
# ============================================================================

class TestDockerfileHeadscaleProbe:
    """Static checks on Dockerfile.headscale-probe."""

    def test_pinned_tailscale_image(self):
        """Must pin tailscale/tailscale:v1.88.3 with a sha256 digest."""
        content = _read(DOCKERFILE)
        # Require at minimum the tag pin; digest pin is strongly preferred
        assert re.search(r"tailscale/tailscale:v1\.88\.3", content), \
            "Dockerfile.headscale-probe must use tailscale/tailscale:v1.88.3"
        assert re.search(r"@sha256:", content), \
            "Dockerfile.headscale-probe must pin by sha256 digest"

    def test_runs_as_uid_10000(self):
        """Final USER must be 10000 (non-root)."""
        content = _read(DOCKERFILE)
        assert re.search(r"^USER\s+10000", content, re.MULTILINE), \
            "Dockerfile.headscale-probe must set USER 10000"

    def test_installs_curl_and_ca_certificates(self):
        """curl and ca-certificates must be installed deterministically."""
        content = _read(DOCKERFILE)
        assert re.search(r"\bcurl\b", content), \
            "Dockerfile.headscale-probe must install curl"
        assert re.search(r"ca-certificates", content), \
            "Dockerfile.headscale-probe must install ca-certificates"

    def test_copies_connect_proxy(self):
        """connect_proxy.py must be COPYed into the image."""
        content = _read(DOCKERFILE)
        assert re.search(r"COPY.*connect_proxy\.py", content), \
            "Dockerfile.headscale-probe must COPY scripts/connect_proxy.py"

    def test_copies_probe_script(self):
        """headscale_probe.sh must be COPYed into the image."""
        content = _read(DOCKERFILE)
        assert re.search(r"COPY.*headscale_probe\.sh", content), \
            "Dockerfile.headscale-probe must COPY scripts/headscale_probe.sh"

    def test_no_privileged_in_dockerfile(self):
        """Dockerfile must not grant privileged capabilities via --cap-add."""
        content = _read(DOCKERFILE)
        assert not re.search(r"--privileged", content), \
            "Dockerfile.headscale-probe must not use --privileged"
        # Reject explicit --cap-add for dangerous caps (comments are fine to mention them)
        assert not re.search(r"--cap-add\s*(?:=\s*)?NET_ADMIN", content), \
            "Dockerfile must not grant NET_ADMIN capability via --cap-add"
        assert not re.search(r"--cap-add\s*(?:=\s*)?SYS_MODULE", content), \
            "Dockerfile must not grant SYS_MODULE capability via --cap-add"

    def test_no_insecure_curl_in_dockerfile(self):
        """Dockerfile RUN lines must not use curl -k."""
        content = _read(DOCKERFILE)
        assert not re.search(r"\bcurl\b[^\n]*(-k\b|--insecure)", content), \
            "Dockerfile RUN curl must not use -k / --insecure"

    def test_env_ssl_cert_file_or_documented(self):
        """SSL_CERT_FILE must appear in Dockerfile (ENV or LABEL/comment)."""
        content = _read(DOCKERFILE)
        assert re.search(r"SSL_CERT_FILE", content), \
            "Dockerfile.headscale-probe must declare SSL_CERT_FILE ENV or reference it"

    def test_based_on_tailscale_image(self):
        """Must be based on the tailscale image (FROM ... tailscale ...)."""
        content = _read(DOCKERFILE)
        assert re.search(r"FROM.*tailscale", content, re.IGNORECASE), \
            "Dockerfile.headscale-probe must use tailscale image as base"

    def test_workdir_set(self):
        """WORKDIR should be set explicitly."""
        content = _read(DOCKERFILE)
        assert re.search(r"^WORKDIR\s+\S+", content, re.MULTILINE), \
            "Dockerfile.headscale-probe should set WORKDIR"

    def test_exposes_no_privileged_ports(self):
        """Any EXPOSEd ports must be >= 1024 (non-root constraint)."""
        content = _read(DOCKERFILE)
        for m in re.finditer(r"^EXPOSE\s+(\d+)", content, re.MULTILINE):
            port = int(m.group(1))
            assert port >= 1024, (
                f"Exposed port {port} is privileged (<1024). "
                "Non-root uid 10000 cannot bind privileged ports."
            )


# ============================================================================
# SECTION 6: .github/workflows/build.yml — headscale-probe job
# ============================================================================

class TestBuildYMLHeadscaleProbeJob:
    """The GitHub Actions workflow must include a job for the headscale-probe image."""

    def test_headscale_probe_image_reference(self):
        """build.yml must reference the headscale-probe image name."""
        content = _read(BUILD_YML)
        assert re.search(r"headscale.probe", content), \
            "build.yml must reference 'headscale-probe' image"

    def test_ghcr_image_path(self):
        """Image must be published to ghcr.io/.../alcf-agent-headscale-probe."""
        content = _read(BUILD_YML)
        assert re.search(r"alcf-agent-headscale-probe", content), \
            "build.yml must publish ghcr.io/.../alcf-agent-headscale-probe"

    def test_uses_dockerfile_headscale_probe(self):
        """Build step must reference Dockerfile.headscale-probe."""
        content = _read(BUILD_YML)
        assert re.search(r"Dockerfile\.headscale-probe", content), \
            "build.yml must build from Dockerfile.headscale-probe"

    def test_multiplatform_amd64_arm64(self):
        """Must build linux/amd64 and linux/arm64."""
        content = _read(BUILD_YML)
        assert re.search(r"linux/amd64", content), \
            "build.yml must build linux/amd64"
        assert re.search(r"linux/arm64", content), \
            "build.yml must build linux/arm64"

    def test_sha_tag(self):
        """Image must be tagged with at least a short SHA tag."""
        content = _read(BUILD_YML)
        assert re.search(r"type=sha", content), \
            "build.yml must tag headscale-probe image with a SHA tag"

    def test_does_not_push_on_schedule_only(self):
        """Workflow must push (not just build) on push/tag triggers."""
        content = _read(BUILD_YML)
        assert re.search(r"push:\s*true", content), \
            "build.yml must push the headscale-probe image (push: true)"
