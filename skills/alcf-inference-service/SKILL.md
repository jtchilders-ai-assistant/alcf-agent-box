---
name: alcf-inference-service
description: Call the ALCF Inference Service LLM gateway (users).
category: research
---

# ALCF Inference Service

Argonne's public-facing, OpenAI-compatible LLM inference gateway for **ALCF users**
(open science) — distinct from Argo, which is ANL-staff-only. Serves open-weight +
Argonne models (gpt-oss, Llama, Gemma, Mistral, AuroraGPT) on Sophia (vLLM) and Metis
(SambaNova). Because the API host is public-facing (not `*.inside.anl.gov`), it is
reachable from a laptop off the ANL network — no VPN required (only a valid Globus login).

Load whenever the task is "call the ALCF inference service", "use ALCF inference for
chat/embeddings", "point an agent/OpenAI client at ALCF inference", or "what models are
hot on Sophia/Metis". DIFFERENT system from Argo (`argonne-argo-api`) and the IRI Facility
API (`alcf-iri-facility-api`) — different host, and a SEPARATE Globus login from IRI.

- **Web UI (Open WebUI):** https://inference.alcf.anl.gov/ — log in with ANL/ALCF creds, pick a model, chat.
- **API host:** `https://inference-api.alcf.anl.gov`  ← the REST API. NOT `inference.alcf.anl.gov`.
- **Docs:** https://docs.alcf.anl.gov/services/inference-endpoints/
- **Auth helper repo:** https://github.com/argonne-lcf/inference-endpoints (`inference_auth_token.py`)
- **Auth:** Globus OAuth2, command-line login flow. Access tokens valid 48h (auto-refresh);
  re-auth required every 30 days.

## CRITICAL: the API host is `inference-api`, not `inference`

`inference.alcf.anl.gov` serves the **Open WebUI single-page app** and returns an HTML
document for *every* path — including things that look like API routes. If a request
returns `<!doctype html>`, you hit the wrong host. All programmatic calls go to
`https://inference-api.alcf.anl.gov`. (The docs table lists only *relative* paths like
`/resource_server/sophia/vllm/v1`; the real host lives in the docs' curl code blocks.)

## Base URLs (verified live 2026-07)

    Sophia (vLLM, full OpenAI-compat):  https://inference-api.alcf.anl.gov/resource_server/sophia/vllm/v1
    Metis  (SambaNova, chat only):      https://inference-api.alcf.anl.gov/resource_server/metis/api/v1

Sophia supports `/chat/completions`, `/completions`, `/embeddings`, `/batches`.
Metis supports `/chat/completions` only.

## Discovery & status endpoints (JSON, need Bearer token)

    GET /resource_server/list-endpoints          # all clusters + every model, grouped
    GET /resource_server/sophia/models           # model list w/ max_model_len, tool-choice flags
    GET /resource_server/sophia/jobs             # which models are HOT (running) right now
    GET /resource_server/metis/models  /metis/jobs

Use `sophia/jobs` -> `running[]` to pick a model that is already loaded. A cold model's
first request can take **10–15 min** to load (Sophia keeps ~5 nodes hot, ~5 rotating;
dynamically-loaded models unload after 2h idle).

## Authentication (Globus command-line flow)

Same mechanism/shape as the IRI API auth script but a **DIFFERENT Globus scope/app**
(see pitfalls). Interactive: prints a URL, you log in in a browser, paste the code back.
Works headless (redirect_uri is `auth.globus.org/v2/web/auth-code`, i.e. code-on-screen,
not a localhost redirect), so it runs fine inside a container / over PTY.

    python3 -m venv venv && source venv/bin/activate
    pip install openai globus_sdk requests
    curl -sL https://raw.githubusercontent.com/argonne-lcf/inference-endpoints/refs/heads/main/inference_auth_token.py -o inference_auth_token.py
    python inference_auth_token.py authenticate                       # interactive: URL + paste code
    token=$(python inference_auth_token.py get_access_token)          # 48h, auto-refresh
    python inference_auth_token.py get_time_until_token_expiration --units hours

Tokens cache at `~/.globus/app/58fdd3bc-e1c3-4ce5-80ea-8d6b87cfb944/inference_app/tokens.json`.
Force re-login: `python inference_auth_token.py authenticate --force` (after logout at
app.globus.org/logout — needed if you see `IdentityMismatchError` or `token not active`).

### Driving the interactive auth from a Hermes session (the working pattern)

Each run generates a FRESH PKCE `code_challenge`, so a URL from a previously-killed process
is useless. Do it live:
1. Start auth as a **background PTY process**: `terminal(background=true, pty=true, command="... python inference_auth_token.py authenticate")`.
2. `process(action=wait, timeout=8)` to capture the printed URL.
3. Hand the user THAT URL; wait for them to paste the code.
4. `process(action=submit, data="<code>", session_id=...)` — sends code + Enter.
5. `process(action=wait)` — exit 0 = success. Verify with `get_access_token`.

## Chat completion (OpenAI-compatible)

    curl -X POST "https://inference-api.alcf.anl.gov/resource_server/sophia/vllm/v1/chat/completions" \
      -H "Authorization: Bearer $token" -H "Content-Type: application/json" \
      -d '{"model":"openai/gpt-oss-120b","max_tokens":400,
           "messages":[{"role":"user","content":"..."}]}'

Model ids are **HuggingFace-style**: `openai/gpt-oss-120b`, `openai/gpt-oss-20b`,
`google/gemma-4-31B-it`, `meta-llama/Meta-Llama-3.1-8B-Instruct`, `argonne/AuroraGPT-*`,
embeddings `Salesforce/SFR-Embedding-Mistral`. Discover the live set via `list-endpoints`.

## PITFALL: gpt-oss reasoning models eat the max_tokens budget

`openai/gpt-oss-120b` / `gpt-oss-20b` are **reasoning models**. The visible answer is in
`choices[0].message.content`; the hidden chain-of-thought is in a separate
`choices[0].message.reasoning` field — and it **consumes the same `max_tokens` budget**.
With too small a cap the model spends the whole budget on reasoning and returns
`content: null`, `finish_reason: "length"`. Observed: `max_tokens=80` -> empty content;
`max_tokens=400` -> `content` present, `finish_reason: "stop"`.

**Implication for Hermes / any OpenAI client:** set a GENEROUS output cap for reasoning
models or you get blank replies. Reasoning models on ALCF include the gpt-oss family,
the **gemma-4 family** (they carry a `reasoning_parser` in the catalog), nemotron-3-super,
and the `*-Thinking` models; plain-chat models (AuroraGPT, Llama) don't need the extra
headroom. In Hermes, express this with a **per-provider** `max_tokens` on the
`custom_providers` entry (there is no top-level per-model `max_tokens`): put reasoning
models in a provider block with a larger cap. The `alcf-agent-box` container does exactly
this — it splits each cluster into a baseline provider (`max_tokens` 2048) and a
`-reasoning` provider (`max_tokens` 12288); see its `scripts/populate_models.py`.

## Using it as a Hermes backend

    model:
      provider: custom
      base_url: https://inference-api.alcf.anl.gov/resource_server/sophia/vllm/v1
      api_key: <globus access token>     # NOT static — 48h rotating; refresh from inference_auth_token.py
      model: openai/gpt-oss-120b

The rotating token means `api_key` can't be a durable literal — a wrapper/entrypoint must
fetch a fresh token (`get_access_token`) and inject it before/at session start.

## Pitfalls

- **Wrong host = HTML.** `inference.alcf.anl.gov` (web UI) returns `<!doctype html>` for
  every path. API calls MUST target `inference-api.alcf.anl.gov`.
- **`wget` is not on stock macOS** — the docs use `wget` for the auth script; use `curl -sL ... -o`.
- **Separate Globus login from IRI.** An inference token sent to `api.alcf.anl.gov`
  (IRI) authed endpoints returns HTTP 401 `Globus token not active`. Inference uses client
  `58fdd3bc-...` / scope `681c10cc-.../action_all`; IRI uses its own scope. A user-facing
  agent that does BOTH chat and job submission needs TWO interactive Globus logins.
- **Cold model latency.** First call to an unloaded model can take 10–15 min. Check
  `sophia/jobs` for a hot model first, or expect a long first request.
- **Not Argo, not AmSC.** Argo (`apps-stage.inside.anl.gov/argoapi`, username-as-key,
  staff-only) and AmSC MAG (LiteLLM, Ping/Dex) are different systems. This is the
  user-facing service.

## Files

- `scripts/probe_inference.sh` — re-runnable end-to-end probe (token -> list-endpoints ->
  hot-model discovery -> chat completion) with PASS/FAIL summary. Run it to verify the
  service + your token in one shot: `./scripts/probe_inference.sh <dir-with-auth-script>`.

## See also

- `argonne-argo-api` — the ANL-staff-only LLM gateway (different host + auth).
- `alcf-iri-facility-api` — job submission / filesystem via Globus (separate token).
