#!/usr/bin/env python3
"""Patch pinned Hermes A2A client for an exact-authority HTTP proxy.

This is intentionally fail-closed and pinned to the v2026.9.14 source shape.
It aborts if any expected source fragment is absent or non-unique.
"""
from pathlib import Path
import sys

path = Path(sys.argv[1] if len(sys.argv) > 1 else "/opt/hermes/plugins/platforms/a2a/tools.py")
text = path.read_text(encoding="utf-8")

replacements = [
    (
        "import urllib.error\nimport urllib.request\n",
        "import urllib.error\nimport urllib.parse\nimport urllib.request\n",
    ),
    (
        'return {"url": entry.get("url", ""), "auth": entry.get("auth", {}) or {},\n'
        '            "timeout": int(entry.get("timeout", _DEFAULT_TIMEOUT)), **extra}\n',
        'return {"url": entry.get("url", ""), "auth": entry.get("auth", {}) or {},\n'
        '            "timeout": int(entry.get("timeout", _DEFAULT_TIMEOUT)),\n'
        '            "proxy": entry.get("proxy", ""),\n'
        '            "proxy_authority": entry.get("proxy_authority", ""), **extra}\n',
    ),
    (
        'def _http_json(url: str, headers: dict, timeout: int, method: str, data: Optional[bytes] = None) -> dict:\n'
        '    req = urllib.request.Request(url, data=data, headers=headers, method=method)\n'
        '    with urllib.request.urlopen(req, timeout=timeout) as resp:  # noqa: S310 (configured peers)\n'
        '        return json.loads(resp.read().decode("utf-8"))\n\n\n'
        'def _http_get_json(url: str, headers: dict, timeout: int) -> dict:\n'
        '    return _http_json(url, headers, timeout, "GET")\n\n\n'
        'def _http_post_json(url: str, body: dict, headers: dict, timeout: int) -> dict:\n'
        '    hdrs = {"Content-Type": "application/json", "A2A-Version": protocol.PROTOCOL_VERSION, **headers}\n'
        '    return _http_json(url, hdrs, timeout, "POST", json.dumps(body).encode("utf-8"))\n\n\n'
        'def _fetch_card(base_url: str, headers: dict, timeout: int) -> dict:\n',
        'class _NoRedirect(urllib.request.HTTPRedirectHandler):\n'
        '    def redirect_request(self, req, fp, code, msg, headers, newurl):\n'
        '        return None\n\n\n'
        'def _http_json(url: str, headers: dict, timeout: int, method: str, data: Optional[bytes] = None,\n'
        '               proxy: str = "", proxy_authority: str = "") -> dict:\n'
        '    req = urllib.request.Request(url, data=data, headers=headers, method=method)\n'
        '    if proxy:\n'
        '        actual = urllib.parse.urlsplit(url).netloc.lower()\n'
        '        expected = str(proxy_authority or "").lower()\n'
        '        if not expected or actual != expected:\n'
        '            raise ValueError(f"proxied A2A URL authority {actual!r} does not match configured authority")\n'
        '        opener = urllib.request.build_opener(urllib.request.ProxyHandler({"http": proxy, "https": proxy}), _NoRedirect())\n'
        '        with opener.open(req, timeout=timeout) as resp:\n'
        '            return json.loads(resp.read().decode("utf-8"))\n'
        '    with urllib.request.urlopen(req, timeout=timeout) as resp:  # noqa: S310 (configured peers)\n'
        '        return json.loads(resp.read().decode("utf-8"))\n\n\n'
        'def _http_get_json(url: str, headers: dict, timeout: int, proxy: str = "", proxy_authority: str = "") -> dict:\n'
        '    return _http_json(url, headers, timeout, "GET", proxy=proxy, proxy_authority=proxy_authority)\n\n\n'
        'def _http_post_json(url: str, body: dict, headers: dict, timeout: int, proxy: str = "", proxy_authority: str = "") -> dict:\n'
        '    hdrs = {"Content-Type": "application/json", "A2A-Version": protocol.PROTOCOL_VERSION, **headers}\n'
        '    return _http_json(url, hdrs, timeout, "POST", json.dumps(body).encode("utf-8"), proxy, proxy_authority)\n\n\n'
        'def _fetch_card(base_url: str, headers: dict, timeout: int, proxy: str = "", proxy_authority: str = "") -> dict:\n',
    ),
    (
        'return _http_get_json(base + "/.well-known/agent-card.json", headers, timeout)\n',
        'return _http_get_json(base + "/.well-known/agent-card.json", headers, timeout, proxy, proxy_authority)\n',
    ),
    (
        'return _http_get_json(base + "/.well-known/agent.json", headers, timeout)\n',
        'return _http_get_json(base + "/.well-known/agent.json", headers, timeout, proxy, proxy_authority)\n',
    ),
    (
        '    timeout = int(peer.get("timeout", _DEFAULT_TIMEOUT))\n'
        '    try:\n'
        '        card = _fetch_card(base_url, headers, min(timeout, 30))  # best-effort, to learn the rpc URL\n',
        '    timeout = int(peer.get("timeout", _DEFAULT_TIMEOUT))\n'
        '    proxy = str(peer.get("proxy") or "")\n'
        '    proxy_authority = str(peer.get("proxy_authority") or "")\n'
        '    try:\n'
        '        card = _fetch_card(base_url, headers, min(timeout, 30), proxy, proxy_authority)  # best-effort\n',
    ),
    (
        '    resp = _http_post_json(_rpc_url(base_url, card), rpc_body, headers, timeout)\n',
        '    resp = _http_post_json(_rpc_url(base_url, card), rpc_body, headers, timeout, proxy, proxy_authority)\n',
    ),
]

for old, new in replacements:
    count = text.count(old)
    if count != 1:
        raise SystemExit(f"expected exactly one pinned source fragment, found {count}")
    text = text.replace(old, new)
path.write_text(text, encoding="utf-8")
