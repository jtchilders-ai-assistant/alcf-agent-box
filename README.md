# ALCF Agent in a Box

A ready-to-run AI agent for **ALCF users**, packaged as a Docker image. Check it
out, run one command, log in with your ALCF/Globus credentials, and get a local
web chat that can:

- **Answer questions about ALCF** using the latest ALCF documentation and a
  curated knowledge base (baked into the image).
- **Run inference on the [ALCF Inference Service]** (Sophia / Metis) — no
  external LLM provider, no API key to manage. The agent's own brain *is* an
  ALCF-hosted open model (e.g. `gpt-oss-120b`).
- **Submit and manage jobs** on ALCF systems (Polaris, Crux, Aurora) through the
  [IRI Facility API], plus filesystem operations on Home/Eagle.

It is built on [Hermes Agent] (Nous Research) — an open-source, provider-agnostic
agent framework with persistent memory, skills, and a built-in web dashboard.

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
3. Launch the web chat at <http://localhost:8787>. Log in with username `alcf`
   (override with `-e ALCF_DASHBOARD_USER=...`) and the password you set. If you
   don't set `ALCF_DASHBOARD_PASSWORD`, the container generates one and prints it
   at startup.

The single `alcf-agent-home` volume persists your Globus tokens **and** the
agent's memory across restarts, so you only log in occasionally (tokens last 48h
and auto-refresh; a full re-auth is required every 30 days).

> **Security:** the dashboard binds `0.0.0.0` inside the container (so `-p`
> works) and therefore requires an auth gate — that's why a password is
> mandatory. Keep the published port bound to localhost.
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
| `ALCF_MODEL` | `openai/gpt-oss-120b` | Model id on the inference service |
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
(`gpt-oss-120b`) *with working tool calls*. Remaining polish is tracked in
[docs/DESIGN.md](docs/DESIGN.md) → "Open items".
