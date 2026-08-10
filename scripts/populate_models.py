#!/usr/bin/env python3
"""Generate the Hermes ``custom_providers:`` block from the LIVE ALCF catalog.

At container start we query BOTH ALCF Inference Service clusters and emit one
``custom_providers`` entry per cluster, each with a curated, chat-only model
mapping that carries every model's REAL serving context window:

  - ``alcf-sophia``  (vLLM,  full OpenAI-compat)  base_url .../sophia/vllm/v1
  - ``alcf-metis``   (SambaNova, chat-only)        base_url .../metis/api/v1

Why this exists
---------------
The ALCF gateway 404s the standard OpenAI ``/v1/models`` path, so Hermes' own
live discovery finds nothing and the model picker shows only the launch model.
We instead query the ALCF-specific ``.../<cluster>/models`` endpoint ourselves
and write the result into a static ``models:`` MAPPING (``discover_models:false``)
so the dropdown reflects the live catalog and every model gets its correct
``context_length`` (Hermes honours per-model
``custom_providers[].models.<id>.context_length`` on an in-session ``/model``
switch — issue #15779). Without the right per-model window a switch overflows the
real context and the gateway silently drops the SSE stream (EmptyStreamError).

Two clusters, two shapes
------------------------
* Sophia entries report ``framework`` + (for served LLMs) ``max_model_len``. We
  keep ``framework == "vllm"`` chat models and derive ``context_length`` from
  ``max_model_len``. We EXCLUDE non-chat frameworks (triton/dinoserver/sam3service)
  and, by id heuristic, embedding + science models (``*-embed``, ``embedding*``,
  ``genslm*``) that lack a chat interface and pollute the picker.
* Metis entries are ``framework == "api"`` (SambaNova) and report NO
  ``max_model_len``. They are all chat models, so we keep them all and assign
  ``context_length`` from a small verified table (METIS_CONTEXT), falling back to
  METIS_DEFAULT_CONTEXT for an unknown new Metis model.

  IMPORTANT: Metis windows differ from the same-named Sophia model — verified
  live 2026-08-10 by reading the gateway's ``context_length_exceeded`` error:
  Mistral-Large-3-675B=8192, gpt-oss-120b=131072, gemma-4-31B-it=131072. That is
  exactly why the two clusters are separate providers, not one merged list.

Allowlist add-backs
-------------------
A few Sophia vLLM CHAT models report no ``max_model_len`` (e.g. gpt-oss-20b,
AuroraGPT variants). A strict "must have max_model_len" filter would drop them,
but ALCF users look for them (AuroraGPT is Argonne's own model). SOPHIA_ALLOWLIST
maps such ids to a fallback context so they still appear.

Never fatal
-----------
Any failure (network, auth, bad JSON, empty result for a cluster) degrades to the
committed static fallback for that cluster, and the script always prints a valid
``custom_providers:`` block so container start cannot break on a lookup failure.

Usage
-----
    populate_models.py [--out PATH]

Prints the YAML block to stdout (and to --out if given). Reads:
    ALCF_INFER_AUTH   path to inference_auth_token.py (token source)
    ALCF_PY           python used to run the auth helper (default: this python)
    ALCF_ENABLE_METIS include the Metis provider? "1" (default) / "0"
"""
import json
import os
import re
import subprocess
import sys
import urllib.request

INFER_HOST = "https://inference-api.alcf.anl.gov/resource_server"

# Per-cluster provider identity: name + base_url template.
SOPHIA_BASE = f"{INFER_HOST}/sophia/vllm/v1"
METIS_BASE = f"{INFER_HOST}/metis/api/v1"

AUTH_HELPER = os.environ.get("ALCF_INFER_AUTH", "/opt/alcf/inference_auth_token.py")
PY = os.environ.get("ALCF_PY", sys.executable)

# Fallback context window for a chat model that reports no server value.
DEFAULT_CONTEXT = 32768

# Hermes refuses to load any model whose resolved context window is below this
# (agent/model_metadata.py: MINIMUM_CONTEXT_LENGTH). A model under the floor is a
# BROKEN dropdown entry — selecting it raises at load/switch — so we exclude any
# model whose real serving window is < MIN_CONTEXT. Keep this in sync with
# Hermes' constant; a mismatch just means we're slightly conservative/liberal at
# the boundary, never that we ship an unusable entry (we err on excluding).
MIN_CONTEXT = 64000

# Sophia chat models that are real vLLM chat endpoints but report no
# max_model_len from the server. Keep them (allowlist) with a sane window.
SOPHIA_ALLOWLIST = {
    "openai/gpt-oss-20b": 128000,
    "argonne/AuroraGPT-IT-v4-0125": 128000,
    "argonne/AuroraGPT-Tulu3-SFT-0125": 128000,
    "argonne/AuroraGPT-DPO-UFB-0225": 128000,
    "argonne/AuroraGPT-KTO-UFB-0325": 128000,
}

# Metis models report no max_model_len; these were verified live from the
# gateway's context_length_exceeded error (2026-08-10). Note they differ from
# the same-named Sophia models.
METIS_CONTEXT = {
    "Mistral-Large-3-675B-Instruct-2512": 8192,
    "gpt-oss-120b": 131072,
    "gemma-4-31B-it": 131072,
}
METIS_DEFAULT_CONTEXT = 32768

# Frameworks that are NOT chat completion endpoints — exclude outright.
NON_CHAT_FRAMEWORKS = {"triton", "dinoserver", "sam3service"}

# Id substrings that mark a non-chat model (embeddings / science) — exclude even
# when the framework looks like vllm.
NON_CHAT_ID_PATTERNS = (
    re.compile(r"embed", re.IGNORECASE),
    re.compile(r"^genslm", re.IGNORECASE),
    re.compile(r"/genslm", re.IGNORECASE),
)

# --- Static fallbacks (used only when live discovery for a cluster fails) -----
# Only models at/above MIN_CONTEXT (64k) belong here — a sub-floor model is a
# broken entry Hermes will reject on load. Values reflect the real serving
# windows verified live 2026-08-10.
SOPHIA_FALLBACK = {
    "openai/gpt-oss-120b": 65536,
    "openai/gpt-oss-20b": 128000,
    "google/gemma-4-31B-it": 128000,
    "google/gemma-4-26B-A4B-it": 262144,
    "arcee-ai/Trinity-Large-Thinking-W4A16": 131072,
    "nvidia/nemotron-3-super-120b": 262144,
    "argonne/AuroraGPT-IT-v4-0125": 128000,
}
# Metis fallback: only the Metis models at/above the floor. Mistral-Large-3
# serves at 8192 (verified) so it is intentionally NOT here — it's unusable.
METIS_FALLBACK = {
    mid: ctx for mid, ctx in METIS_CONTEXT.items() if ctx >= MIN_CONTEXT
}


def _get_token() -> str:
    out = subprocess.check_output(
        [PY, AUTH_HELPER, "get_access_token"], text=True, timeout=30
    )
    # the helper may emit warnings on earlier lines; the token is the last line
    return out.strip().splitlines()[-1].strip()


def _fetch_models(cluster: str, token: str) -> list:
    """GET .../<cluster>/models -> list of model dicts (raises on failure)."""
    url = f"{INFER_HOST}/{cluster}/models"
    req = urllib.request.Request(url, headers={"Authorization": "Bearer " + token})
    with urllib.request.urlopen(req, timeout=30) as r:
        doc = json.load(r)
    items = doc.get("data", doc) if isinstance(doc, dict) else doc
    return [m for m in items if isinstance(m, dict)] if isinstance(items, list) else []


def _is_non_chat_id(mid: str) -> bool:
    return any(p.search(mid) for p in NON_CHAT_ID_PATTERNS)


def _select_sophia(models: list) -> dict:
    """{model_id: context_length} for Sophia chat models, filtered + allowlisted.

    Excludes non-chat frameworks, embedding/science ids, AND any model whose real
    serving window is below Hermes' MIN_CONTEXT floor (an unusable dropdown entry).
    """
    out = {}
    for m in models:
        mid = m.get("id")
        if not mid:
            continue
        fw = (m.get("framework") or "").lower()
        if fw in NON_CHAT_FRAMEWORKS:
            continue
        if _is_non_chat_id(mid):
            continue
        mml = m.get("max_model_len")
        if isinstance(mml, int) and mml > 0:
            ctx = mml
        elif mid in SOPHIA_ALLOWLIST:
            ctx = SOPHIA_ALLOWLIST[mid]
        else:
            # a vllm model with no window and not allowlisted -> skip
            continue
        if ctx < MIN_CONTEXT:
            print(f"[populate_models] sophia: skip {mid} (window {ctx} < {MIN_CONTEXT})",
                  file=sys.stderr)
            continue
        out[mid] = ctx
    return out


def _select_metis(models: list) -> dict:
    """{model_id: context_length} for Metis models (all chat; no server window).

    Metis reports no max_model_len, so context comes from METIS_CONTEXT (verified)
    or METIS_DEFAULT_CONTEXT. Any model below Hermes' MIN_CONTEXT floor is excluded
    (e.g. Mistral-Large-3 serves at only 8192 — unusable in Hermes).
    """
    out = {}
    for m in models:
        mid = m.get("id")
        if not mid:
            continue
        fw = (m.get("framework") or "").lower()
        if fw in NON_CHAT_FRAMEWORKS:
            continue
        if _is_non_chat_id(mid):
            continue
        ctx = METIS_CONTEXT.get(mid, METIS_DEFAULT_CONTEXT)
        if ctx < MIN_CONTEXT:
            print(f"[populate_models] metis: skip {mid} (window {ctx} < {MIN_CONTEXT})",
                  file=sys.stderr)
            continue
        out[mid] = ctx
    return out


def _resolve_cluster(cluster: str, token: str, selector, fallback: dict) -> tuple:
    """Return (mapping, source) where source is 'live' or 'fallback'."""
    try:
        models = _fetch_models(cluster, token)
        mapping = selector(models)
        if mapping:
            return mapping, "live"
        print(f"[populate_models] {cluster}: empty selection; using fallback",
              file=sys.stderr)
    except Exception as e:  # noqa: BLE001
        print(f"[populate_models] {cluster} discovery failed ({e}); using fallback",
              file=sys.stderr)
    return dict(fallback), "fallback"


def _emit_provider(name: str, base_url: str, mapping: dict) -> list:
    """Render one custom_providers entry (2-space list-item indent)."""
    lines = [
        f"  - name: {name}",
        f'    base_url: "{base_url}"',
        '    api_key: "${ALCF_ACCESS_TOKEN}"',
        "    discover_models: false",
        "    models:",
    ]
    for mid in sorted(mapping):
        lines.append(f"      {mid}:")
        lines.append(f"        context_length: {mapping[mid]}")
    return lines


def build_block(include_metis: bool = True) -> str:
    try:
        token = _get_token()
    except Exception as e:  # noqa: BLE001
        print(f"[populate_models] token fetch failed ({e}); full static fallback",
              file=sys.stderr)
        token = None

    if token:
        sophia_map, sophia_src = _resolve_cluster(
            "sophia", token, _select_sophia, SOPHIA_FALLBACK)
    else:
        sophia_map, sophia_src = dict(SOPHIA_FALLBACK), "fallback"

    lines = ["custom_providers:"]
    lines += _emit_provider("alcf-sophia", SOPHIA_BASE, sophia_map)
    print(f"[populate_models] sophia: {len(sophia_map)} models ({sophia_src})",
          file=sys.stderr)

    if include_metis:
        if token:
            metis_map, metis_src = _resolve_cluster(
                "metis", token, _select_metis, METIS_FALLBACK)
        else:
            metis_map, metis_src = dict(METIS_FALLBACK), "fallback"
        lines += _emit_provider("alcf-metis", METIS_BASE, metis_map)
        print(f"[populate_models] metis: {len(metis_map)} models ({metis_src})",
              file=sys.stderr)

    return "\n".join(lines) + "\n"


def main() -> int:
    import argparse
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default="", help="also write the block to this path")
    ap.add_argument("--no-metis", action="store_true",
                    help="omit the Metis provider")
    args = ap.parse_args()

    include_metis = not args.no_metis and os.environ.get("ALCF_ENABLE_METIS", "1") != "0"
    block = build_block(include_metis=include_metis)
    sys.stdout.write(block)
    if args.out:
        with open(args.out, "w", encoding="utf-8") as f:
            f.write(block)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
