# ALCF Agent in a Box

A ready-to-run AI agent for **ALCF users**, packaged as a Docker image. Check it
out, run one command, log in with your ALCF/Globus credentials, and get a local
web chat that can:

- **Answer questions about ALCF** using the latest ALCF documentation and a
  curated knowledge base (baked into the image).
- **Run inference on the [ALCF Inference Service]** (Sophia / Metis) — no
  external LLM provider, no API key to manage. The agent's own brain *is* an
  ALCF-hosted open model (default `google/gemma-4-31B-it`).
- **Submit and manage jobs** on ALCF systems (Polaris, Crux, Aurora) through the
  [IRI Facility API], plus filesystem operations on Home/Eagle.

It is built on [Hermes Agent] (Nous Research) — an open-source, provider-agnostic
agent framework with persistent memory, skills, and a built-in web dashboard.

> ⚠️ **Independent tool — not an official ALCF/Argonne/DOE product**, and it runs
> an **autonomous AI agent** that can be wrong and that takes real actions with
> your credentials (jobs, node-hours, files). **You are responsible for what it
> does.** Please read [DISCLAIMER.md](DISCLAIMER.md) before using it.

[ALCF Inference Service]: https://docs.alcf.anl.gov/services/inference-endpoints/
[IRI Facility API]: https://docs.alcf.anl.gov/services/iri-api/
[Hermes Agent]: https://github.com/NousResearch/hermes-agent

---

## Quick start

```bash
docker run -it --rm \
  -p 8787:8787 \
  -e ALCF_DASHBOARD_PASSWORD='choose-a-password' \
  -v alcf-agent-home:/opt/data \
  ghcr.io/jtchilders-ai-assistant/alcf-agent:latest
```

On first run the container will:

1. Ask you to authenticate to the **ALCF Inference Service** (Globus browser
   login — it prints a URL, you log in, paste back a code).
2. *(Optional, for job submission)* Ask you to authenticate to the **IRI
   Facility API** — a **separate** Globus login. Set `-e ALCF_ENABLE_IRI=0` to
   skip it and use chat only.
3. Launch the web chat at **<https://localhost:8787>** (note **https**). Your
   browser will show a one-time "not private" warning because the container uses
   a **self-signed certificate** — click **Advanced → proceed to localhost**.
   (HTTPS is required so the chat's copy/paste works — browsers only allow
   clipboard access on secure origins.) Log in with username `alcf` (override
   with `-e ALCF_DASHBOARD_USER=...`) and the password you set. If you don't set
   `ALCF_DASHBOARD_PASSWORD`, the container generates one and prints it at
   startup.

The single `alcf-agent-home` volume persists your Globus tokens **and** the
agent's memory across restarts, so you only log in occasionally (tokens last 48h
and auto-refresh; a full re-auth is required every 30 days).

## Data & filesystem access (sandboxed by design)

**The agent cannot see or touch your laptop's files.** It runs fully inside the
container. Its file and terminal tools only reach the container's own
filesystem:

- `/opt/data` — the one **named Docker volume** (`alcf-agent-home`). This is
  Docker-managed storage, **not** a folder in your home directory. It holds the
  agent's config, memory, Globus tokens, and session history so they survive
  restarts.
- `/opt/alcf`, `/opt/hermes` — baked-in ALCF content and the agent code.

Nothing under your host home (`~/Documents`, `~/anl`, etc.) is bind-mounted, so
the agent can't read or modify your local files, and `--rm` discards the
container on exit (only the named volume persists). This is intentional: a
support tool shouldn't have ambient access to a user's machine.

If you *want* the agent to work with local files, add an explicit bind mount of
a **dedicated** directory (never your whole home):

```bash
# creates/uses ~/alcf-work on your host, visible to the agent at /work
docker run -it --rm -p 8787:8787 \
  -e ALCF_DASHBOARD_PASSWORD='choose-a-password' \
  -v alcf-agent-home:/opt/data \
  -v "$HOME/alcf-work:/work" \
  ghcr.io/jtchilders-ai-assistant/alcf-agent:latest
```

Then ask the agent to read/write under `/work`. Only that directory is exposed.

> **Security:** the dashboard runs behind a built-in **Caddy HTTPS** proxy
> (self-signed local cert) so the chat's clipboard works, and it requires the
> username/password auth gate. Keep the published port bound to localhost.
>
> **IRI job submission:** the IRI Facility API uses a **separate** Globus login
> from inference. If you skipped it at startup (or the agent reports it can't
> find IRI credentials), run this once and follow the prompts:
> ```bash
> docker exec -it <container> \
>   /opt/hermes/.venv/bin/python /opt/alcf/alcf_facility_api_globus_token.py authenticate
> ```
> The token is stored in the `alcf-agent-home` volume, so it persists.
>
> **Network:** the ALCF Inference Service endpoint (`inference-api.alcf.anl.gov`)
> is public-facing, so this works from a laptop off the ALCF network. Some IRI
> API operations may require membership in the relevant ALCF project.

---

## What's inside

| Piece | Source | Purpose |
|---|---|---|
| Hermes Agent (official image) | `nousresearch/hermes-agent:latest` | Agent core + web dashboard (base image) |
| Tool-message patch | `patches/0001-strip-tool-message-name.patch` | Enables agentic tool use on the ALCF gateway |
| ALCF skills | `skills/` | How to call ALCF inference / IRI / PBS |
| Knowledge seed | `memory/MEMORY.md` | Curated, **sanitized** ALCF facts (always injected) |
| Docs snapshot | `docs/` | Latest ALCF user docs (refreshed nightly) |
| Config template | `config/config.template.yaml` | Points Hermes at ALCF inference; carries the static model-list fallback |
| Model-list generator | `scripts/populate_models.py` | Builds the switchable model list from the live ALCF catalog at start (reasoning split, 64k floor, hot/cold report) |
| Entrypoint | `scripts/entrypoint.sh` | First-run auth, config render, dynamic model list, launch-provider + context-floor guard, token-refresh loop, launch |

### Architecture: built on the official Hermes image

This image is `FROM nousresearch/hermes-agent:latest` plus three thin layers
(patch, Globus auth helpers, ALCF content). We deliberately do **not** rebuild
Hermes — the official image already handles the fixed SQLite build, s6-overlay
supervision, the editable install, and the dashboard. See
[docs/DESIGN.md](docs/DESIGN.md).

### Why a patched Hermes?

The ALCF Inference Service's vLLM gateway validates the Chat Completions
tool-message schema strictly and rejects a `name` field on `role: tool`
messages with HTTP 422, which breaks agentic (tool-using) sessions. Hermes was
extended with a config flag `model.strip_tool_message_name` (default off) that
strips that field for such gateways. The image applies the patch and sets the
flag to `true`. The fix is upstreamable; once merged the patch layer becomes a
no-op. See [docs/DESIGN.md](docs/DESIGN.md) for the full root-cause writeup.

---

## Configuration knobs

Everything is driven by `config/config.template.yaml`, rendered into the
running config at container start. Environment variables you can override at
`docker run` time:

| Env var | Default | Meaning |
|---|---|---|
| `ALCF_MODEL` | `google/gemma-4-31B-it` | Model id on the inference service |
| `ALCF_CLUSTER` | `sophia` | `sophia` (vLLM) or `metis` (SambaNova) |
| `ALCF_MAX_TOKENS` | `2048` | Baseline per-response output cap for **plain chat** models |
| `ALCF_REASONING_MAX_TOKENS` | `12288` | Per-response output cap for **reasoning** models (gpt-oss, gemma-4, nemotron-3-super, *-Thinking). They spend part of the output budget on a hidden reasoning channel, so they need more headroom than chat models. |
| `ALCF_DASHBOARD_PORT` | `8787` | Web chat port inside the container |
| `ALCF_DASHBOARD_USER` | `alcf` | Dashboard login username |
| `ALCF_DASHBOARD_PASSWORD` | *(auto-generated + printed)* | Dashboard login password (hashed at start; plaintext never stored) |
| `ALCF_ENABLE_IRI` | `1` | Prompt for the second (IRI) Globus login |
| `ALCF_ENABLE_METIS` | `1` | Include the Metis cluster's models in the switchable list |
| `ALCF_SHOW_MODEL_STATUS` | `1` | Print the hot/cold model warm-up banner at startup |

## What happens at container start

`scripts/entrypoint.sh` is the launch script. On every start (not just the first
run) it performs the following steps before handing off to the Hermes dashboard.
All of the ALCF-service calls below are **best-effort and non-fatal** — if the
network or catalog is unavailable, each step falls back to a safe default rather
than aborting the launch.

1. **Globus authentication.** On first run, prompts for the **Inference Service**
   login (and, unless `ALCF_ENABLE_IRI=0`, the separate **IRI Facility API**
   login). Tokens are stored in the `/opt/data` volume; later starts reuse them.
2. **Dashboard auth gate.** Hashes the dashboard password (from
   `ALCF_DASHBOARD_PASSWORD`, or an auto-generated one printed once). Hermes
   refuses a non-loopback bind without this gate.
3. **Config render** (`render_config`), which builds the running config from
   `config/config.template.yaml`:
   - **Fetch a fresh inference token** — it's the `api_key`, and it rotates.
   - **Resolve the launch model's real context window**
     (`resolve_context_length.py`) from the gateway's `max_model_len`, because
     ALCF caps some models below their published spec and Hermes must be told the
     true window. Falls back to 128000 on any lookup failure.
   - **Generate the switchable model list** (`populate_models.py`) from the live
     catalog on both clusters: filter to chat models, drop anything below the 64k
     context floor, split each cluster into baseline vs. `-reasoning` providers,
     and stamp each provider's per-response output cap (`ALCF_MAX_TOKENS` /
     `ALCF_REASONING_MAX_TOKENS`). On discovery failure it emits the committed
     static fallback block instead. (See **Switching models** above.)
   - **Context-floor guard.** If the *launch* model's real window is under 64k,
     refuse to start (exit 78) with an actionable list of valid ≥64k models,
     instead of letting Hermes throw a raw stacktrace at load.
   - **Launch-provider resolution.** Find which generated provider lists the
     launch model and point the top-level `model:` block at it
     (`custom:alcf-sophia`, `custom:alcf-sophia-reasoning`, …), so the very first
     turn inherits the correct per-model output cap.
   - **Splice + substitute** the providers block into the template and write the
     final config.
4. **Hot/cold warm-up banner** (`populate_models.py --hot-report`, unless
   `ALCF_SHOW_MODEL_STATUS=0`): prints which offered models are loaded on GPU
   *now* vs. which will cold-start (~10–15 min, HTTP 503 until ready).
5. **Seed / refresh skills, memory, and SOUL.** ALCF skills, `MEMORY.md`, and the
   agent's `SOUL.md` identity are image-managed: refreshed from the image on each
   start **only if you haven't edited your copy** (tracked by checksum stamps), so
   knowledge-base fixes reach existing volumes without clobbering user edits. A
   stock/legacy Hermes `SOUL.md` with no ALCF stamp is replaced so the ALCF
   identity always lands.
6. **Token-refresh loop + launch.** A background loop re-renders the config with a
   fresh inference token every 6h (tokens last 48h; a full re-auth is required
   every 30 days). If a refresh fails — usually the 30-day limit — it logs a loud
   banner and drops a status file the agent surfaces *in chat*. Then the Hermes
   dashboard starts behind the Caddy HTTPS proxy.

## Memory & documentation

The agent's ALCF knowledge is delivered in two complementary tiers:

1. **`MEMORY.md` — always-injected knowledge base.** A curated distillation of
   ALCF facts (endpoints, auth model, systems, "you are an ALCF support agent")
   that Hermes injects into every turn. Seeded from `memory/MEMORY.md` into the
   data volume on first run; the user can edit it and their edits are preserved.
   Keep it compact — it costs tokens on every message.

2. **`docs/` — full ALCF docs snapshot, read on demand.** `scripts/fetch_docs.py`
   pulls full ALCF user-guide pages (inference, IRI API, running jobs, example
   scripts, Polaris/Aurora getting-started, filesystems, allocations) into
   `docs/`, baked into the image at `/opt/alcf/docs/`. These are the
   authoritative detail source: the agent searches/reads them with its file tool
   when a question needs specifics. `MEMORY.md` carries an index of these pages
   so the agent knows what's available and when to read each. A nightly GitHub
   Action refreshes the snapshot, so `:latest` is never more than a day stale.

Why not a vector/RAG memory store? The official Hermes image ships only the
built-in `MEMORY.md` memory plus opt-in external providers; there is no bundled
local RAG backend. The `MEMORY.md` (orientation) + on-disk docs (deep lookup by
the file tool) design is backend-agnostic, needs no extra infrastructure, and
works out of the box. To add retrieval later, configure an external memory
provider (`hermes memory setup`) and ingest `docs/` into it.

## Switching models

The ALCF Inference Service serves 40+ models across two clusters (**Sophia**,
vLLM; **Metis**, SambaNova), but its gateway doesn't expose the standard OpenAI
`/v1/models` discovery path in the place Hermes expects. So instead of shipping a
hand-maintained list, the container **generates the switchable model list at
startup** from the live ALCF catalog (`scripts/populate_models.py`) and writes it
into the running config. This means the dropdown tracks what ALCF actually serves,
without a rebuild.

In the web chat, click the **MODEL** selector and pick a provider + model. The
providers are named by cluster and reasoning class:

- **`alcf-sophia`** — Sophia plain-chat models (e.g. `argonne/AuroraGPT-IT-v4-0125`)
- **`alcf-sophia-reasoning`** — Sophia reasoning models (`google/gemma-4-31B-it`
  (default), `openai/gpt-oss-120b`, `openai/gpt-oss-20b`,
  `nvidia/nemotron-3-super-120b`, `arcee-ai/Trinity-Large-Thinking-…`)
- **`alcf-metis`** / **`alcf-metis-reasoning`** — the Metis equivalents (drop the
  Metis providers with `-e ALCF_ENABLE_METIS=0`)

Three behaviors are worth knowing:

1. **64k context floor.** Hermes refuses to load any model whose *real* serving
   window is below 64,000 tokens. ALCF caps many models well under that (all
   Llama 3.x/4, Mixtral, Devstral, Mistral-Large-2407 serve at 16k–32k), so those
   are **intentionally excluded** from the list — they would be broken dropdown
   entries. The generator reads each model's true `max_model_len` from the gateway
   rather than trusting the published spec.

2. **Reasoning vs. plain chat is a separate provider, with a bigger output cap.**
   Reasoning models (gpt-oss, gemma-4, nemotron-3-super, `*-Thinking`) spend part
   of the `max_tokens` output budget on a hidden reasoning channel, so a small cap
   can leave them returning empty responses. The generator detects reasoning
   models (via the gateway's `reasoning_parser` field, plus an id heuristic for
   models served without it) and puts them in the `-reasoning` provider with a
   larger per-response cap — `ALCF_REASONING_MAX_TOKENS` (default **12288**) vs.
   `ALCF_MAX_TOKENS` (default **2048**) for plain chat. The launch model is
   automatically pointed at whichever provider matches its class, so the first
   turn already gets the right cap.

3. **Hot vs. cold (the HTTP 503 you might see).** All models share the same
   endpoint + Globus token, so *switching* is instant — but ALCF only keeps a
   subset loaded on GPU at any moment. Selecting a **cold** model triggers a
   ~10–15 min load and returns `HTTP 503 "online but not ready"` until it warms
   up. That looks like a failure but isn't. At startup the container prints a
   **hot/cold banner** (`populate_models.py --hot-report`) showing which offered
   models are hot *right now*, so you can pick an instant one or know to wait.
   Suppress it with `-e ALCF_SHOW_MODEL_STATUS=0`.

If the live catalog is unreachable at startup, the generator falls back to a
committed static list in `config/config.template.yaml`, so the container always
comes up with a usable (if possibly slightly stale) set of models.

For the **full live catalog** (embeddings, GenSLM science models, everything —
including the sub-64k models that can't be the agent's brain), just ask the
agent: *"what models are available on ALCF inference?"* — it queries the service
directly via its `alcf-inference-service` skill.

---

## Development

See [docs/DESIGN.md](docs/DESIGN.md) for architecture, the auth model, and the
verification log (spike results proving inference + agentic tool use work).

Build locally:

```bash
docker build -t alcf-agent:dev .
```

## Status

**Working, verified end-to-end in a real container.** A `docker run` does the
Globus login, persists the token to the volume (subsequent starts skip re-auth),
serves an auth-gated web chat, and the in-container agent drives ALCF inference
(default `google/gemma-4-31B-it`) *with working tool calls*. Remaining polish is
tracked in
[docs/DESIGN.md](docs/DESIGN.md) → "Open items".

---

## License & disclaimer

Licensed under the **Apache License, Version 2.0** — see [LICENSE](LICENSE) and
[NOTICE](NOTICE). This includes an explicit disclaimer of warranty and
limitation of liability.

This is an **independent community project**, not an official ALCF/Argonne/DOE
product, and it runs an **autonomous AI agent that can be wrong and that acts
with your credentials**. Before using it, read **[DISCLAIMER.md](DISCLAIMER.md)** —
it covers the non-affiliation, the agent's autonomy and fallibility, and your
responsibility for the actions it takes on your behalf.
