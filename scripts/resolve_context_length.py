#!/usr/bin/env python3
"""Resolve the REAL serving context window (max_model_len) for the active model.

ALCF caps some models below their published spec (e.g. Sophia serves
google/gemma-4-31B-it at 128000, not the 256000 that Hermes assumes from the
model family). Hermes honours an explicit ``model.context_length`` in config as
its highest-priority override, so entrypoint.sh injects the value this script
prints as ``ALCF_CONTEXT_LENGTH`` before rendering the config.

Resolution order:
  1. Query the cluster's ``.../<cluster>/models`` endpoint for the selected
     model's ``max_model_len`` (authoritative — it's what the server will
     actually serve).
  2. Fall back to the ``ALCF_CONTEXT_LENGTH`` env var if set and valid.
  3. Fall back to a safe default (128000) otherwise.

Prints a single integer to stdout. Never fails the container: any error path
degrades to the env/default fallback, because a wrong-but-sane window is far
better than a crash at startup.

Usage:
    resolve_context_length.py <vllm_base_url> <model_id>

<vllm_base_url> is the OpenAI-wire base, e.g.
    https://inference-api.alcf.anl.gov/resource_server/sophia/vllm/v1
The models endpoint is derived from it:
    https://inference-api.alcf.anl.gov/resource_server/sophia/models
"""
import json
import os
import re
import subprocess
import sys
import urllib.request

DEFAULT_CONTEXT = 128000
AUTH_HELPER = os.environ.get(
    "ALCF_INFER_AUTH", "/opt/alcf/inference_auth_token.py"
)
PY = os.environ.get("ALCF_PY", sys.executable)


def _fallback() -> int:
    """Env override, else the safe default. Always returns a positive int."""
    raw = os.environ.get("ALCF_CONTEXT_LENGTH", "").strip()
    try:
        v = int(raw)
        if v > 0:
            return v
    except (TypeError, ValueError):
        pass
    return DEFAULT_CONTEXT


def _models_url(vllm_base_url: str) -> str:
    """Derive the discovery endpoint from the vllm base URL.

    .../resource_server/<cluster>/vllm/v1  ->  .../resource_server/<cluster>/models
    .../resource_server/<cluster>/api/v1   ->  .../resource_server/<cluster>/models
    """
    m = re.match(r"^(.*/resource_server/[^/]+)/", vllm_base_url.rstrip("/") + "/")
    if not m:
        raise ValueError(f"cannot derive models URL from {vllm_base_url!r}")
    return m.group(1) + "/models"


def _get_token() -> str:
    return subprocess.check_output(
        [PY, AUTH_HELPER, "get_access_token"], text=True, timeout=30
    ).strip()


def _served_max_len(vllm_base_url: str, model_id: str) -> int:
    url = _models_url(vllm_base_url)
    token = _get_token()
    req = urllib.request.Request(url, headers={"Authorization": "Bearer " + token})
    with urllib.request.urlopen(req, timeout=30) as r:
        doc = json.load(r)
    items = doc.get("data", doc) if isinstance(doc, dict) else doc
    if not isinstance(items, list):
        raise ValueError("unexpected models payload shape")
    for m in items:
        if not isinstance(m, dict):
            continue
        mid = m.get("id") or m.get("Models") or ""
        if str(mid) == model_id:
            mml = m.get("max_model_len")
            if isinstance(mml, int) and mml > 0:
                return mml
            raise ValueError(f"model {model_id!r} has no usable max_model_len ({mml!r})")
    raise ValueError(f"model {model_id!r} not found in models list")


def main() -> int:
    if len(sys.argv) != 3:
        # Misuse: still print something sane so the caller never breaks.
        print(_fallback())
        return 0
    vllm_base_url, model_id = sys.argv[1], sys.argv[2]
    try:
        val = _served_max_len(vllm_base_url, model_id)
        # Surface the source on stderr so entrypoint logs show provenance.
        print(f"resolved max_model_len={val} for {model_id} from server",
              file=sys.stderr)
        print(val)
    except Exception as e:  # noqa: BLE001 — never fail the container
        fb = _fallback()
        print(f"context-length lookup failed ({e}); using fallback {fb}",
              file=sys.stderr)
        print(fb)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
