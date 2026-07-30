# ALCF Agent in a Box  (Option A: extend the official Hermes image)
#
# We build ON TOP of the official, hardened Hermes image
# (nousresearch/hermes-agent) rather than reinventing its build. That image
# already handles the fixed SQLite build, s6-overlay supervision, editable
# install, config/skills seeding (docker/stage2-hook.sh), the /opt/data volume,
# and the web dashboard. We add exactly three things:
#
#   1. A one-file patch enabling model.strip_tool_message_name (required because
#      the ALCF vLLM gateway rejects `name` on role:tool messages -> HTTP 422).
#      Upstreamable; once merged this layer becomes a no-op and can be deleted.
#   2. ALCF Globus auth helpers (inference + IRI) + their Python deps.
#   3. ALCF content baked as image defaults: skills, curated MEMORY.md, a docs
#      snapshot, the config template, and our entrypoint that does first-run
#      Globus auth, renders the config with a fresh token, and launches the
#      dashboard.
#
# Pin the base by tag for reproducibility; bump deliberately.
ARG HERMES_BASE=nousresearch/hermes-agent:latest
FROM ${HERMES_BASE}

# The base image sets USER/ENV/ENTRYPOINT for the stock Hermes runtime. We need
# root to patch the (root-owned, read-only) install tree and drop in content.
USER root

# ---------------------------------------------------------------------------
# 0. Caddy (TLS terminator). The dashboard chat's copy/paste uses the browser
#    Clipboard API, which browsers gate to HTTPS ("secure context"). Caddy
#    serves the dashboard over HTTPS with a local self-signed cert so paste
#    works. Single static multi-arch binary copied from the official image.
# ---------------------------------------------------------------------------
COPY --from=caddy:2 /usr/bin/caddy /usr/bin/caddy
COPY config/Caddyfile /opt/alcf/Caddyfile

# ---------------------------------------------------------------------------
# 1. Apply the strip_tool_message_name patch to the installed Hermes source.
#    git is present in the base image; `patch` is not. Applied against
#    /opt/hermes which is a git-tracked install tree.
# ---------------------------------------------------------------------------
COPY patches/0001-strip-tool-message-name.patch /tmp/alcf/0001.patch
RUN cd /opt/hermes \
    && git apply --unsafe-paths --directory=/opt/hermes /tmp/alcf/0001.patch \
    && grep -q strip_tool_message_name agent/transports/chat_completions.py \
    && echo "ALCF patch applied: strip_tool_message_name present" \
    && rm -rf /tmp/alcf

# ---------------------------------------------------------------------------
# 2. ALCF Globus auth helpers + deps. The base image's venv is uv-managed
#    (no pip binary), so install with `uv pip` into that interpreter. requests
#    is already present in the base; globus-sdk is added here.
# ---------------------------------------------------------------------------
RUN uv pip install --python /opt/hermes/.venv/bin/python --no-cache globus-sdk requests
COPY scripts/inference_auth_token.py /opt/alcf/inference_auth_token.py
COPY scripts/alcf_facility_api_globus_token.py /opt/alcf/alcf_facility_api_globus_token.py

# ---------------------------------------------------------------------------
# 3. ALCF content (changes most often -> last for cache friendliness).
#    /opt/alcf is the staging area; the entrypoint copies skills + MEMORY.md
#    into $HERMES_HOME (/opt/data) on first run so they land on the durable
#    volume without clobbering user edits.
# ---------------------------------------------------------------------------
COPY skills/  /opt/alcf/skills/
COPY memory/  /opt/alcf/memory/
COPY docs/    /opt/alcf/docs/
COPY config/config.template.yaml /opt/alcf/config.template.yaml
COPY scripts/entrypoint.sh /opt/alcf/entrypoint.sh
RUN chmod +x /opt/alcf/entrypoint.sh /opt/alcf/inference_auth_token.py \
             /opt/alcf/alcf_facility_api_globus_token.py \
    && chown -R hermes:hermes /opt/alcf

# ALCF defaults (override at `docker run` with -e).
ENV ALCF_MODEL=openai/gpt-oss-120b \
    ALCF_CLUSTER=sophia \
    ALCF_MAX_TOKENS=2048 \
    ALCF_DASHBOARD_PORT=8787 \
    ALCF_DASHBOARD_INTERNAL_PORT=9119 \
    ALCF_ENABLE_IRI=1 \
    HERMES_HOME=/opt/data \
    XDG_DATA_HOME=/opt/data/.local/share \
    XDG_CONFIG_HOME=/opt/data/.config

EXPOSE 8787

# Our entrypoint does the ALCF first-run flow, then hands off to the stock
# `hermes dashboard`. We run it as the non-root hermes user (the base image's
# supervised services also drop to this user).
USER hermes
WORKDIR /opt/data
ENTRYPOINT ["/opt/alcf/entrypoint.sh"]
