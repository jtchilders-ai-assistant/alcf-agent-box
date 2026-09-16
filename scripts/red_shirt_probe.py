#!/usr/bin/env python3
"""Red Shirt Polaris: machine-readable runtime probes for readiness, A2A, and
inference verification.

Design: docs/superpowers/specs/2026-09-16-red-shirt-polaris-design.md
Plan:   docs/superpowers/plans/2026-09-16-red-shirt-polaris.md (Task 4)

Subcommands
-----------
    probe card --url URL [--proxy URL --proxy-authority HOST:PORT]
    probe a2a-negative --url URL
    probe a2a-send --url URL --token-file FILE --message TEXT
                   [--proxy URL --proxy-authority HOST:PORT]
    probe inference --base-url URL --model ID --token-file FILE [--proxy URL]
    probe ready-record --output FILE
        --card-url URL [--card-proxy URL --card-proxy-authority HOST:PORT]
        --a2a-negative-url URL
        --a2a-send-url URL --a2a-token-file FILE --a2a-message TEXT
                   [--a2a-proxy URL --a2a-proxy-authority HOST:PORT]
        --inference-base-url URL --inference-model ID --inference-token-file FILE
                   [--inference-proxy URL]
    probe terminal-record --exit-code N --output FILE

Design notes
------------
- stdlib only (urllib/json/argparse) — no third-party deps in the image.
- Every subcommand prints exactly one JSON object to stdout and returns
  0 on success, 1 on any failure. Nothing but non-secret status text ever
  reaches stdout/stderr — bearer tokens are read from files, never logged,
  never included in an error message, never passed as CLI args.
- Proxy routing is per-call and explicit (urllib.request.ProxyHandler +
  build_opener), never a process-wide os.environ mutation, so a caller that
  forgets --proxy cannot accidentally route a call (inference or otherwise)
  through some other proxy path. A live-socket experiment (see the plan's
  Task 4 Step 4) confirmed the pinned Hermes/urllib stdlib path honors an
  explicit per-call HTTP proxy for an exact, even DNS-unresolvable,
  authority — so no extension to connect_proxy.py was required.
- Split routing, enforced by exact authority, not by convention:
    * card/a2a-send (Wesley, tailnet-only) accept --proxy (the Tailscale
      userspace outbound HTTP proxy) but REQUIRE --proxy-authority whenever
      --proxy is supplied. Before any network call is made, the target
      URL's host:port is compared byte-for-byte to --proxy-authority; on
      any mismatch (or a missing --proxy-authority) the call fails closed
      with ok:false and NO request is ever issued — the Tailscale proxy can
      never be used to reach an arbitrary authority.
    * inference accepts an independent --proxy (the ALCF forward proxy) with
      no authority restriction (the ALCF base_url is the operator-supplied,
      already-pinned target) — it is never the Tailscale proxy and is
      configured, tested, and asserted as a distinct opener/call.
- Wire format matches plugins/platforms/a2a/tools.py: JSON-RPC 2.0,
  method "SendMessage", v1.0 Message shape (contextId/messageId on the
  Message, not the params). Card discovery is GET
  <url>/.well-known/agent-card.json.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import tempfile
import urllib.error
import urllib.request
import uuid
from pathlib import Path
from typing import Any, Optional

DEFAULT_TIMEOUT = 15


# ---------------------------------------------------------------------------
# Secret-free helpers
# ---------------------------------------------------------------------------

def _read_token(token_file: str) -> str:
    """Read a bearer token from a file. Never logs or returns it via an
    exception message that could be printed verbatim elsewhere."""
    path = Path(token_file)
    if not path.is_file():
        raise FileNotFoundError(f"token file not found: {token_file}")
    token = path.read_text(encoding="utf-8").strip()
    if not token:
        raise ValueError(f"token file is empty: {token_file}")
    return token


def _opener(proxy_url: Optional[str]) -> urllib.request.OpenerDirector:
    """Build a per-call urllib opener. With a proxy, ONLY this opener's calls
    go through it — no process-wide os.environ mutation, so inference (or any
    other call built with a fresh opener/urlopen) is never captured."""
    if proxy_url:
        handler = urllib.request.ProxyHandler({"http": proxy_url, "https": proxy_url})
        return urllib.request.build_opener(handler)
    # ProxyHandler({}) disables ambient environment proxy pickup entirely,
    # so a caller that does not pass --proxy is immune to an inherited
    # HTTP_PROXY/http_proxy env var silently redirecting the call.
    return urllib.request.build_opener(urllib.request.ProxyHandler({}))


def _http_get_json(url: str, *, proxy: Optional[str], headers: Optional[dict] = None,
                   timeout: int = DEFAULT_TIMEOUT) -> dict:
    req = urllib.request.Request(url, headers=headers or {}, method="GET")
    opener = _opener(proxy)
    with opener.open(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8"))


def _http_post_json(url: str, body: dict, *, proxy: Optional[str], headers: Optional[dict] = None,
                    timeout: int = DEFAULT_TIMEOUT) -> tuple[int, Any]:
    """POST JSON; returns (status_code, parsed_body_or_None). Raises only on
    transport-level failures (DNS, connection refused, timeout)."""
    data = json.dumps(body).encode("utf-8")
    hdrs = {"Content-Type": "application/json", **(headers or {})}
    req = urllib.request.Request(url, data=data, headers=hdrs, method="POST")
    opener = _opener(proxy)
    try:
        with opener.open(req, timeout=timeout) as resp:
            raw = resp.read()
            parsed = _try_parse_json(raw)
            return resp.status, parsed
    except urllib.error.HTTPError as e:
        raw = e.read()
        parsed = _try_parse_json(raw)
        return e.code, parsed


def _try_parse_json(raw: bytes) -> Any:
    try:
        return json.loads(raw.decode("utf-8"))
    except Exception:
        return None


def _card_url(base_url: str) -> str:
    return base_url.rstrip("/") + "/.well-known/agent-card.json"


class AuthorityMismatchError(Exception):
    """Raised when a Tailscale-proxied call's target authority does not
    exactly match the configured --proxy-authority allowlist entry. Carries
    no secret material — safe to stringify into a JSON detail field."""


def _authority_of(url: str) -> str:
    """Exact host:port (or host, if the URL carries no explicit port)."""
    from urllib.parse import urlsplit
    return urlsplit(url).netloc


def _check_wesley_authority(url: str, proxy_url: Optional[str],
                            proxy_authority: Optional[str]) -> None:
    """Fail closed BEFORE any network call when a Tailscale proxy is
    supplied without (or with a mismatching) exact authority allowlist.

    This is the enforcement point for the design's "exact Wesley tailnet
    authority only" rule: the Tailscale outbound proxy must never be usable
    to reach an arbitrary authority just because a caller supplied one.
    """
    if not proxy_url:
        return
    if not proxy_authority:
        raise AuthorityMismatchError(
            "--proxy supplied without --proxy-authority: refusing to use "
            "the Tailscale proxy for an unconstrained authority"
        )
    actual = _authority_of(url)
    if actual != proxy_authority:
        raise AuthorityMismatchError(
            f"target authority {actual!r} does not match the exact "
            f"allowlisted --proxy-authority {proxy_authority!r}: refusing "
            "to route a non-Wesley authority through the Tailscale proxy"
        )


def _text_part(text: str) -> dict:
    return {"text": text, "mediaType": "text/plain"}


def _text_message(text: str, context_id: str) -> dict:
    return {
        "role": "ROLE_USER",
        "parts": [_text_part(text)],
        "messageId": uuid.uuid4().hex,
        "contextId": context_id,
    }


def _extract_reply_text(result: Any) -> str:
    """Pull text from a v1.0 SendMessageResponse (task or message wrapper)."""
    if not isinstance(result, dict):
        return ""
    payload = result.get("task") or result.get("message") or result
    if not isinstance(payload, dict):
        return ""
    for artifact in payload.get("artifacts", []) or []:
        for part in artifact.get("parts", []) or []:
            txt = part.get("text")
            if isinstance(txt, str) and txt:
                return txt
    status = payload.get("status", {}) or {}
    msg = status.get("message")
    if isinstance(msg, dict):
        for part in msg.get("parts", []) or []:
            txt = part.get("text")
            if isinstance(txt, str) and txt:
                return txt
    # bare Message result
    for part in payload.get("parts", []) or []:
        txt = part.get("text")
        if isinstance(txt, str) and txt:
            return txt
    return ""


# ---------------------------------------------------------------------------
# Individual probes — each returns a result dict {"step", "ok", ...}
# ---------------------------------------------------------------------------

def probe_card(url: str, proxy: Optional[str], proxy_authority: Optional[str] = None) -> dict:
    try:
        _check_wesley_authority(url, proxy, proxy_authority)
        card = _http_get_json(_card_url(url), proxy=proxy)
    except Exception as e:  # noqa: BLE001 — fail closed, message is non-secret
        return {"step": "card", "ok": False, "detail": f"{type(e).__name__}: {e}"}
    name = card.get("name") if isinstance(card, dict) else None
    if not name:
        return {"step": "card", "ok": False, "detail": "card missing 'name'"}
    return {"step": "card", "ok": True, "name": name}


def probe_a2a_negative(url: str) -> dict:
    """An unauthenticated SendMessage must be rejected with HTTP 401."""
    body = {
        "jsonrpc": "2.0",
        "id": uuid.uuid4().hex,
        "method": "SendMessage",
        "params": {"message": _text_message("negative control probe", uuid.uuid4().hex)},
    }
    try:
        status, _parsed = _http_post_json(url, body, proxy=None)
    except Exception as e:  # noqa: BLE001
        return {"step": "a2a_negative", "ok": False, "detail": f"{type(e).__name__}: {e}"}
    if status == 401:
        return {"step": "a2a_negative", "ok": True, "detail": "HTTP 401 as required"}
    return {"step": "a2a_negative", "ok": False,
            "detail": f"expected HTTP 401 for unauthenticated request, got {status}"}


def probe_a2a_send(url: str, token_file: str, message: str,
                   proxy: Optional[str], proxy_authority: Optional[str] = None) -> dict:
    try:
        _check_wesley_authority(url, proxy, proxy_authority)
        token = _read_token(token_file)
    except Exception as e:  # noqa: BLE001
        return {"step": "a2a_send", "ok": False, "detail": f"{type(e).__name__}: {e}"}

    context_id = "ctx-" + uuid.uuid4().hex[:16]
    body = {
        "jsonrpc": "2.0",
        "id": "task-" + uuid.uuid4().hex[:16],
        "method": "SendMessage",
        "params": {"message": _text_message(message, context_id)},
    }
    headers = {"Authorization": f"Bearer {token}"}
    try:
        status, parsed = _http_post_json(url, body, proxy=proxy, headers=headers)
    except Exception as e:  # noqa: BLE001
        return {"step": "a2a_send", "ok": False, "detail": f"{type(e).__name__}: {e}"}

    if status != 200:
        return {"step": "a2a_send", "ok": False, "detail": f"unexpected HTTP status {status}"}
    if not isinstance(parsed, dict):
        return {"step": "a2a_send", "ok": False, "detail": "malformed JSON-RPC response"}
    if "error" in parsed:
        err = parsed["error"]
        detail = err.get("message", str(err)) if isinstance(err, dict) else str(err)
        return {"step": "a2a_send", "ok": False, "detail": f"peer returned an error: {detail}"}

    result = parsed.get("result")
    reply = _extract_reply_text(result)
    if not reply:
        return {"step": "a2a_send", "ok": False, "detail": "reply artifact/status text was empty"}
    return {"step": "a2a_send", "ok": True, "reply": reply, "context_id": context_id}


def probe_inference(base_url: str, model: str, token_file: str,
                    proxy: Optional[str] = None) -> dict:
    """Direct ALCF inference smoke test. ``proxy``, when supplied, is the
    ALCF forward proxy (e.g. proxy.alcf.anl.gov:3128) — an entirely distinct
    opener/call path from the Tailscale outbound proxy used for Wesley
    traffic. Never subject to the Wesley exact-authority allowlist: the
    ALCF base_url is the operator-supplied, already-pinned inference
    endpoint, not an arbitrary caller-chosen tailnet authority."""
    try:
        token = _read_token(token_file)
    except Exception as e:  # noqa: BLE001
        return {"step": "inference", "ok": False, "detail": f"{type(e).__name__}: {e}"}

    body = {
        "model": model,
        "messages": [{"role": "user", "content": "Reply with exactly one word: pong."}],
        "max_tokens": 16,
    }
    headers = {"Authorization": f"Bearer {token}"}
    url = base_url.rstrip("/") + "/chat/completions"
    try:
        status, parsed = _http_post_json(url, body, proxy=proxy, headers=headers, timeout=60)
    except Exception as e:  # noqa: BLE001
        return {"step": "inference", "ok": False, "detail": f"{type(e).__name__}: {e}"}

    if status != 200:
        return {"step": "inference", "ok": False, "detail": f"unexpected HTTP status {status}"}
    if not isinstance(parsed, dict):
        return {"step": "inference", "ok": False, "detail": "malformed JSON response"}
    choices = parsed.get("choices") or []
    if not choices:
        return {"step": "inference", "ok": False, "detail": "no choices in response"}
    content = (choices[0].get("message") or {}).get("content")
    if not isinstance(content, str) or not content.strip():
        return {"step": "inference", "ok": False,
                "detail": "HTTP 200 but content was null/empty — reject as unusable"}
    return {"step": "inference", "ok": True, "content_length": len(content)}


# ---------------------------------------------------------------------------
# Atomic JSON write (shared with red_shirt_config.py's approach)
# ---------------------------------------------------------------------------

def _atomic_write_json(path: Path, payload: dict) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=f".{path.name}.", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(payload, f, indent=2, sort_keys=True)
            f.write("\n")
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def _print_and_exit(payload: dict) -> int:
    print(json.dumps(payload, sort_keys=True))
    return 0 if payload.get("ok") else 1


def cmd_card(args: argparse.Namespace) -> int:
    return _print_and_exit(probe_card(args.url, args.proxy, args.proxy_authority))


def cmd_a2a_negative(args: argparse.Namespace) -> int:
    return _print_and_exit(probe_a2a_negative(args.url))


def cmd_a2a_send(args: argparse.Namespace) -> int:
    return _print_and_exit(
        probe_a2a_send(args.url, args.token_file, args.message, args.proxy, args.proxy_authority)
    )


def cmd_inference(args: argparse.Namespace) -> int:
    return _print_and_exit(probe_inference(args.base_url, args.model, args.token_file, args.proxy))


def cmd_ready_record(args: argparse.Namespace) -> int:
    results = [
        probe_card(args.card_url, args.card_proxy, args.card_proxy_authority),
        probe_a2a_negative(args.a2a_negative_url),
        probe_a2a_send(args.a2a_send_url, args.a2a_token_file, args.a2a_message,
                       args.a2a_proxy, args.a2a_proxy_authority),
        probe_inference(args.inference_base_url, args.inference_model,
                        args.inference_token_file, args.inference_proxy),
    ]
    overall_ok = all(r["ok"] for r in results)
    payload = {"overall_ok": overall_ok, "results": results}
    _atomic_write_json(Path(args.output), payload)
    print(json.dumps(payload, sort_keys=True))
    return 0 if overall_ok else 1


def cmd_terminal_record(args: argparse.Namespace) -> int:
    payload = {"exit_code": args.exit_code, "ok": args.exit_code == 0}
    _atomic_write_json(Path(args.output), payload)
    print(json.dumps(payload, sort_keys=True))
    return 0


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="red_shirt_probe.py",
        description="Red Shirt Polaris: readiness/A2A/inference probes.",
    )
    sub = p.add_subparsers(dest="command", required=True)

    card = sub.add_parser("card", help="fetch the A2A Agent Card, optionally through the Tailscale proxy")
    card.add_argument("--url", required=True)
    card.add_argument("--proxy", default=None,
                      help="Tailscale outbound HTTP proxy URL (e.g. http://127.0.0.1:1056)")
    card.add_argument("--proxy-authority", default=None,
                      help="exact host:port this proxy may be used for; required with --proxy")
    card.set_defaults(func=cmd_card)

    neg = sub.add_parser("a2a-negative", help="unauthenticated SendMessage must return HTTP 401")
    neg.add_argument("--url", required=True)
    neg.set_defaults(func=cmd_a2a_negative)

    send = sub.add_parser("a2a-send", help="authenticated A2A v1.0 SendMessage")
    send.add_argument("--url", required=True)
    send.add_argument("--token-file", required=True)
    send.add_argument("--message", required=True)
    send.add_argument("--proxy", default=None,
                      help="Tailscale outbound HTTP proxy URL (e.g. http://127.0.0.1:1056)")
    send.add_argument("--proxy-authority", default=None,
                      help="exact host:port this proxy may be used for; required with --proxy")
    send.set_defaults(func=cmd_a2a_send)

    inf = sub.add_parser("inference", help="direct ALCF inference smoke test")
    inf.add_argument("--base-url", required=True)
    inf.add_argument("--model", required=True)
    inf.add_argument("--token-file", required=True)
    inf.add_argument("--proxy", default=None,
                     help="ALCF forward proxy URL (e.g. http://proxy.alcf.anl.gov:3128); "
                          "a distinct path from the Tailscale outbound proxy, no authority "
                          "restriction (the ALCF base_url is already operator-pinned)")
    inf.set_defaults(func=cmd_inference)

    ready = sub.add_parser("ready-record", help="run every probe and emit a JSON readiness record")
    ready.add_argument("--output", required=True)
    ready.add_argument("--card-url", required=True)
    ready.add_argument("--card-proxy", default=None)
    ready.add_argument("--card-proxy-authority", default=None)
    ready.add_argument("--a2a-negative-url", required=True)
    ready.add_argument("--a2a-send-url", required=True)
    ready.add_argument("--a2a-token-file", required=True)
    ready.add_argument("--a2a-message", required=True)
    ready.add_argument("--a2a-proxy", default=None)
    ready.add_argument("--a2a-proxy-authority", default=None)
    ready.add_argument("--inference-base-url", required=True)
    ready.add_argument("--inference-model", required=True)
    ready.add_argument("--inference-token-file", required=True)
    ready.add_argument("--inference-proxy", default=None)
    ready.set_defaults(func=cmd_ready_record)

    term = sub.add_parser("terminal-record", help="write the final exit-code record")
    term.add_argument("--exit-code", type=int, required=True)
    term.add_argument("--output", required=True)
    term.set_defaults(func=cmd_terminal_record)

    return p


def main(argv: Optional[list] = None) -> int:
    args = build_parser().parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
