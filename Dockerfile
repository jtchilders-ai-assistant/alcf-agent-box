# ALCF Agent in a Box
#
# Layers, cheap-to-expensive so edits to skills/docs/config don't rebuild the
# world:
#   1. base OS + system deps
#   2. Hermes Agent (patched fork, pinned) + Python deps  <- heavy, rarely changes
#   3. ALCF Globus auth helpers                            <- rarely changes
#   4. skills / memory seed / docs / config / entrypoint   <- changes often

FROM python:3.11-slim AS base

ARG DEBIAN_FRONTEND=noninteractive
RUN apt-get update && apt-get install -y --no-install-recommends \
        git curl ca-certificates tini gettext-base \
    && rm -rf /var/lib/apt/lists/*

# Non-root user. Home is where ~/.hermes and ~/.globus live (mount volumes here).
RUN useradd --create-home --shell /bin/bash --uid 1000 alcf
ENV HOME=/home/alcf \
    HERMES_HOME=/home/alcf/.hermes \
    PATH=/home/alcf/.local/bin:/opt/venv/bin:$PATH

# ---------------------------------------------------------------------------
# 2. Hermes Agent — install the PATCHED fork at a pinned ref.
#    The patch adds model.strip_tool_message_name (needed for ALCF tool use).
# ---------------------------------------------------------------------------
ARG HERMES_REPO=https://github.com/jtchilders-ai-assistant/hermes-agent.git
ARG HERMES_REF=feat/strip-tool-message-name

RUN python -m venv /opt/venv \
    && /opt/venv/bin/pip install --no-cache-dir --upgrade pip \
    && git clone --depth 1 --branch "${HERMES_REF}" "${HERMES_REPO}" /opt/hermes-agent \
    && /opt/venv/bin/pip install --no-cache-dir /opt/hermes-agent

# ---------------------------------------------------------------------------
# 3. ALCF Globus auth helpers (inference + IRI). Vendored so the image works
#    offline-ish and pins a known-good version of each helper script.
# ---------------------------------------------------------------------------
RUN /opt/venv/bin/pip install --no-cache-dir globus-sdk openai requests
COPY scripts/inference_auth_token.py /opt/alcf/inference_auth_token.py
COPY scripts/alcf_facility_api_globus_token.py /opt/alcf/alcf_facility_api_globus_token.py

# ---------------------------------------------------------------------------
# 4. Content that changes often — kept last for fast rebuilds.
# ---------------------------------------------------------------------------
# Skills: how the agent talks to ALCF inference / IRI / PBS.
COPY skills/ /opt/alcf/skills/
# Curated, sanitized ALCF knowledge seed (imported into memory on first run).
COPY memory/ /opt/alcf/memory/
# Snapshot of ALCF user docs (refreshed nightly by CI).
COPY docs/ /opt/alcf/docs/
# Config template + entrypoint.
COPY config/config.template.yaml /opt/alcf/config.template.yaml
COPY scripts/entrypoint.sh /opt/alcf/entrypoint.sh
RUN chmod +x /opt/alcf/entrypoint.sh && chown -R alcf:alcf /opt/alcf /home/alcf

USER alcf
WORKDIR /home/alcf

ENV ALCF_MODEL=openai/gpt-oss-120b \
    ALCF_CLUSTER=sophia \
    ALCF_MAX_TOKENS=2048 \
    ALCF_DASHBOARD_PORT=8787 \
    ALCF_ENABLE_IRI=1

EXPOSE 8787

# tini reaps zombies (the dashboard spawns child processes).
ENTRYPOINT ["/usr/bin/tini", "--", "/opt/alcf/entrypoint.sh"]
