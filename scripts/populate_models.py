#!/usr/bin/env python3
"""Generate the Hermes ``custom_providers:`` block from the LIVE ALCF catalog.

At container start we query every serving ALCF Inference Service cluster and emit
one ``custom_providers`` entry per cluster, each with a curated, chat-only model
mapping that carries every model's REAL serving context window:

  - ``alcf-sophia``   (vLLM,  full OpenAI-compat)  base_url .../sophia/vllm/v1
  - ``alcf-metis``    (SambaNova, chat-only)       base_url .../metis/api/v1
  - ``alcf-minerva``  (api framework)              base_url .../minerva/api/v1

``tara`` also appears in list-endpoints but is still being provisioned
(``/models`` -> ``[]``, ``/jobs`` -> HTTP 500), so it gets no provider; the status
banner reports it as provisioning. Promote it in ``_clusters()`` once it serves.

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

Three clusters, three catalog shapes
------------------------------------
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
  live 2026-08-10, re-verified 2026-08-24 by reading the gateway's
  ``context_length_exceeded`` error: Mistral-Large-3-675B=8192,
  gpt-oss-120b=131072, gemma-4-31B-it=131072. That is exactly why the clusters
  are separate providers, not one merged list.
* Minerva entries are ``framework == "api"`` but publish a STRUCTURED
  ``capabilities`` object (schema_version 1) — the richest of the three:
  ``capabilities.context_window_tokens`` and ``capabilities.reasoning.supported``
  are authoritative, so Minerva needs neither a hardcoded window table nor the
  reasoning id heuristic. Read them; the heuristic is only a last resort there.

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
    populate_models.py [--out PATH] [--no-metis] [--no-minerva]
    populate_models.py --status-report        # LIVE/QUEUED/OFFLINE + windows
    populate_models.py --launch-provider ID

Prints the YAML block to stdout (and to --out if given). Reads:
    ALCF_INFER_AUTH     path to inference_auth_token.py (token source)
    ALCF_PY             python used to run the auth helper (default: this python)
    ALCF_ENABLE_METIS   include the Metis provider?   "1" (default) / "0"
    ALCF_ENABLE_MINERVA include the Minerva provider? "1" (default) / "0"
"""
import json
import os
import re
import subprocess
import sys
import textwrap
import urllib.request

INFER_HOST = "https://inference-api.alcf.anl.gov/resource_server"

# Per-cluster provider identity: name + base_url template.
SOPHIA_BASE = f"{INFER_HOST}/sophia/vllm/v1"
METIS_BASE = f"{INFER_HOST}/metis/api/v1"
MINERVA_BASE = f"{INFER_HOST}/minerva/api/v1"

# Clusters that appear in list-endpoints but are NOT yet serving models. We do
# not build a provider for these; the status banner reports them as
# "provisioning" so the user knows to check back rather than assuming a bug.
# Verified 2026-08-24: tara /models -> [] and /jobs -> HTTP 500
# ("Failed to read Tara router config: FIRST_V2_REDIS_URL is not set"), i.e.
# the machine is still being accepted. Re-check and promote it to a real
# cluster once /models returns entries.
PROVISIONING_CLUSTERS = ("tara",)

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

# Minerva models report their window + reasoning class in a structured
# `capabilities` object (schema_version 1) — richer and more authoritative than
# Sophia's `max_model_len`/`reasoning_parser` pair:
#   capabilities.context_window_tokens   int
#   capabilities.reasoning.supported     bool
# Verified live 2026-08-24: gpt-oss-120b 131072, inkling-bf16 262144,
# nemotron-3-ultra 262144 — all reasoning:true, all tool_calling:true.
# NOTE the id heuristic MISSES inkling-bf16 and nemotron-3-ultra, so without the
# capabilities read they would land in the 2048-token baseline provider and
# return empty content (their reasoning channel eats the whole budget —
# reproduced: inkling-bf16 @ max_tokens=30 -> content:null finish_reason:length).
MINERVA_DEFAULT_CONTEXT = 131072

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
# Minerva fallback: verified live 2026-08-24 from capabilities.*. All three are
# reasoning models (capabilities.reasoning.supported = true), all well above the
# 64k floor.
MINERVA_FALLBACK = {
    "gpt-oss-120b": (131072, True),
    "inkling-bf16": (262144, True),
    "nemotron-3-ultra": (262144, True),
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


# Models dropped because their real serving window is below Hermes' 64k floor,
# recorded per cluster as {cluster: {model_id: context_length}} while selecting.
# The status banner prints these so a user comparing the box against the ALCF web
# UI can see WHY a model they can see there is missing here (e.g. Metis
# Mistral-Large-3-675B serves at only 8192). Populated as a side effect of the
# selectors; harmless when unread.
EXCLUDED_BY_FLOOR: dict = {}


def _note_excluded(cluster: str, mid: str, ctx: int) -> None:
    EXCLUDED_BY_FLOOR.setdefault(cluster, {})[mid] = ctx
    print(f"[populate_models] {cluster}: skip {mid} (window {ctx} < {MIN_CONTEXT})",
          file=sys.stderr)


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
            _note_excluded("sophia", mid, ctx)
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
            _note_excluded("metis", mid, ctx)
            continue
        out[mid] = (ctx, _is_reasoning_id(mid))
    return out


def _select_minerva(models: list) -> dict:
    """{model_id: (context_length, is_reasoning)} for Minerva models.

    Minerva (framework: api) publishes a structured ``capabilities`` object, so
    unlike Sophia/Metis we do NOT have to guess: read
    ``capabilities.context_window_tokens`` for the window and
    ``capabilities.reasoning.supported`` for the reasoning class, falling back to
    the id heuristic only when the field is absent. This matters — inkling-bf16
    and nemotron-3-ultra match no reasoning id pattern but ARE reasoning models,
    and the id heuristic alone would starve them of output budget.
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
        caps = m.get("capabilities") or {}
        protos = caps.get("api_protocols")
        if isinstance(protos, list) and protos and "chat_completions" not in protos:
            continue
        ctx = caps.get("context_window_tokens")
        if not (isinstance(ctx, int) and ctx > 0):
            ctx = MINERVA_DEFAULT_CONTEXT
        if ctx < MIN_CONTEXT:
            _note_excluded("minerva", mid, ctx)
            continue
        reasoning = (caps.get("reasoning") or {}).get("supported")
        is_reasoning = bool(reasoning) if reasoning is not None else _is_reasoning_id(mid)
        out[mid] = (ctx, is_reasoning)
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


def _clusters(include_metis: bool = True, include_minerva: bool = True) -> list:
    """Ordered cluster registry: (cluster, provider_name, base_url, selector, fallback).

    Single source of truth shared by build_block(), the status banner and the
    launch-provider resolver, so adding a cluster is a one-line change here.
    Provisioning clusters (see PROVISIONING_CLUSTERS) are deliberately absent —
    they have no models to offer; the banner reports them separately.
    """
    out = [("sophia", "alcf-sophia", SOPHIA_BASE, _select_sophia, SOPHIA_FALLBACK)]
    if include_metis:
        out.append(("metis", "alcf-metis", METIS_BASE, _select_metis, METIS_FALLBACK))
    if include_minerva:
        out.append(("minerva", "alcf-minerva", MINERVA_BASE,
                    _select_minerva, MINERVA_FALLBACK))
    return out


def build_block(include_metis: bool = True, include_minerva: bool = True) -> str:
    try:
        token = _get_token()
    except Exception as e:  # noqa: BLE001
        print(f"[populate_models] token fetch failed ({e}); full static fallback",
              file=sys.stderr)
        token = None

    lines = ["custom_providers:"]
    for cluster, provider, base, selector, fallback in _clusters(
            include_metis=include_metis, include_minerva=include_minerva):
        if token:
            mapping, src = _resolve_cluster(cluster, token, selector, fallback)
        else:
            mapping, src = dict(fallback), "fallback"
        lines += _emit_cluster(provider, base, mapping)
        print(f"[populate_models] {cluster}: {len(mapping)} models ({src})",
              file=sys.stderr)

    return "\n".join(lines) + "\n"


def _split_models_field(job: dict) -> list:
    """A /jobs entry's "Models" field may list several comma-joined ids."""
    return [m.strip() for m in str(job.get("Models", "")).split(",") if m.strip()]


def _fetch_job_states(cluster: str, token: str) -> tuple:
    """Return (live_ids, queued_info) from .../<cluster>/jobs.

    ``live_ids`` is the set of model ids currently RUNNING (loaded on GPU,
    instant first token). ``queued_info`` maps model id -> a short human string
    describing the pending job (estimated start time and/or the scheduler's
    comment), for models that are scheduled but NOT yet running.

    Raises on any transport/parse failure so the caller can report "unknown"
    rather than mislabel every model as offline.
    """
    url = f"{INFER_HOST}/{cluster}/jobs"
    req = urllib.request.Request(url, headers={"Authorization": "Bearer " + token})
    with urllib.request.urlopen(req, timeout=30) as r:
        doc = json.load(r)
    if not isinstance(doc, dict):
        return set(), {}

    live: set = set()
    for job in doc.get("running", []) or []:
        if not isinstance(job, dict):
            continue
        if str(job.get("Model Status", "")).lower() not in ("", "running"):
            continue
        live.update(_split_models_field(job))

    queued: dict = {}
    for job in doc.get("queued", []) or []:
        if not isinstance(job, dict):
            continue
        eta = str(job.get("Estimated Start Time") or "").strip()
        comment = str(job.get("Job Comments") or "").strip()
        # Strip the scheduler's redundant "Not Running: " prefix.
        if comment.lower().startswith("not running:"):
            comment = comment.split(":", 1)[1].strip()
        detail = f"starts ~{eta}" if eta else ""
        if comment:
            detail = f"{detail}; {comment}" if detail else comment
        for mid in _split_models_field(job):
            if mid not in live:
                queued[mid] = detail or "queued"
    return live, queued


def _fetch_hot(cluster: str, token: str) -> set:
    """Back-compat alias: the set of model ids currently RUNNING (hot)."""
    return _fetch_job_states(cluster, token)[0]


def _fmt_model(mid: str, mapping: dict, extra: str = "") -> str:
    """'model-id (128k ctx)' — the offered window, for at-a-glance reference.

    ``mapping`` is the cluster's {id: (context_length, is_reasoning)} selection;
    the window shown is exactly the ``context_length`` we write into the config
    for that model, so the banner and the dropdown can never disagree.
    """
    ctx = (mapping.get(mid) or (0,))[0]
    if ctx >= 1000:
        # 128000 -> 128k, 131072 -> 131k, 262144 -> 262k
        win = f"{round(ctx / 1000)}k ctx"
    elif ctx:
        win = f"{ctx} ctx"
    else:
        win = "ctx unknown"
    return f"{mid} ({win}{'; ' + extra if extra else ''})"


def status_report() -> int:
    """Print a per-cluster model availability banner.

    Classifies every OFFERED model against the live ``.../<cluster>/jobs`` state
    into three buckets, and annotates each with its context window:

      LIVE     — in ``running``: answers immediately.
      QUEUED   — in ``queued``: a PBS job exists but has not started; we print the
                 scheduler's estimated start time, which can be many HOURS away.
      OFFLINE  — offered by the catalog but in no job bucket: not loaded and not
                 scheduled. A request returns HTTP 503 "... is offline."

    This replaces the old HOT/cold split, which lumped QUEUED and OFFLINE together
    and hardcoded the label "cold (~10-15m)". That was actively wrong whenever a
    cluster was down: on 2026-08-24 Sophia had zero running models and a queue
    whose estimated start was ~31 hours out, yet every Sophia model was advertised
    as ready in 10-15 minutes.

    Best-effort: any failure downgrades a cluster line to "status unknown" rather
    than emitting a wrong availability claim.
    """
    try:
        token = _get_token()
    except Exception as e:  # noqa: BLE001
        print(f"[status-report] token fetch failed ({e}); skipping status banner",
              file=sys.stderr)
        return 0

    include_metis = os.environ.get("ALCF_ENABLE_METIS", "1") != "0"
    include_minerva = os.environ.get("ALCF_ENABLE_MINERVA", "1") != "0"

    lines = ["Model availability (context window shown per model):"]
    any_live = False
    for cluster, _provider, _base, selector, fallback in _clusters(
            include_metis=include_metis, include_minerva=include_minerva):
        offered, _src = _resolve_cluster(cluster, token, selector, fallback)
        try:
            live, queued = _fetch_job_states(cluster, token)
            known = True
        except Exception as e:  # noqa: BLE001
            print(f"[status-report] {cluster} /jobs failed ({e})", file=sys.stderr)
            live, queued, known = set(), {}, False

        lines.append(f"  {cluster}:")
        if not known:
            lines.append(f"    status unknown (could not read /jobs) — "
                         f"{len(offered)} models offered")
            continue

        live_ids = sorted(m for m in offered if m in live)
        queued_ids = sorted(m for m in offered if m not in live and m in queued)
        offline_ids = sorted(m for m in offered
                             if m not in live and m not in queued)
        any_live = any_live or bool(live_ids)

        if live_ids:
            lines.append("    LIVE (instant):")
            for mid in live_ids:
                lines.append(f"      - {_fmt_model(mid, offered)}")
        else:
            lines.append("    LIVE (instant): (none)")
        if queued_ids:
            lines.append("    QUEUED (job pending — see estimated start):")
            for mid in queued_ids:
                lines.append(f"      - {_fmt_model(mid, offered, queued[mid])}")
        if offline_ids:
            lines.append("    OFFLINE (not loaded, not scheduled — requests 503):")
            for mid in offline_ids:
                lines.append(f"      - {_fmt_model(mid, offered)}")

        dropped = EXCLUDED_BY_FLOOR.get(cluster) or {}
        if dropped:
            lines.append(f"    not offered — below Hermes' {MIN_CONTEXT // 1000}k "
                         f"context floor ({len(dropped)}):")
            detail = ", ".join(f"{mid} ({ctx})" for mid, ctx in sorted(dropped.items()))
            for chunk in textwrap.wrap(detail, width=76):
                lines.append(f"      {chunk}")

    for cluster in PROVISIONING_CLUSTERS:
        lines.append(f"  {cluster}:")
        lines.append("    provisioning at ALCF — no models yet; check back later")

    lines.append("  Note: a LIVE model answers immediately. Selecting an OFFLINE or")
    lines.append("  QUEUED model returns HTTP 503 until ALCF loads it — a warm-up is")
    lines.append("  ~10-15 min, but a queued job may wait HOURS for free nodes.")
    if not any_live:
        lines.append("  WARNING: no model is LIVE on any cluster right now.")
    # Emit to stdout so the entrypoint can `log` it line-by-line.
    sys.stdout.write("\n".join(lines) + "\n")
    return 0


# Back-compat alias for the previous public name.
hot_report = status_report


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
    cluster, provider = "sophia", "alcf-sophia"
    for cl, prov, cl_base, _sel, _fb in _clusters():
        if f"/{cl}/" in base or base.rstrip("/") == cl_base.rstrip("/"):
            cluster, provider = cl, prov
            break

    is_reasoning = _is_reasoning_id(model_id)
    if not is_reasoning:
        # Consult the live catalog for an authoritative reasoning signal:
        # Sophia advertises `reasoning_parser`, Minerva advertises
        # capabilities.reasoning.supported. Best-effort, never fatal.
        try:
            token = _get_token()
            for m in _fetch_models(cluster, token):
                if m.get("id") != model_id:
                    continue
                caps = m.get("capabilities") or {}
                if m.get("reasoning_parser") or (caps.get("reasoning") or {}).get("supported"):
                    is_reasoning = True
                break
        except Exception:  # noqa: BLE001
            pass

    name = f"{provider}-reasoning" if is_reasoning else provider
    return f"custom:{name}"


def main() -> int:
    import argparse
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default="", help="also write the block to this path")
    ap.add_argument("--no-metis", action="store_true",
                    help="omit the Metis provider")
    ap.add_argument("--no-minerva", action="store_true",
                    help="omit the Minerva provider")
    ap.add_argument("--status-report", "--hot-report", dest="status_report",
                    action="store_true",
                    help="print a per-cluster LIVE/QUEUED/OFFLINE availability "
                         "banner (with each model's context window) instead of "
                         "the custom_providers block (queries /jobs). "
                         "--hot-report is the deprecated alias.")
    ap.add_argument("--launch-provider", default="", metavar="MODEL_ID",
                    help="print the named custom provider the given launch model "
                         "should resolve through (e.g. custom:alcf-sophia-reasoning) "
                         "and exit")
    args = ap.parse_args()

    if args.launch_provider:
        sys.stdout.write(launch_provider(args.launch_provider) + "\n")
        return 0

    if args.status_report:
        return status_report()

    include_metis = not args.no_metis and os.environ.get("ALCF_ENABLE_METIS", "1") != "0"
    include_minerva = (not args.no_minerva
                       and os.environ.get("ALCF_ENABLE_MINERVA", "1") != "0")
    block = build_block(include_metis=include_metis, include_minerva=include_minerva)
    sys.stdout.write(block)
    if args.out:
        with open(args.out, "w", encoding="utf-8") as f:
            f.write(block)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
