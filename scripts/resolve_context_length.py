#!/usr/bin/env python3
"""Resolve REAL serving context windows (max_model_len) for the ALCF models.

Two outputs, selected by --mode:

  --mode value  (default): print ONE integer — the active model's max_model_len,
      for the top-level ``model.context_length``. Falls back to
      $ALCF_CONTEXT_LENGTH then 128000.

  --mode mapping: print a YAML ``models:`` mapping (2-space indented, ready to
      splice under a custom_providers entry) giving each listed model its real
      ``context_length``. This is what makes an in-session ``/model`` switch use
      the correct window — Hermes honours per-model
      ``custom_providers[].models.<id>.context_length`` overrides (issue #15779),
      and the ALCF gateway 404s the ``/v1/models`` path Hermes would otherwise
      probe, so without this a switch falls back to Hermes' (wrong) static table.

ALCF caps some models below their published spec (gemma-4-31B-it=128000 not
256000; gpt-oss-120b=65536; Mistral-Large-2407=16384; ...). The authoritative
value is the server's ``max_model_len`` from ``.../<cluster>/models``.

Never fatal: any error path degrades to a safe default so container start can't
break on a lookup failure.

Usage:
    resolve_context_length.py <vllm_base_url> <model_id> [--mode value|mapping] \
        [--models m1,m2,...]
"""
import argparse
import json
import os
import re
import subprocess
import sys
import urllib.request

DEFAULT_CONTEXT = 128000
AUTH_HELPER = os.environ.get("ALCF_INFER_AUTH", "/opt/alcf/inference_auth_token.py")
PY = os.environ.get("ALCF_PY", sys.executable)


def _fallback() -> int:
    raw = os.environ.get("ALCF_CONTEXT_LENGTH", "").strip()
    try:
        v = int(raw)
        if v > 0:
            return v
    except (TypeError, ValueError):
        pass
    return DEFAULT_CONTEXT


def _models_url(vllm_base_url: str) -> str:
    """.../resource_server/<cluster>/vllm/v1 -> .../resource_server/<cluster>/models"""
    m = re.match(r"^(.*/resource_server/[^/]+)/", vllm_base_url.rstrip("/") + "/")
    if not m:
        raise ValueError(f"cannot derive models URL from {vllm_base_url!r}")
    return m.group(1) + "/models"


def _get_token() -> str:
    return subprocess.check_output(
        [PY, AUTH_HELPER, "get_access_token"], text=True, timeout=30
    ).strip()


def _fetch_max_lens(vllm_base_url: str) -> dict:
    """Return {model_id: max_model_len} for every model that reports one."""
    url = _models_url(vllm_base_url)
    token = _get_token()
    req = urllib.request.Request(url, headers={"Authorization": "Bearer " + token})
    with urllib.request.urlopen(req, timeout=30) as r:
        doc = json.load(r)
    items = doc.get("data", doc) if isinstance(doc, dict) else doc
    out = {}
    if isinstance(items, list):
        for m in items:
            if not isinstance(m, dict):
                continue
            mid = m.get("id") or m.get("Models")
            mml = m.get("max_model_len")
            if mid and isinstance(mml, int) and mml > 0:
                out[str(mid)] = mml
    return out


def _mode_value(vllm_base_url: str, model_id: str) -> None:
    try:
        lens = _fetch_max_lens(vllm_base_url)
        val = lens.get(model_id)
        if not (isinstance(val, int) and val > 0):
            raise ValueError(f"no usable max_model_len for {model_id!r}")
        print(f"resolved max_model_len={val} for {model_id} from server",
              file=sys.stderr)
        print(val)
    except Exception as e:  # noqa: BLE001
        fb = _fallback()
        print(f"context-length lookup failed ({e}); using fallback {fb}",
              file=sys.stderr)
        print(fb)


def _mode_mapping(vllm_base_url: str, models: list) -> None:
    """Emit a 2-space-indented YAML models: mapping with per-model context_length.

    Any model whose server value can't be resolved is emitted with the fallback,
    so the mapping always covers every listed model (switching is always safe).
    """
    try:
        lens = _fetch_max_lens(vllm_base_url)
    except Exception as e:  # noqa: BLE001
        lens = {}
        print(f"models listing failed ({e}); using fallback for all",
              file=sys.stderr)
    fb = _fallback()
    lines = ["models:"]
    for mid in models:
        ctx = lens.get(mid)
        if not (isinstance(ctx, int) and ctx > 0):
            ctx = fb
            print(f"  (fallback {fb} for {mid})", file=sys.stderr)
        lines.append(f"  {mid}:")
        lines.append(f"    context_length: {ctx}")
    print("\n".join(lines))


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("vllm_base_url")
    ap.add_argument("model_id", nargs="?", default="")
    ap.add_argument("--mode", choices=["value", "mapping"], default="value")
    ap.add_argument("--models", default="",
                    help="comma-separated model ids (for --mode mapping)")
    args = ap.parse_args()

    if args.mode == "mapping":
        models = [m.strip() for m in args.models.split(",") if m.strip()]
        if not models and args.model_id:
            models = [args.model_id]
        _mode_mapping(args.vllm_base_url, models)
    else:
        _mode_value(args.vllm_base_url, args.model_id or "")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
