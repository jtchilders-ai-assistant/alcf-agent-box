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
import json
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

    def test_rejects_malformed_authority(self):
        """Proxy must reject malformed CONNECT authorities (no host:port)."""
        content = _read(CONNECT_PROXY)
        # Should have a 400 or 403 response for malformed authorities
        assert re.search(r"400|bad request|malformed", content, re.IGNORECASE), \
            "connect_proxy.py must return 400 Bad Request for malformed authorities"

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

    def test_non_sslip_connect_forwarded_unchanged_original_section(self, fake_upstream):
        """
        CONNECT to any valid host:port OTHER than the sslip.io authority
        must be forwarded unchanged to upstream (not rejected).
        This ensures Tailscale DERP/control traffic flows through.
        """
        listen_port = _free_port()
        proc = _start_connect_proxy(listen_port, "127.0.0.1", fake_upstream.port)
        try:
            status_line, _ = _send_connect(
                listen_port, "example.com:443"
            )
            assert "200" in status_line, (
                f"Expected 200 for example.com:443 (forwarded unchanged), got: {status_line!r}"
            )
            time.sleep(0.1)
            assert fake_upstream.received_connect_lines, (
                "Fake upstream must receive a CONNECT for non-sslip authority — "
                "proxy must forward (not reject) valid CONNECTs."
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


# ============================================================================
# SECTION 7: Task-2 spec blockers — TDD additions
# ============================================================================

class TestConnectProxyForwardAll:
    """
    connect_proxy.py must forward ANY syntactically valid CONNECT authority
    unchanged to upstream, rewriting ONLY the exact Headscale sslip.io authority.
    (Design change from fail-closed to pass-through for non-sslip authorities.)
    """

    def test_non_sslip_connect_forwarded_unchanged(self, fake_upstream):
        """
        CONNECT derp1.tailscale.com:443 must be forwarded to upstream AS-IS
        (not rejected with 403).  Tailscale DERP/control must flow through.
        """
        listen_port = _free_port()
        proc = _start_connect_proxy(listen_port, "127.0.0.1", fake_upstream.port)
        try:
            status_line, _ = _send_connect(listen_port, "derp1.tailscale.com:443")
            assert "200" in status_line, (
                f"Expected 200 for derp1.tailscale.com:443 (should be forwarded "
                f"unchanged), got: {status_line!r}"
            )
            time.sleep(0.1)
            assert fake_upstream.received_connect_lines, (
                "Fake upstream never received a CONNECT for derp1.tailscale.com:443 "
                "— proxy must forward non-sslip CONNECTs unchanged."
            )
            sent = fake_upstream.received_connect_lines[0]
            assert "derp1.tailscale.com:443" in sent, (
                f"Upstream must receive original authority unchanged. Got: {sent!r}"
            )
        finally:
            proc.terminate()
            proc.wait(timeout=5)

    def test_arbitrary_valid_connect_forwarded(self, fake_upstream):
        """
        CONNECT example.com:8443 (any valid host:port) must be forwarded unchanged.
        """
        listen_port = _free_port()
        proc = _start_connect_proxy(listen_port, "127.0.0.1", fake_upstream.port)
        try:
            status_line, _ = _send_connect(listen_port, "example.com:8443")
            assert "200" in status_line, (
                f"Expected 200 for example.com:8443 (forwarded unchanged), "
                f"got: {status_line!r}"
            )
            time.sleep(0.1)
            assert fake_upstream.received_connect_lines, (
                "Fake upstream never received CONNECT for example.com:8443"
            )
            sent = fake_upstream.received_connect_lines[0]
            assert "example.com:8443" in sent, (
                f"Upstream must receive original authority unchanged. Got: {sent!r}"
            )
        finally:
            proc.terminate()
            proc.wait(timeout=5)

    def test_malformed_authority_still_rejected(self, fake_upstream):
        """
        A malformed authority (no colon+port, e.g. 'notahost') must be rejected
        with 400 or 403 (not forwarded to upstream).
        """
        listen_port = _free_port()
        proc = _start_connect_proxy(listen_port, "127.0.0.1", fake_upstream.port)
        try:
            status_line, _ = _send_connect(listen_port, "notahost")
            assert "400" in status_line or "403" in status_line, (
                f"Expected 400/403 for malformed authority 'notahost', got: {status_line!r}"
            )
            time.sleep(0.1)
            assert not fake_upstream.received_connect_lines, (
                "Malformed authority must never reach the upstream proxy."
            )
        finally:
            proc.terminate()
            proc.wait(timeout=5)

    def test_headscale_sslip_still_rewritten_to_numeric_ip(self, fake_upstream):
        """
        CONNECT 143.198.112.69.sslip.io:443 must still be rewritten to
        CONNECT 143.198.112.69:443 at the upstream (unchanged from original design).
        """
        listen_port = _free_port()
        proc = _start_connect_proxy(listen_port, "127.0.0.1", fake_upstream.port)
        try:
            status_line, _ = _send_connect(
                listen_port, f"{HEADSCALE_HOST}:{HEADSCALE_PORT}"
            )
            assert "200" in status_line, (
                f"Expected 200 for Headscale CONNECT, got: {status_line!r}"
            )
            time.sleep(0.1)
            assert fake_upstream.received_connect_lines, \
                "Fake upstream never received a CONNECT for Headscale"
            sent = fake_upstream.received_connect_lines[0]
            assert HEADSCALE_IP + ":443" in sent, (
                f"Headscale sslip.io must be rewritten to numeric IP at upstream. "
                f"Got: {sent!r}"
            )
            assert HEADSCALE_HOST not in sent, (
                f"sslip.io hostname must not reach upstream (must be rewritten). "
                f"Got: {sent!r}"
            )
        finally:
            proc.terminate()
            proc.wait(timeout=5)


class TestWesleyUrlDefault:
    """WESLEY_URL default must be http://100.64.0.2:8642/health (includes port+path)."""

    def test_wesley_url_default_includes_port_and_health_path(self):
        """
        WESLEY_URL default must be 'http://100.64.0.2:8642/health'.
        Plain 'http://100.64.0.2/' is wrong — no port and no /health endpoint.
        """
        content = _read(PROBE_SCRIPT)
        assert re.search(
            r'WESLEY_URL.*100\.64\.0\.2:8642/health',
            content
        ), (
            "WESLEY_URL default must be 'http://100.64.0.2:8642/health' "
            f"(includes port 8642 and /health path). "
            "Found instead: "
            + (re.search(r'WESLEY_URL=.*', content) or type('', (), {'group': lambda s, n: 'NOT FOUND'})()).group(0)  # type: ignore[attr-defined]
        )


class TestHeadscaleTlsValidation:
    """
    Probe must perform an explicit Headscale TLS pre-join check:
    curl --cacert CA_FILE through the local rewrite proxy, requiring HTTP 200
    from HEADSCALE_URL/health.  Must NOT use -k/--insecure.
    """

    def test_headscale_health_check_present(self):
        """Script must explicitly curl HEADSCALE_URL/health before joining."""
        content = _read(PROBE_SCRIPT)
        # The curl command is multiline (backslash continuation), so check for
        # HEADSCALE_URL/health as a URL argument appearing near a curl block.
        assert re.search(
            r'HEADSCALE_URL\}/health|HEADSCALE_URL.*health|headscale.*health',
            content, re.IGNORECASE
        ) and re.search(r'\bcurl\b', content), (
            "headscale_probe.sh must curl HEADSCALE_URL/health as a pre-join "
            "TLS validation step."
        )

    def test_headscale_health_check_uses_cacert(self):
        """The Headscale health check curl must use --cacert CA_FILE (no -k)."""
        content = _read(PROBE_SCRIPT)
        # The curl is multiline — check that both --cacert and HEADSCALE_URL/health
        # appear in the same logical command block (within 20 lines of each other).
        # Use a re.DOTALL block search for the curl command that hits /health.
        assert re.search(
            r'curl\s*\\[^}]*--cacert[^}]*HEADSCALE_URL\}/health|'
            r'curl\s*\\[^Z]*HEADSCALE_URL\}/health[^Z]*--cacert',
            content, re.DOTALL
        ) or (
            re.search(r'--cacert', content) and
            re.search(r'HEADSCALE_URL\}/health', content)
        ), (
            "The Headscale health check curl must use --cacert CA_FILE, not -k."
        )

    def test_headscale_health_check_http200_required(self):
        """Health check must verify HTTP 200 response."""
        content = _read(PROBE_SCRIPT)
        assert re.search(
            r'200|http_code.*headscale|headscale.*http_code',
            content, re.IGNORECASE
        ), (
            "Headscale TLS health check must verify HTTP 200 response."
        )

    def test_headscale_health_result_recorded(self):
        """Health check result must be recorded in JSON output (_result call)."""
        content = _read(PROBE_SCRIPT)
        assert re.search(
            r'_result.*headscale|headscale.*_result',
            content, re.IGNORECASE
        ), (
            "Headscale health check result must be recorded via _result function."
        )

    def test_headscale_health_check_through_proxy(self):
        """The health check curl must route through the local rewrite proxy."""
        content = _read(PROBE_SCRIPT)
        # Look for --proxy pointing to 127.0.0.1 near the headscale health curl
        assert re.search(
            r'--proxy[^\n]*127\.0\.0\.1|proxy.*headscale.*health|headscale.*health.*proxy',
            content, re.IGNORECASE
        ), (
            "Headscale health check curl must use --proxy 127.0.0.1:CONNECT_PROXY_PORT "
            "so TLS goes through the local rewrite proxy."
        )

    def test_tailscale_up_requires_zero_tls_curl_rc_and_http_200(self):
        """A stale/partial HTTP 200 must not bypass a failed TLS curl."""
        content = _read(PROBE_SCRIPT)
        assert re.search(
            r'if\s+\[\s+"\$\{SOCKET_READY\}"\s+=\s+"true"\s+\]\s+&&\s+'
            r'\[\s+"\$\{HS_CURL_RC\}"\s+-eq\s+0\s+\]\s+&&\s+'
            r'\[\s+"\$\{HS_HTTP_CODE\}"\s+=\s+"200"\s+\]',
            content,
        ), "tailscale up must require socket ready, curl rc=0, and HTTP 200"


class TestProxyReadinessCheck:
    """
    After the wait loop, if the connect_proxy never bound, the script must
    record a failure result and exit rather than blindly continuing.
    """

    def test_proxy_readiness_failure_recorded(self):
        """If proxy never binds, _result must record failure before exit."""
        content = _read(PROBE_SCRIPT)
        assert re.search(
            r'PROXY_READY|proxy.*never|connect_proxy.*fail|proxy.*bind.*fail',
            content, re.IGNORECASE
        ), (
            "headscale_probe.sh must check proxy readiness after the wait loop "
            "and record/emit failure if it never bound."
        )

    def test_proxy_readiness_exits_on_failure(self):
        """If proxy never binds, script must exit (not continue blindly)."""
        content = _read(PROBE_SCRIPT)
        # Must have conditional logic: if not ready → exit or skip all further steps
        assert re.search(
            r'PROXY_READY.*=.*false|PROXY_READY.*=.*true',
            content
        ) or re.search(
            r'proxy.*never.*bind|connect_proxy.*not.*start',
            content, re.IGNORECASE
        ), (
            "headscale_probe.sh must track proxy readiness state and gate "
            "further steps on it."
        )


class TestTailscaleUpSanitization:
    """
    tailscale up output must be suppressed (could expose registration URLs).
    Only success/failure must be recorded, never raw output.
    """

    def test_tailscale_up_stdout_not_printed(self):
        """tailscale up stdout must be redirected (>/dev/null or to variable)."""
        content = _read(PROBE_SCRIPT)
        # tailscale up is a multiline backslash-continued command; the redirect
        # >/dev/null appears on its own continuation line.
        # Verify: (a) tailscale up invocation exists, (b) >/dev/null 2>/dev/null
        # appears in the same command block (DOTALL search for the up section).
        assert re.search(r'timeout[^\n]+"?\$\{TS_UP_TIMEOUT\}[^\n]*tailscale|tailscale[^\n]*--hostname', content), (
            "tailscale up invocation not found in headscale_probe.sh"
        )
        assert re.search(r'>/dev/null', content), (
            "tailscale up stdout must be redirected (>/dev/null) — "
            "it could expose registration URLs or secrets."
        )
        # Verify the redirect appears within the tailscale up command block
        assert re.search(
            r'(?:timeout[^#]*?tailscale|tailscale[^#]*?--hostname)[^#]*?>/dev/null',
            content, re.DOTALL
        ), (
            "tailscale up stdout redirect (>/dev/null) must be part of the "
            "tailscale up command block, not elsewhere."
        )

    def test_tailscale_up_stderr_not_printed(self):
        """tailscale up stderr must not go to stdout/terminal unfiltered."""
        content = _read(PROBE_SCRIPT)
        # Must NOT have bare '2>&1' without also redirecting to /dev/null
        # 2>&1 alone sends both to terminal; acceptable: 2>/dev/null or 2>&1 >/dev/null
        has_bare_2and1 = bool(re.search(
            r'tailscale[^\n]*\bup\b[^\n]*2>&1(?![^\n]*>/dev/null)',
            content
        ))
        assert not has_bare_2and1, (
            "tailscale up must not use bare '2>&1' (exposes stderr). "
            "Use '>/dev/null 2>&1' or '2>/dev/null'."
        )


class TestJsonConstructedWithPython:
    """
    Final JSON must be constructed with Python (json.dumps) to handle paths
    with quotes/backslashes safely, not shell string interpolation.
    """

    def test_json_output_uses_python(self):
        """JSON emission must use Python's json module, not shell echo interpolation."""
        content = _read(PROBE_SCRIPT)
        assert re.search(
            r'python3[^\n]*json|import json|json\.dumps|json\.loads',
            content, re.IGNORECASE
        ), (
            "headscale_probe.sh must use Python to construct/emit JSON output "
            "to handle paths with special characters safely."
        )

    def test_no_raw_shell_json_interpolation(self):
        """Shell echo with raw variable interpolation in JSON must not be used."""
        content = _read(PROBE_SCRIPT)
        # Check for echo/printf constructing JSON with unescaped variable expansion
        # Pattern: echo "..." with ${VAR} inside JSON object (dangerous)
        dangerous = re.findall(
            r'echo\s+"[^"]*\$\{[A-Z_]+\}[^"]*"[^"]*\}',
            content
        )
        assert not dangerous, (
            f"Shell echo JSON interpolation found (unsafe for special chars): "
            f"{dangerous[:3]}"
        )


# ============================================================================
# SECTION 8: Additional quality / correctness tests
# ============================================================================

class TestConnectProxyQuality:
    """connect_proxy.py quality and correctness properties."""

    def test_header_size_cap(self):
        """connect_proxy.py must enforce a max header size to prevent unbounded reads."""
        content = _read(CONNECT_PROXY)
        # Should have a named cap constant and enforcement in the read loop
        assert re.search(
            r"MAX_HEADER_SIZE|max_header|len\(raw\).*>",
            content
        ), (
            "connect_proxy.py must cap incoming CONNECT header size "
            "to prevent unbounded memory growth."
        )

    def test_upstream_socket_closed_on_all_paths(self):
        """Upstream socket must be closed (or context-managed) on every exit path."""
        content = _read(CONNECT_PROXY)
        import ast
        tree = ast.parse(content)
        # Must use try/finally or context manager on upstream socket
        has_finally_or_with = bool(
            re.search(r"up\.close\(\)|with socket", content)
        )
        assert has_finally_or_with, (
            "connect_proxy.py must explicitly close the upstream socket (up.close()) "
            "on every code path to prevent fd leaks."
        )

    def test_malformed_authority_rejected_live(self, fake_upstream):
        """Live: CONNECT with no port must be rejected 400, never forwarded."""
        listen_port = _free_port()
        proc = _start_connect_proxy(listen_port, "127.0.0.1", fake_upstream.port)
        try:
            status_line, _ = _send_connect(listen_port, "justahostname")
            assert "400" in status_line or "403" in status_line, (
                f"Expected 400/403 for no-port authority, got: {status_line!r}"
            )
            time.sleep(0.1)
            assert not fake_upstream.received_connect_lines, (
                "Malformed authority (no port) must not reach upstream."
            )
        finally:
            proc.terminate()
            proc.wait(timeout=5)

    def test_proxy_uses_python_socket_readiness(self):
        """headscale_probe.sh must use an explicit portable socket readiness check."""
        content = _read(PROBE_SCRIPT)
        assert "socket.connect_ex" in content, (
            "headscale_probe.sh must use Python socket.connect_ex for the "
            "portable proxy readiness check."
        )

    def test_missing_auth_key_emits_valid_json(self):
        """A missing auth-key file must return JSON, not a Python KeyError."""
        env = os.environ.copy()
        env.update({
            "AUTH_KEY_FILE": '/tmp/missing-auth-"-file',
            "CA_FILE": "/tmp/missing-ca-file",
        })
        proc = subprocess.run(
            ["bash", PROBE_SCRIPT],
            env=env,
            text=True,
            capture_output=True,
            timeout=10,
        )
        assert proc.returncode == 1
        payload = json.loads(proc.stderr)
        assert payload == {
            "error": "AUTH_KEY_FILE not readable",
            "file": '/tmp/missing-auth-"-file',
        }

    def test_missing_default_auth_key_emits_valid_json(self):
        """Shell defaults need not be exported for error JSON to work."""
        env = os.environ.copy()
        env.pop("AUTH_KEY_FILE", None)
        env.pop("CA_FILE", None)
        proc = subprocess.run(
            ["bash", PROBE_SCRIPT],
            env=env,
            text=True,
            capture_output=True,
            timeout=10,
        )
        assert proc.returncode == 1
        payload = json.loads(proc.stderr)
        assert payload["error"] == "AUTH_KEY_FILE not readable"
        assert payload["file"] == "/run/secrets/headscale-auth-key"

    def test_result_serialization_does_not_add_double_dash_element(self):
        """The shell-to-Python array bridge must not serialize a literal '--'."""
        content = _read(PROBE_SCRIPT)
        assert not re.search(
            r"json\.dumps\(sys\.argv\[1:\]\).*\s--\s",
            content,
        ), "A literal '--' argument becomes a phantom failed result"

    def test_upstream_socket_has_unconditional_finally_close(self):
        """The connected upstream socket must close after tunnel return/errors."""
        content = _read(CONNECT_PROXY)
        assert re.search(
            r"finally:\s*\n(?:\s+if up is not None:\s*\n)?\s+up\.close\(\)",
            content,
        ), "Upstream socket needs an unconditional finally close"


class TestTailscaleUpTimeout:
    """tailscale up must have an explicit timeout to prevent hanging indefinitely."""

    def test_tailscale_up_has_timeout(self):
        """tailscale up must use --timeout or be wrapped in a timeout command."""
        content = _read(PROBE_SCRIPT)
        assert re.search(
            r"timeout\s+\d+\s+tailscale|tailscale[^\n]*--timeout|"
            r"timeout\s+\"\$\{[A-Z_]+\}\"[^\n]*tailscale",
            content
        ), (
            "tailscale up must have an explicit timeout (timeout N tailscale up ...) "
            "to avoid hanging the probe container indefinitely."
        )
