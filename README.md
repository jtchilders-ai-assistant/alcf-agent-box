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
| Config template | `config/config.template.yaml` | Points Hermes at ALCF inference |
| Entrypoint | `scripts/entrypoint.sh` | First-run auth + token refresh + launch |

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
| `ALCF_MAX_TOKENS` | `2048` | Output cap (reasoning models need headroom) |
| `ALCF_DASHBOARD_PORT` | `8787` | Web chat port inside the container |
| `ALCF_DASHBOARD_USER` | `alcf` | Dashboard login username |
| `ALCF_DASHBOARD_PASSWORD` | *(auto-generated + printed)* | Dashboard login password (hashed at start; plaintext never stored) |
| `ALCF_ENABLE_IRI` | `1` | Prompt for the second (IRI) Globus login |

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

The ALCF Inference Service serves 40+ models, but its gateway doesn't expose the
standard OpenAI `/v1/models` path, so the image ships a **curated switchable
list** (in `config/config.template.yaml` under `custom_providers`). In the web
chat, click the **MODEL** selector → **alcf-inference** and pick one:

- `google/gemma-4-31B-it` (default) — non-reasoning, kept consistently hot
- `openai/gpt-oss-120b`, `openai/gpt-oss-20b` — reasoning models
- `meta-llama/Llama-3.3-70B-Instruct`, `Meta-Llama-3.1-8B-Instruct`,
  `Llama-4-Maverick-…`, `Llama-4-Scout-…`
- `mistralai/Mixtral-8x22B-…`,
  `mistralai/Mistral-Large-…`, `nvidia/nemotron-3-super-120b`
- `argonne/AuroraGPT-IT-v4-0125` (Argonne's own model)

All share the same endpoint + Globus token, so switching is instant (a cold
model may take 10-15 min to load on its first request). The **gpt-oss** models
are *reasoning* models (hidden reasoning consumes the token budget); the
non-reasoning models feel snappier for plain chat.

For the **full live catalog** (embeddings, GenSLM science models, everything),
just ask the agent: *"what models are available on ALCF inference?"* — it queries
the service directly via its `alcf-inference-service` skill. To add more to the
dropdown, edit the `models:` list in the config template and rebuild.

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
