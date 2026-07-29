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
  -v alcf-agent-home:/home/alcf/.hermes \
  -v alcf-agent-globus:/home/alcf/.globus \
  ghcr.io/argonne-lcf/alcf-agent:latest
```

On first run the container will:

1. Ask you to authenticate to the **ALCF Inference Service** (Globus browser
   login — it prints a URL, you log in, paste back a code).
2. *(Optional, for job submission)* Ask you to authenticate to the **IRI
   Facility API** — a **separate** Globus login.
3. Launch the web chat at <http://localhost:8787>.

The two named volumes persist your Globus tokens and the agent's memory across
restarts, so you only log in occasionally (tokens last 48h and auto-refresh; a
full re-auth is required every 30 days).

> **Network:** the ALCF Inference Service endpoint (`inference-api.alcf.anl.gov`)
> is public-facing, so this works from a laptop off the ALCF network. Some IRI
> API operations may require you to be a member of the relevant ALCF project.

---

## What's inside

| Piece | Source | Purpose |
|---|---|---|
| Hermes Agent (pinned, patched) | `NousResearch/hermes-agent` fork | Agent core + web dashboard |
| ALCF skills | `skills/` | How to call ALCF inference / IRI / PBS |
| Knowledge seed | `memory/` | Curated, **sanitized** ALCF facts |
| Docs snapshot | `docs/` | Latest ALCF user docs (refreshed nightly) |
| Config template | `config/config.template.yaml` | Points Hermes at ALCF inference |
| Entrypoint | `scripts/entrypoint.sh` | First-run auth + token refresh + launch |

### Why a patched Hermes?

The ALCF Inference Service's vLLM gateway validates the Chat Completions
tool-message schema strictly and rejects a `name` field on `role: tool`
messages with HTTP 422, which breaks agentic (tool-using) sessions. Hermes was
extended with a config flag `model.strip_tool_message_name` (default off) that
strips that field for such gateways. The image sets the flag to `true`. See
[docs/DESIGN.md](docs/DESIGN.md) for the full root-cause writeup.

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
| `ALCF_ENABLE_IRI` | `1` | Prompt for the second (IRI) Globus login |

---

## Development

See [docs/DESIGN.md](docs/DESIGN.md) for architecture, the auth model, and the
verification log (spike results proving inference + agentic tool use work).

Build locally:

```bash
docker build -t alcf-agent:dev .
```

## Status

**Scaffold.** The interaction architecture is verified end-to-end (ALCF
inference drives Hermes agentically; the web dashboard works). Remaining work is
tracked in [docs/DESIGN.md](docs/DESIGN.md) → "Open items".
