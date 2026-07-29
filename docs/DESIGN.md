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
 laptop │  browser ──http://localhost:8787──▶  hermes dashboard (web chat) │
 ───────┼─────────────────────────────────────────┬────────────────────── │
        │                                          │ agent core (tools)    │
        │   ~/.globus (vol) ── Globus tokens ──────┤                       │
        │   ~/.hermes (vol) ── memory + config ────┤                       │
        └──────────────────────────────────────────┼──────────────────────┘
                                                    │
                    ┌───────────────────────────────┼───────────────────────────┐
                    ▼                                ▼                            ▼
        ALCF Inference Service          IRI Facility API                 (skills + docs
        inference-api.alcf.anl.gov      api.alcf.anl.gov                  baked into image)
        (LLM brain: gpt-oss-120b)       (job submit / fs ops)
```

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

## Two provider quirks (both handled)

1. **Reasoning models return empty content with a small output cap.** `gpt-oss-*`
   spend `max_tokens` on a hidden reasoning channel. Fix: `model.max_tokens: 2048`.
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
scripts/entrypoint.sh              first-run auth, config render, refresh loop, launch
scripts/fetch_docs.py              pulls ALCF user-guides markdown into docs/
scripts/inference_auth_token.py    vendored Globus helper (inference)
scripts/alcf_facility_api_globus_token.py  vendored Globus helper (IRI)
skills/                            alcf-inference-service, alcf-iri-facility-api, alcf-pbs...
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
- [ ] Review `Vendor_Support` project-name references in the IRI skill (generic
      example; harmless but confirm before publish).
- [ ] Confirm the Hermes fork branch has no unrelated personal patches when the
      image pins it (or pin a dedicated clean tag).

## Open items

1. `docker build` validation + a first real container run-through of the
   two-login onboarding.
2. Decide image base model default (gpt-oss-120b vs a non-reasoning model like a
   Llama/Gemma to avoid the max_tokens subtlety for naive users).
3. `git init` + create the GitHub repo under `jtchilders-ai-assistant` (or the
   `argonne-lcf` org if this becomes official).
4. Decide whether to also serve the Hermes CLI/TUI (currently dashboard-only).
5. Upstream the `strip_tool_message_name` fix to NousResearch as a PR.
