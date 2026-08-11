# ALCF Agent in a Box — Design

## Goal

Ship a Docker image that an **ALCF user** can `docker run` on a laptop and
immediately get an AI agent that (a) knows ALCF, (b) thinks using an
ALCF-hosted model, and (c) can act on ALCF systems (job submission, filesystem)
— all through a local web chat, with no external LLM provider and no API key to
manage.

## Why Hermes

[Hermes Agent](https://github.com/NousResearch/hermes-agent) already is the
thing we'd otherwise build: a provider-agnostic agent core with persistent
memory, a skills system, a terminal/tool loop, and — critically — a built-in
**web dashboard** (`hermes dashboard`) that serves a local chat UI. So the image
is Hermes + ALCF-specific content + a thin runtime wrapper, not a new framework.

## Architecture

```
        ┌─────────────────────────── container ───────────────────────────┐
        │                                                                  │
 laptop │  browser ─https://localhost:8787─▶ Caddy (TLS) ─▶ hermes         │
 ───────┼──────────────────────────────────────┬── dashboard (web chat)── │
        │  (self-signed cert; HTTPS = clipboard)│    agent core (tools)    │
        │   /opt/data (named volume) ───────────┤                          │
        │     ├─ .globus  → Globus tokens       │                          │
        │     └─ memory + config + sessions     │                          │
        └───────────────────────────────────────┼─────────────────────────┘
                                                 │
                    ┌────────────────────────────┼───────────────────────────┐
                    ▼                             ▼                            ▼
        ALCF Inference Service          IRI Facility API                 (skills + docs
        inference-api.alcf.anl.gov      api.alcf.anl.gov                  baked into image)
        (LLM brain: gemma-4-31B-it)     (job submit / fs ops)
```

A single named volume (`alcf-agent-home` → `/opt/data`) holds Globus tokens
*and* Hermes memory/config/sessions; Caddy terminates TLS on the public port
(so the chat's clipboard works) and reverse-proxies to the dashboard on
loopback inside the container.

## The LLM source: ALCF Inference Service (not Argo)

Argo is ANL-staff-only; this agent is for **users**. The ALCF Inference Service
(Sophia vLLM / Metis SambaNova) is open to ALCF users and OpenAI-compatible.

- API host: `https://inference-api.alcf.anl.gov` (NOT `inference.alcf.anl.gov`,
  which serves the Open WebUI SPA).
- Sophia base_url: `/resource_server/sophia/vllm/v1`; Metis:
  `/resource_server/metis/api/v1`.
- It's public-facing, so the agent works from an off-network laptop.

## Auth model (the load-bearing constraint)

Both ALCF services use **Globus OAuth**, but they are **two separate logins**:

| Service | Helper script | Token used for |
|---|---|---|
| Inference | `inference_auth_token.py` | The agent's LLM calls (as `api_key`) |
| IRI API | `alcf_facility_api_globus_token.py` | Job submission, filesystem ops |

An inference token sent to the IRI API returns `401 Globus token not active`.
Tokens last 48h and auto-refresh; a full re-auth is required every 30 days.

Consequences baked into the design:
- **No credentials in the image.** First run does the interactive Globus login(s);
  tokens live in the mounted `~/.globus` volume.
- The entrypoint runs a **token-refresh loop** (every 6h) that re-renders the
  Hermes config with a fresh inference access token, because the token is the
  `api_key` and it rotates.

## Startup pipeline (what the launch scripts do)

`scripts/entrypoint.sh` runs on every container start and renders the live config
before launching the dashboard. The design principle throughout is **fail-open**:
every ALCF-service call is best-effort and falls back to a safe default rather
than aborting the launch, and the one intentional hard stop (the context-floor
guard) only fires on a positive, confirmed sub-floor reading.

Order of operations in `render_config()` (re-run by the 6h token-refresh loop):

1. **Fresh inference token.** The token is the `api_key` and rotates, so it's
   re-fetched on every render.
2. **Launch-model context window** (`resolve_context_length.py`). Reads the real
   `max_model_len` from the gateway because ALCF caps some models below their
   published spec and Hermes' family table would otherwise over-estimate it.
   Defaults to 128000 on any failure (so the floor guard fails open).
3. **Model-list generation** (`populate_models.py`). Queries the live catalog on
   both clusters, filters to chat models with a real ≥64k window, and emits a
   `custom_providers:` block. Each cluster is split into a baseline provider and a
   `-reasoning` provider (see quirk #1), each carrying its own `max_tokens`.
   Reasoning classification uses the catalog `reasoning_parser` field plus an id
   heuristic. On any discovery failure it prints the committed static fallback
   block from the template, so the container always comes up with a usable list.
4. **Context-floor guard.** Hermes hard-refuses a model whose window is below
   `MINIMUM_CONTEXT_LENGTH` (64000). If the *launch* model is sub-floor, exit 78
   with an actionable list of valid models rather than a raw Hermes stacktrace.
5. **Launch-provider resolution.** Scan the *generated block* (single source of
   truth) for which provider lists the launch model, and set the top-level
   `model.provider` to that named provider (`custom:alcf-sophia[-reasoning]`).
   This is how turn one inherits the correct per-model output cap given that
   Hermes has no top-level per-model `max_tokens`. Falls back to the Python
   `--launch-provider` id heuristic, then to `custom:alcf-sophia`.
6. **Splice + substitute.** The generated block replaces the sentinel-to-EOF
   region of `config.template.yaml`, then `${VAR}` placeholders are substituted.

After the render, a **hot/cold warm-up banner** (`--hot-report`, gated by
`ALCF_SHOW_MODEL_STATUS`) reports which offered models are loaded on GPU now vs.
which will cold-start (~10–15 min, HTTP 503 until ready). Then skills / `MEMORY.md`
/ `SOUL.md` are seeded-or-refreshed (checksum-stamped so user edits are never
clobbered), the 6h refresh loop starts, and the dashboard launches behind Caddy.

## Two provider quirks (both handled)

1. **Reasoning models return empty content with a small output cap.** Reasoning
   models (`gpt-oss-*`, `gemma-4-*`, `nemotron-3-super`, `*-Thinking`) spend part
   of the `max_tokens` output budget on a hidden reasoning channel, so a small cap
   can leave them returning empty content (`finish_reason=length`). Hermes has no
   *per-model* `max_tokens` (unlike `context_length`) — it honours a per-*provider*
   `max_tokens`, and only when the top-level `model.max_tokens` is unset. Fix
   (`populate_models.py`): split each cluster into a baseline provider
   (`alcf-sophia`, `max_tokens` 2048) and a `-reasoning` provider
   (`alcf-sophia-reasoning`, `max_tokens` 12288, env-overridable via
   `ALCF_MAX_TOKENS` / `ALCF_REASONING_MAX_TOKENS`); leave top-level
   `model.max_tokens` unset so `/model` switches resolve the right cap per request;
   and point the launch model's top-level `model.provider` at whichever named
   provider matches its class so turn one gets the right cap too. Reasoning is
   detected from the gateway's `reasoning_parser` field, with an id heuristic for
   models served without it (e.g. gpt-oss).
2. **The vLLM gateway rejects `name` on `role: tool` messages** with HTTP 422
   `extra_forbidden`, breaking all agentic tool use. `name` is a *valid* Chat
   Completions field that compliant providers accept, so we can't strip it
   unconditionally. Fix: a config-gated flag added to Hermes,
   `model.strip_tool_message_name` (default off), which drops the field for this
   provider only. The image sets it to `true`.
   - Patch lives on the fork branch `feat/strip-tool-message-name`
     (`agent/transports/chat_completions.py`), with 4 unit tests; 547
     transport/provider tests pass. Upstreamable — helps anyone pointing Hermes
     at a strict OpenAI-compatible gateway.

## Verification log (spike, 2026-07-29)

All proven with real execution, not description:
- ✅ Headless Globus auth → 48h inference token.
- ✅ `curl` chat completion to Sophia `gpt-oss-120b` → HTTP 200, correct content
  (with adequate `max_tokens`).
- ✅ A/B curl proof of the `name`/422 blocker and the drop-`name` fix.
- ✅ Hermes (isolated `HERMES_HOME`) plain chat → correct answer.
- ✅ Hermes agentic tool call that previously 422'd → now returns the right
  answer with the flag on.
- ✅ `hermes dashboard` web chat drove `gpt-oss-120b` through autonomous terminal
  tool calls end-to-end.

## Repo layout

```
Dockerfile                         patched-Hermes install + content, layered
config/config.template.yaml        single ALCF inference target + both fixes
config/SOUL.md                     agent identity + "what can I do" greeting (seeded to $HERMES_HOME/SOUL.md)
scripts/entrypoint.sh              first-run auth, config render + dynamic model list, floor/launch-provider guards, refresh loop, launch
scripts/populate_models.py         generates the switchable model list from the live ALCF catalog (reasoning split, 64k floor, --hot-report, --launch-provider)
scripts/resolve_context_length.py  resolves a model's real serving window (max_model_len) for the context-floor guard
scripts/fetch_docs.py              pulls ALCF user-guides markdown into docs/
scripts/inference_auth_token.py    vendored Globus helper (inference)
scripts/alcf_facility_api_globus_token.py  vendored Globus helper (IRI)
scripts/iri_hello_world.py         one-shot IRI job submitter (write; consumes allocation)
scripts/alcf_facility.py           read-only helper: system status, my jobs, job output, allocations
skills/                            alcf-inference-service, alcf-iri-facility-api, alcf-pbs..., alcf-facility-status-and-jobs
memory/MEMORY.md                   curated, sanitized ALCF knowledge (seeded to built-in memory)
docs/                              ALCF docs snapshot (nightly CI)
.github/workflows/build.yml        build + push image to GHCR
.github/workflows/refresh-docs.yml nightly docs refresh
```

## Sanitization checklist (before publishing)

The image is shared, so it must carry only generic ALCF knowledge:
- [x] No personal ANL username / API keys in skills or config.
- [x] Memory seed is a curated public-facts file, NOT an export of a personal
      Mnemosyne bank.
- [x] Review `Vendor_Support` project-name references in the IRI skill —
      genericized to `<your project>` / "look it up via GET /account/projects"
      (removed the specific project name + allocation node-hour figures).
- [x] The pinned Hermes fork branch carries only the `strip_tool_message_name`
      patch (4 unit tests); no unrelated personal patches. Pin a dedicated clean
      tag if the branch later accumulates other commits.

## Open items

Everything in the original build plan is done and verified (image built and
run end-to-end, default model chosen — see below — GitHub repo + CI live). The
remaining items are genuinely optional follow-ups:

1. Serve the Hermes CLI/TUI in addition to the dashboard (currently
   dashboard-only; the CLI still works if you `docker exec` in).
2. Upstream the `strip_tool_message_name` fix to NousResearch as a PR. The fork
   branch `feat/strip-tool-message-name` is pushed; once merged, the
   `patches/0001-*.patch` layer becomes a no-op and can be deleted.

### Resolved (kept for history)

- ✅ `docker build` validation + a real first-run of the two-login onboarding.
- ✅ Default model decided: `google/gemma-4-31B-it` — a non-reasoning model kept
  consistently *hot* on Sophia, so it avoids both the cold-start HTTP 503 and
  gpt-oss's reasoning-token thrash under agentic load. All models stay
  switchable in the dashboard.
- ✅ Repo created under `jtchilders-ai-assistant` with GHCR publish + nightly
  docs-refresh CI. (Move to `argonne-lcf` if this becomes official.)
