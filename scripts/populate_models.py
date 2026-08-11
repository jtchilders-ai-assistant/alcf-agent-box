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

# --- Per-model output cap (max_tokens) ---------------------------------------
# ALCF serves several REASONING models whose hidden chain-of-thought is emitted
# on a separate channel that DRAWS FROM THE SAME max_tokens budget as the visible
# answer. With a small cap, the model can burn the whole budget thinking and
# return empty content (finish_reason=length) — verified on gpt-oss at ALCF. So
# reasoning models need a bigger output cap than plain chat models.
#
# Hermes has NO per-model max_tokens (unlike context_length): it honours a
# per-PROVIDER max_tokens on a custom_providers entry (runtime_provider.py
# _lift_max_output_tokens), applied only when the top-level model.max_tokens is
# unset. So we express "per-model" caps by SPLITTING each cluster into two
# provider blocks — a baseline block and a "-reasoning" block — each carrying its
# own max_tokens. The launch model's cap is set separately by the entrypoint on
# the top-level model: block (that model resolves through raw provider:custom,
# not a named provider).
#
# Both caps are env-overridable (entrypoint passes them through):
#   ALCF_MAX_TOKENS            baseline output cap  (default 2048)
#   ALCF_REASONING_MAX_TOKENS  reasoning output cap (default 12288)
BASELINE_MAX_TOKENS = int(os.environ.get("ALCF_MAX_TOKENS") or 2048)
REASONING_MAX_TOKENS = int(os.environ.get("ALCF_REASONING_MAX_TOKENS") or 12288)

# Reasoning detection is two-layered:
#  (1) AUTHORITATIVE — the Sophia /models entry advertises a `reasoning_parser`
#      (e.g. nemotron-3-super="super_v3"; gemma-4*="gemma4"). vLLM only sets this
#      when the model is served with a reasoning parser, i.e. it emits a separate
#      reasoning channel. This is captured per-model during selection.
#  (2) ID HEURISTIC — some reasoning models are served WITHOUT a reasoning_parser
#      field (gpt-oss uses the built-in Harmony format + the `openai` tool-call
#      parser; Trinity-Large-Thinking exposes none). Catch these by id. Also the
#      only signal we have on Metis (framework: api, no parser field at all).
REASONING_ID_PATTERNS = (
    re.compile(r"gpt-oss", re.IGNORECASE),
    re.compile(r"gemma-4", re.IGNORECASE),  # gemma-4* served with reasoning_parser=gemma4 on Sophia; Metis omits the field
    re.compile(r"thinking", re.IGNORECASE),
    re.compile(r"reasoning", re.IGNORECASE),
    re.compile(r"deepseek-?r1", re.IGNORECASE),
    re.compile(r"\bqwq\b", re.IGNORECASE),
    re.compile(r"(^|[/-])o[13]([-.]|$)", re.IGNORECASE),  # o1 / o3 families
)


def _is_reasoning_id(mid: str) -> bool:
    return any(p.search(mid) for p in REASONING_ID_PATTERNS)

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
# broken entry Hermes will reject on load. Values are (context_length, is_reasoning),
# reflecting the real serving windows + reasoning class verified live 2026-08-10.
SOPHIA_FALLBACK = {
    "openai/gpt-oss-120b": (65536, True),
    "openai/gpt-oss-20b": (128000, True),
    "google/gemma-4-31B-it": (128000, True),
    "google/gemma-4-26B-A4B-it": (262144, True),
    "arcee-ai/Trinity-Large-Thinking-W4A16": (131072, True),
    "nvidia/nemotron-3-super-120b": (262144, True),
    "argonne/AuroraGPT-IT-v4-0125": (128000, False),
}
# Metis fallback: only the Metis models at/above the floor. Mistral-Large-3
# serves at 8192 (verified) so it is intentionally NOT here — it's unusable.
METIS_FALLBACK = {
    mid: (ctx, _is_reasoning_id(mid))
    for mid, ctx in METIS_CONTEXT.items() if ctx >= MIN_CONTEXT
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
    """{model_id: (context_length, is_reasoning)} for Sophia chat models.

    Excludes non-chat frameworks, embedding/science ids, AND any model whose real
    serving window is below Hermes' MIN_CONTEXT floor (an unusable dropdown entry).
    is_reasoning is True when the server advertises a `reasoning_parser` OR the id
    matches a known reasoning family (see _is_reasoning_id).
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
        is_reasoning = bool(m.get("reasoning_parser")) or _is_reasoning_id(mid)
        out[mid] = (ctx, is_reasoning)
    return out


def _select_metis(models: list) -> dict:
    """{model_id: (context_length, is_reasoning)} for Metis models (all chat).

    Metis reports no max_model_len, so context comes from METIS_CONTEXT (verified)
    or METIS_DEFAULT_CONTEXT. Any model below Hermes' MIN_CONTEXT floor is excluded
    (e.g. Mistral-Large-3 serves at only 8192 — unusable in Hermes). Metis exposes
    no reasoning_parser field, so reasoning is detected by id heuristic only.
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
        out[mid] = (ctx, _is_reasoning_id(mid))
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


def _emit_provider(name: str, base_url: str, mapping: dict,
                   max_tokens: int = 0) -> list:
    """Render one custom_providers entry (2-space list-item indent).

    ``mapping`` is {model_id: (context_length, is_reasoning)}; only context_length
    is emitted per model (Hermes has no per-model max_tokens). ``max_tokens`` > 0
    emits a PROVIDER-level output cap that Hermes lifts onto AIAgent.max_tokens
    for any model selected under this provider (when top-level model.max_tokens is
    unset) — this is how we give reasoning vs non-reasoning models different caps.
    """
    lines = [
        f"  - name: {name}",
        f'    base_url: "{base_url}"',
        '    api_key: "${ALCF_ACCESS_TOKEN}"',
        "    discover_models: false",
    ]
    if max_tokens and max_tokens > 0:
        lines.append(f"    max_tokens: {max_tokens}")
    lines.append("    models:")
    for mid in sorted(mapping):
        ctx = mapping[mid][0]
        lines.append(f"      {mid}:")
        lines.append(f"        context_length: {ctx}")
    return lines


def _split_reasoning(mapping: dict) -> tuple:
    """Split {id:(ctx,is_reasoning)} into (baseline_map, reasoning_map)."""
    baseline = {mid: v for mid, v in mapping.items() if not v[1]}
    reasoning = {mid: v for mid, v in mapping.items() if v[1]}
    return baseline, reasoning


def _emit_cluster(name: str, base_url: str, mapping: dict) -> list:
    """Emit one or two provider blocks for a cluster, split by reasoning class.

    Non-reasoning models go in ``<name>`` with the baseline output cap; reasoning
    models go in ``<name>-reasoning`` with the larger reasoning cap. A block is
    omitted entirely if its bucket is empty, so a cluster with only one class
    yields a single provider (no empty "-reasoning" entry cluttering the picker).
    """
    baseline, reasoning = _split_reasoning(mapping)
    lines = []
    if baseline:
        lines += _emit_provider(name, base_url, baseline,
                                max_tokens=BASELINE_MAX_TOKENS)
    if reasoning:
        lines += _emit_provider(f"{name}-reasoning", base_url, reasoning,
                                max_tokens=REASONING_MAX_TOKENS)
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
    lines += _emit_cluster("alcf-sophia", SOPHIA_BASE, sophia_map)
    print(f"[populate_models] sophia: {len(sophia_map)} models ({sophia_src})",
          file=sys.stderr)

    if include_metis:
        if token:
            metis_map, metis_src = _resolve_cluster(
                "metis", token, _select_metis, METIS_FALLBACK)
        else:
            metis_map, metis_src = dict(METIS_FALLBACK), "fallback"
        lines += _emit_cluster("alcf-metis", METIS_BASE, metis_map)
        print(f"[populate_models] metis: {len(metis_map)} models ({metis_src})",
              file=sys.stderr)

    return "\n".join(lines) + "\n"


def _fetch_hot(cluster: str, token: str) -> set:
    """Return the set of model ids currently RUNNING (hot) on a cluster.

    Reads .../<cluster>/jobs. Each running entry's "Models" field may list
    several comma-joined ids (e.g. "openai/gpt-oss-120b,openai/gpt-oss-20b"),
    so we split on commas. Never raises — returns an empty set on any failure
    (the caller then reports "unknown", not "cold", so we don't lie).
    """
    url = f"{INFER_HOST}/{cluster}/jobs"
    req = urllib.request.Request(url, headers={"Authorization": "Bearer " + token})
    hot: set = set()
    with urllib.request.urlopen(req, timeout=30) as r:
        doc = json.load(r)
    running = doc.get("running", []) if isinstance(doc, dict) else []
    for job in running:
        if not isinstance(job, dict):
            continue
        if str(job.get("Model Status", "")).lower() not in ("", "running"):
            continue
        for mid in str(job.get("Models", "")).split(","):
            mid = mid.strip()
            if mid:
                hot.add(mid)
    return hot


def hot_report() -> int:
    """Print a hot/cold status banner for the OFFERED models on each cluster.

    Cross-references the models we put in the dropdown against the live /jobs
    hot set so the user knows, before touching the picker, which models answer
    instantly and which trigger a ~10-15 min ALCF GPU cold-load (HTTP 503
    "online but not ready" until warm). Best-effort: any failure downgrades a
    line to "status unknown" rather than emitting a wrong hot/cold claim.
    """
    try:
        token = _get_token()
    except Exception as e:  # noqa: BLE001
        print(f"[hot-report] token fetch failed ({e}); skipping hot/cold banner",
              file=sys.stderr)
        return 0

    include_metis = os.environ.get("ALCF_ENABLE_METIS", "1") != "0"
    clusters = [("sophia", _select_sophia, SOPHIA_FALLBACK)]
    if include_metis:
        clusters.append(("metis", _select_metis, METIS_FALLBACK))

    lines = ["Model warm-up status (ALCF loads cold models on first use, ~10-15 min):"]
    for cluster, selector, fallback in clusters:
        offered, _src = _resolve_cluster(cluster, token, selector, fallback)
        try:
            hot = _fetch_hot(cluster, token)
            known = True
        except Exception as e:  # noqa: BLE001
            print(f"[hot-report] {cluster} /jobs failed ({e})", file=sys.stderr)
            hot, known = set(), False
        hot_ids = sorted(m for m in offered if m in hot)
        cold_ids = sorted(m for m in offered if m not in hot)
        lines.append(f"  {cluster}:")
        if not known:
            lines.append(f"    status unknown (could not read /jobs) — "
                         f"{len(offered)} models offered")
            continue
        lines.append(f"    HOT  (instant): {', '.join(hot_ids) if hot_ids else '(none)'}")
        lines.append(f"    cold (~10-15m): {', '.join(cold_ids) if cold_ids else '(none)'}")
    lines.append("  Tip: selecting a cold model returns HTTP 503 'online but not "
                 "ready' for a few minutes; wait and retry — it is loading, not broken.")
    # Emit to stdout so the entrypoint can `log` it line-by-line.
    sys.stdout.write("\n".join(lines) + "\n")
    return 0


def launch_provider(model_id: str) -> str:
    """Return the named custom provider the LAUNCH model should resolve through.

    The top-level model: block in config resolves through a NAMED custom provider
    so the launch turn inherits that provider's per-model output cap. This returns
    e.g. 'custom:alcf-sophia-reasoning' or 'custom:alcf-metis'. The cluster is read
    from ALCF_BASE_URL (the launch model always uses that endpoint); the reasoning
    class is taken from the live catalog's reasoning_parser when reachable, else
    the id heuristic (so this is robust offline / on discovery failure).

    Never raises — on any doubt it returns the reasoning provider, because giving a
    plain chat model extra output headroom is harmless, while starving a reasoning
    model of headroom produces empty responses.
    """
    base = os.environ.get("ALCF_BASE_URL", "") or SOPHIA_BASE
    cluster = "alcf-metis" if "/metis/" in base else "alcf-sophia"

    is_reasoning = _is_reasoning_id(model_id)
    if not is_reasoning:
        # Consult the live catalog for an authoritative reasoning_parser signal;
        # only the Sophia catalog carries it. Best-effort, never fatal.
        try:
            token = _get_token()
            cl = "metis" if cluster == "alcf-metis" else "sophia"
            for m in _fetch_models(cl, token):
                if m.get("id") == model_id and m.get("reasoning_parser"):
                    is_reasoning = True
                    break
        except Exception:  # noqa: BLE001
            pass

    name = f"{cluster}-reasoning" if is_reasoning else cluster
    return f"custom:{name}"


def main() -> int:
    import argparse
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default="", help="also write the block to this path")
    ap.add_argument("--no-metis", action="store_true",
                    help="omit the Metis provider")
    ap.add_argument("--hot-report", action="store_true",
                    help="print a hot/cold warm-up status banner instead of the "
                         "custom_providers block (queries /jobs)")
    ap.add_argument("--launch-provider", default="", metavar="MODEL_ID",
                    help="print the named custom provider the given launch model "
                         "should resolve through (e.g. custom:alcf-sophia-reasoning) "
                         "and exit")
    args = ap.parse_args()

    if args.launch_provider:
        sys.stdout.write(launch_provider(args.launch_provider) + "\n")
        return 0

    if args.hot_report:
        return hot_report()

    include_metis = not args.no_metis and os.environ.get("ALCF_ENABLE_METIS", "1") != "0"
    block = build_block(include_metis=include_metis)
    sys.stdout.write(block)
    if args.out:
        with open(args.out, "w", encoding="utf-8") as f:
            f.write(block)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
