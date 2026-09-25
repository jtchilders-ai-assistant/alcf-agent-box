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
- **Run commands on ALCF compute nodes** through Globus Compute, using the
  user's own account and allocation—without giving the container SSH access.

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
  -p 127.0.0.1:8787:8787 \
  -e ALCF_DASHBOARD_PASSWORD='choose-a-password' \
  -v alcf-agent-home:/opt/data \
  ghcr.io/jtchilders-ai-assistant/alcf-agent:latest
```

On first run the container will:

1. Start **ONE combined Globus login** for the ALCF Inference Service, IRI
   Facility API, and Globus Compute. It prints one URL; you log in and paste
   back one authorization code. The official package's combined login always
   requests all four supported services (including Globus Transfer). The
   `ALCF_ENABLE_IRI` and `ALCF_ENABLE_GLOBUS_COMPUTE` flags disable the box's
   corresponding runtime features, not scopes in that combined consent.
2. Launch the web chat at **<http://localhost:8787>**. Chrome treats localhost
   as a trustworthy secure context, so chat copy/paste works without a local
   certificate or browser warning. Log in with username `alcf` (override
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
docker run -it --rm -p 127.0.0.1:8787:8787 \
  -e ALCF_DASHBOARD_PASSWORD='choose-a-password' \
  -v alcf-agent-home:/opt/data \
  -v "$HOME/alcf-work:/work" \
  ghcr.io/jtchilders-ai-assistant/alcf-agent:latest
```

Then ask the agent to read/write under `/work`. Only that directory is exposed.

> **Security:** publish the dashboard exactly as shown, on host `127.0.0.1`
> only. Browsers grant `http://localhost` secure-context privileges for clipboard
> access, while the dashboard username/password gate remains enabled. Do not
> replace the mapping with `-p 8787:8787`, which exposes it on every interface.
>
> **Reauthentication:** all enabled ALCF services share the combined login. If
> the agent reports expired or missing credentials, renew them with `alcf-tokens login`:
> ```bash
> docker exec -it <container> \
>   /opt/hermes/.venv/bin/alcf-tokens login
> ```
> The official `alcf-tokens login` default always requests all four ALCF services
> (Inference, IRI, Globus Compute, and Transfer) in one browser visit. One fresh
> login is sufficient after an upgrade (the client ID changed).

### Compute-node shell (MCP `bash` tool) & subagents

When the base Hermes supports MCP, the agent's compute-node shell is also
exposed to the model as a native **`bash` tool**: one warm node held across the
conversation, with `cd` / `export` / `module load` persisting between commands
like a real login shell. Recommended knobs at `docker run`:

- `-e ALCF_BASH_ACCOUNT=<project>` — default ALCF project to charge (otherwise
  the agent looks one up or asks you, then it sticks for the session).
- `-e ALCF_BASH_ENDPOINT=polaris|crux|sophia|edith`, `-e ALCF_BASH_QUEUE=...`,
  `-e ALCF_BASH_WALLTIME=H:MM:SS` — where the node comes from (defaults:
  polaris / debug / 1:00:00). `-e ALCF_ENABLE_MCP_BASH=0` removes the tool.
- `-e ALCF_DELEGATION_MODEL=google/gemma-4-26B-A4B-it` — run `delegate_task`
  subagents (e.g. build/test workers) on a **262k-context** model while your
  chat stays on the default. Defaults to the chat model if unset.
> The token is stored in the `alcf-agent-home` volume, so it persists.
>
> **Network:** the ALCF Inference Service endpoint (`inference-api.alcf.anl.gov`)
> is public-facing, so this works from a laptop off the ALCF network. Some IRI
> API operations may require membership in the relevant ALCF project.

---

## What's inside

| Piece | Source | Purpose |
|---|---|---|
| Hermes Agent (official image) | `nousresearch/hermes-agent:v2026.7.30` | Agent core + web dashboard (pinned base image) |
| Tool-message patch | `patches/0001-strip-tool-message-name.patch` | Enables agentic tool use on the ALCF gateway |
| ALCF skills | `skills/` | How to call ALCF inference / IRI / PBS |
| Knowledge seed | `memory/MEMORY.md` | Curated, **sanitized** ALCF facts (always injected) |
| Docs snapshot | `docs/` | Latest ALCF user docs (refreshed nightly) |
| Config template | `config/config.template.yaml` | Points Hermes at ALCF inference; carries the static model-list fallback |
| Model-list generator | `scripts/populate_models.py` | Builds the switchable model list from the live ALCF catalog at start (reasoning split, 64k floor, availability report) |
| Entrypoint | `scripts/entrypoint.sh` | First-run auth, config render, dynamic model list, launch-provider + context-floor guard, token-refresh loop, launch |

### Architecture: built on the official Hermes image

This image is `FROM nousresearch/hermes-agent:v2026.7.30` plus three thin layers
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
| `ALCF_ENABLE_IRI` | `1` | Enable IRI job/filesystem tools (the official combined login still requests the IRI credential when disabled) |
| `ALCF_ENABLE_GLOBUS_COMPUTE` | `1` | Enable compute-node execution (the official combined login still requests the Compute credential when disabled) |
| `ALCF_ENABLE_METIS` | `1` | Include the Metis cluster's models in the switchable list |
| `ALCF_ENABLE_MINERVA` | `1` | Include the Minerva cluster's models in the switchable list |
| `ALCF_SHOW_MODEL_STATUS` | `1` | Print the model availability banner (LIVE/QUEUED/OFFLINE + context windows) at startup |
| `ALCF_BASH_ACCOUNT` | *(unset)* | **Recommended:** your default ALCF project for the compute-node `bash` tool. When set, the agent holds ONE warm compute node across the whole conversation instead of paying repeated cold starts; when unset, it must ask/look up a project first. |
## What happens at container start

`scripts/entrypoint.sh` is the launch script. On every start (not just the first
run) it performs the following steps before handing off to the Hermes dashboard.
All of the ALCF-service calls below are **best-effort and non-fatal** — if the
network or catalog is unavailable, each step falls back to a safe default rather
than aborting the launch.

1. **Globus authentication.** On first run, the pinned official
   `alcf-tokens==0.3.0` package performs ONE combined Globus login for Inference,
   IRI, Globus Compute, and Globus Transfer. It applies the ALCF all-services
   policy and stores refreshable per-service credentials in `/opt/data`.
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
4. **Model availability banner** (`populate_models.py --status-report`, unless
   `ALCF_SHOW_MODEL_STATUS=0`): classifies every offered model as **LIVE**
   (loaded on GPU now), **QUEUED** (a job exists but hasn't started — the
   scheduler's estimated start is printed, and can be hours out) or **OFFLINE**
   (not loaded, not scheduled — requests 503), and prints each model's context
   window. Models dropped for being under the 64k floor are listed too, so the
   box's list can be reconciled against the ALCF web UI.
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
   dashboard starts behind the loopback-only Caddy HTTP proxy.

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
- **`alcf-minerva-reasoning`** — Minerva models (`gpt-oss-120b`, `inkling-bf16`,
  `nemotron-3-ultra`; drop with `-e ALCF_ENABLE_MINERVA=0`)

A fourth ALCF cluster, **`tara`**, is registered upstream but is still being
provisioned (it serves no models yet), so it gets no provider. The startup banner
reports it as provisioning; it will appear automatically once it serves models.

Three behaviors are worth knowing:

1. **64k context floor.** Hermes refuses to load any model whose *real* serving
   window is below 64,000 tokens. ALCF caps many models well under that (all
   Llama 3.x/4, Mixtral, Devstral, Mistral-Large-2407 serve at 16k–32k; Metis
   `Mistral-Large-3-675B` serves at just **8192**), so those are **intentionally
   excluded** from the list — they would be broken dropdown entries. The
   generator reads each model's true window from the gateway rather than trusting
   the published spec, and the startup banner names the excluded models with
   their windows so you can reconcile the box's list against the ALCF web UI.

2. **Reasoning vs. plain chat is a separate provider, with a bigger output cap.**
   Reasoning models (gpt-oss, gemma-4, nemotron-3-super, `*-Thinking`) spend part
   of the `max_tokens` output budget on a hidden reasoning channel, so a small cap
   can leave them returning empty responses. The generator detects reasoning
   models (Minerva's `capabilities.reasoning.supported` where available, else the
   gateway's `reasoning_parser` field, plus an id heuristic for models served
   without either) and puts them in the `-reasoning` provider with a larger
   per-response cap — `ALCF_REASONING_MAX_TOKENS` (default **12288**) vs.
   `ALCF_MAX_TOKENS` (default **2048**) for plain chat. The launch model is
   automatically pointed at whichever provider matches its class, so the first
   turn already gets the right cap.

3. **LIVE vs. QUEUED vs. OFFLINE (the HTTP 503 you might see).** All models share
   the same endpoint + Globus token, so *switching* is instant — but ALCF only
   keeps a subset loaded on GPU at any moment. At startup the container prints an
   **availability banner** (`populate_models.py --status-report`) that classifies
   every offered model and shows its context window:

   - **LIVE** — loaded on GPU, answers immediately.
   - **QUEUED** — a job exists but hasn't started. The banner prints the
     scheduler's estimated start, which can be **hours** away when the cluster has
     no free nodes.
   - **OFFLINE** — not loaded and not scheduled; requests return
     `HTTP 503 "... is offline."`

   A model that is merely warming up returns `HTTP 503 "online but not ready"` for
   ~10–15 min and then works — that looks like a failure but isn't. A whole
   cluster can also be down, in which case *every* model on it shows OFFLINE.
   Suppress the banner with `-e ALCF_SHOW_MODEL_STATUS=0`.

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
Experimental cluster-resident agents and development campaigns are maintained
separately in the private `alcf-agent-experiments` repository; this repository
contains only the laptop product.

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
