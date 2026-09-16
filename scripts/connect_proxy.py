#!/usr/bin/env python3
"""
connect_proxy.py — fail-closed local CONNECT rewrite proxy.

Listens on 127.0.0.1 (loopback only). Accepts ONLY the HTTP CONNECT method.
Rewrites EXACTLY the Headscale sslip.io authority to its numeric IP before
forwarding to the upstream ALCF proxy.  All other destinations are rejected
with 403 Forbidden.

TLS bytes are tunnelled as-is (no ssl module — end-to-end TLS preserved).

Usage:
    python3 connect_proxy.py [--listen-port PORT] [--upstream HOST:PORT]

Environment:
    UPSTREAM_PROXY   http://host:port of the upstream (ALCF) proxy.
                     Overridden by --upstream flag if provided.

Design notes:
  • Fail-closed: unknown destinations → 403 (never forwarded).
  • CONNECT-only: any other HTTP method → 405 Method Not Allowed.
  • No ssl module used; TLS remains end-to-end with original SNI intact.
  • No third-party dependencies; stdlib only.
  • Listen address is always 127.0.0.1 (never 0.0.0.0).
"""
from __future__ import annotations

import argparse
import logging
import os
import re
import select
import signal
import socket
import sys
import threading

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

# The ONLY authority that this proxy will forward.  Requests targeting any
# other host are rejected with 403 Forbidden (fail-closed).
HEADSCALE_AUTHORITY   = "143.198.112.69.sslip.io:443"
HEADSCALE_REWRITE_TO  = "143.198.112.69:443"

LISTEN_HOST    = "127.0.0.1"
DEFAULT_PORT   = 18443
BUFSIZE        = 65536
CONN_TIMEOUT   = 15  # seconds to establish upstream connection

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s connect_proxy %(levelname)s %(message)s",
    stream=sys.stderr,
)
log = logging.getLogger("connect_proxy")


# ---------------------------------------------------------------------------
# CONNECT request parser
# ---------------------------------------------------------------------------

def _parse_connect(raw: bytes) -> tuple[str, str] | None:
    """
    Parse the first line of a CONNECT request.

    Returns (method, authority) or None if parsing fails.
    The caller is responsible for verifying method == 'CONNECT'.
    """
    try:
        header_block = raw.split(b"\r\n\r\n")[0].decode("latin-1")
    except Exception:
        return None
    first_line = header_block.splitlines()[0]
    parts = first_line.split()
    if len(parts) < 2:
        return None
    return parts[0].upper(), parts[1]


# ---------------------------------------------------------------------------
# Tunnel helper
# ---------------------------------------------------------------------------

def _tunnel(client: socket.socket, upstream: socket.socket) -> None:
    """Bidirectionally relay bytes until either socket closes."""
    sockets = [client, upstream]
    while True:
        try:
            r, _, _ = select.select(sockets, [], sockets, 30)
        except Exception:
            break
        if not r:
            break
        for s in r:
            other = upstream if s is client else client
            try:
                data = s.recv(BUFSIZE)
            except OSError:
                data = b""
            if not data:
                return
            try:
                other.sendall(data)
            except OSError:
                return


# ---------------------------------------------------------------------------
# Per-connection handler
# ---------------------------------------------------------------------------

def _handle_client(conn: socket.socket, addr: tuple, upstream_host: str,
                   upstream_port: int) -> None:
    try:
        conn.settimeout(CONN_TIMEOUT)
        raw = b""
        while b"\r\n\r\n" not in raw:
            chunk = conn.recv(BUFSIZE)
            if not chunk:
                return
            raw += chunk

        parsed = _parse_connect(raw)
        if parsed is None:
            conn.sendall(b"HTTP/1.1 400 Bad Request\r\n\r\n")
            return

        method, authority = parsed

        if method != "CONNECT":
            # Only CONNECT is accepted — reject everything else.
            log.warning("Rejected non-CONNECT method: %s from %s", method, addr[0])
            conn.sendall(
                b"HTTP/1.1 405 Method Not Allowed\r\n"
                b"Allow: CONNECT\r\n"
                b"\r\n"
            )
            return

        # Fail-closed: only allow the exact Headscale authority.
        if authority != HEADSCALE_AUTHORITY:
            log.warning(
                "Blocked CONNECT to non-whitelisted authority: %s from %s",
                authority, addr[0],
            )
            conn.sendall(
                b"HTTP/1.1 403 Forbidden\r\n"
                b"X-Reason: destination not in allowlist\r\n"
                b"\r\n"
            )
            return

        # Rewrite the authority to the numeric IP (bypass sslip.io sinkhole).
        rewritten_authority = HEADSCALE_REWRITE_TO
        log.info("CONNECT %s -> rewrite -> CONNECT %s (upstream %s:%d)",
                 authority, rewritten_authority, upstream_host, upstream_port)

        # Connect to the upstream ALCF proxy.
        try:
            up = socket.create_connection(
                (upstream_host, upstream_port), timeout=CONN_TIMEOUT
            )
        except OSError as exc:
            log.error("Cannot reach upstream proxy %s:%d: %s",
                      upstream_host, upstream_port, exc)
            conn.sendall(b"HTTP/1.1 502 Bad Gateway\r\n\r\n")
            return

        # Forward rewritten CONNECT to upstream.
        connect_req = (
            f"CONNECT {rewritten_authority} HTTP/1.1\r\n"
            f"Host: {rewritten_authority}\r\n"
            f"\r\n"
        ).encode()
        try:
            up.sendall(connect_req)
        except OSError as exc:
            log.error("Failed to send CONNECT to upstream: %s", exc)
            conn.sendall(b"HTTP/1.1 502 Bad Gateway\r\n\r\n")
            up.close()
            return

        # Read upstream's response to our CONNECT.
        up_resp = b""
        try:
            up.settimeout(CONN_TIMEOUT)
            while b"\r\n\r\n" not in up_resp:
                chunk = up.recv(BUFSIZE)
                if not chunk:
                    break
                up_resp += chunk
        except OSError as exc:
            log.error("Upstream response error: %s", exc)
            conn.sendall(b"HTTP/1.1 502 Bad Gateway\r\n\r\n")
            up.close()
            return

        first_line = up_resp.split(b"\r\n")[0].decode("latin-1", errors="replace")
        if not re.search(r"^HTTP/\d+\.\d+\s+2\d\d", first_line):
            log.warning("Upstream rejected CONNECT: %s", first_line)
            conn.sendall(b"HTTP/1.1 502 Bad Gateway\r\n\r\n")
            up.close()
            return

        # Signal success to the client, then tunnel TLS bytes transparently.
        conn.sendall(b"HTTP/1.1 200 Connection established\r\n\r\n")
        _tunnel(conn, up)

    except Exception as exc:  # pylint: disable=broad-except
        log.exception("Unhandled error in handler: %s", exc)
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Main server loop
# ---------------------------------------------------------------------------

def _parse_upstream(upstream_str: str) -> tuple[str, int]:
    """Parse 'host:port' or 'http://host:port' into (host, port)."""
    # Strip scheme if present
    s = re.sub(r"^https?://", "", upstream_str)
    # Remove trailing slash
    s = s.rstrip("/")
    if ":" in s:
        host, port_str = s.rsplit(":", 1)
        return host, int(port_str)
    return s, 3128  # default ALCF proxy port


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(
        description="Fail-closed CONNECT rewrite proxy for Headscale/Polaris"
    )
    parser.add_argument(
        "--listen-port", type=int, default=DEFAULT_PORT,
        help=f"Local port to listen on (default {DEFAULT_PORT})"
    )
    parser.add_argument(
        "--upstream", default=None,
        help="Upstream proxy as host:port (overrides UPSTREAM_PROXY env var)"
    )
    args = parser.parse_args(argv)

    # Determine upstream from flag or env var
    upstream_raw = args.upstream or os.environ.get("UPSTREAM_PROXY", "proxy.alcf.anl.gov:3128")
    upstream_host, upstream_port = _parse_upstream(upstream_raw)

    log.info("Starting connect_proxy on %s:%d → upstream %s:%d",
             LISTEN_HOST, args.listen_port, upstream_host, upstream_port)
    log.info("Allowlist: CONNECT %s → %s", HEADSCALE_AUTHORITY, HEADSCALE_REWRITE_TO)

    server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    server.bind((LISTEN_HOST, args.listen_port))
    server.listen(16)

    def _shutdown(sig, frame):  # noqa: ANN001
        log.info("Shutting down (signal %d)", sig)
        server.close()
        sys.exit(0)

    signal.signal(signal.SIGTERM, _shutdown)
    signal.signal(signal.SIGINT,  _shutdown)

    while True:
        try:
            conn, addr = server.accept()
        except OSError:
            break
        t = threading.Thread(
            target=_handle_client,
            args=(conn, addr, upstream_host, upstream_port),
            daemon=True,
        )
        t.start()


if __name__ == "__main__":
    main()
