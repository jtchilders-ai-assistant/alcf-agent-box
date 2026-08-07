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
# Pin the base by an explicit version tag for reproducibility (not :latest,
# which drifts and could break the strip_tool_message_name patch apply or move
# the venv/plugin paths the entrypoint relies on). Bump deliberately after
# re-verifying the patch applies + tool calls still work. v2026.7.30 is the
# release the full feature set was verified against.
ARG HERMES_BASE=nousresearch/hermes-agent:v2026.7.30
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
#
#    Idempotent + upstream-safe: if the base image already carries the flag
#    (i.e. the fix was merged upstream and we bumped HERMES_BASE), we SKIP the
#    patch instead of failing the build on an already-applied hunk. Once the
#    flag is guaranteed upstream, this whole layer can be deleted.
# ---------------------------------------------------------------------------
COPY patches/0001-strip-tool-message-name.patch /tmp/alcf/0001.patch
RUN cd /opt/hermes \
    && if grep -q strip_tool_message_name agent/transports/chat_completions.py; then \
         echo "ALCF patch skipped: strip_tool_message_name already present (upstream?)"; \
       else \
         git apply --unsafe-paths --directory=/opt/hermes /tmp/alcf/0001.patch \
         && grep -q strip_tool_message_name agent/transports/chat_completions.py \
         && echo "ALCF patch applied: strip_tool_message_name present"; \
       fi \
    && rm -rf /tmp/alcf

# ---------------------------------------------------------------------------
# 2. ALCF Globus auth helpers + deps. The base image's venv is uv-managed
#    (no pip binary), so install with `uv pip` into that interpreter. requests
#    is already present in the base; globus-sdk is added here.
# ---------------------------------------------------------------------------
RUN uv pip install --python /opt/hermes/.venv/bin/python --no-cache globus-sdk requests globus-compute-sdk
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
COPY config/SOUL.md /opt/alcf/SOUL.md
COPY scripts/entrypoint.sh /opt/alcf/entrypoint.sh
COPY scripts/iri_hello_world.py /opt/alcf/iri_hello_world.py
COPY scripts/alcf_facility.py /opt/alcf/alcf_facility.py
COPY scripts/alcf_remote_bash.py /opt/alcf/alcf_remote_bash.py
COPY scripts/resolve_context_length.py /opt/alcf/resolve_context_length.py
RUN chmod +x /opt/alcf/entrypoint.sh /opt/alcf/inference_auth_token.py \
             /opt/alcf/alcf_facility_api_globus_token.py /opt/alcf/iri_hello_world.py \
             /opt/alcf/alcf_facility.py /opt/alcf/alcf_remote_bash.py \
             /opt/alcf/resolve_context_length.py \
    && chown -R hermes:hermes /opt/alcf

# Bake the ALCF-agent-box git revision + build date so the container can print
# an unambiguous version banner at startup ("am I running the right image?").
# CI passes ALCF_GIT_SHA=${{ github.sha }}; a bare local build leaves it "dev".
ARG ALCF_GIT_SHA=dev
ARG ALCF_BUILD_DATE=unknown
RUN printf '%s\n%s\n' "${ALCF_GIT_SHA}" "${ALCF_BUILD_DATE}" > /opt/alcf/.alcf_version \
    && chown hermes:hermes /opt/alcf/.alcf_version

# ALCF defaults (override at `docker run` with -e).
# Default model: google/gemma-4-31B-it — a NON-reasoning model that is
# consistently kept "hot" on Sophia (multiple always-on instances), so it avoids
# both (a) the HTTP 503 "online but not ready" cold-start you get from
# less-popular models and (b) gpt-oss-120b's reasoning-token thrash/garble under
# agentic load. All models remain switchable in the dashboard.
ENV ALCF_MODEL=google/gemma-4-31B-it \
    ALCF_CLUSTER=sophia \
    ALCF_MAX_TOKENS=2048 \
    ALCF_DASHBOARD_PORT=8787 \
    ALCF_DASHBOARD_INTERNAL_PORT=9119 \
    ALCF_ENABLE_IRI=1 \
    ALCF_ENABLE_GLOBUS_COMPUTE=1 \
    HERMES_HOME=/opt/data \
    XDG_DATA_HOME=/opt/data/.local/share \
    XDG_CONFIG_HOME=/opt/data/.config

EXPOSE 8787

# Health: the public port is HTTPS (Caddy, self-signed) proxying to the
# dashboard. `curl -k` tolerates the self-signed cert; we accept ANY HTTP
# response (including the 401 auth challenge) as "server up" — we only care
# that Caddy + the dashboard behind it are answering. start-period covers
# first-run Globus auth + dashboard warmup.
HEALTHCHECK --interval=30s --timeout=5s --start-period=120s --retries=3 \
  CMD curl -ks -o /dev/null -w '%{http_code}' \
        "https://127.0.0.1:${ALCF_DASHBOARD_PORT}/" 2>/dev/null \
      | grep -qE '^[1-5][0-9]{2}$' || exit 1

# Our entrypoint does the ALCF first-run flow, then hands off to the stock
# `hermes dashboard`. We run it as the non-root hermes user (the base image's
# supervised services also drop to this user).
USER hermes
WORKDIR /opt/data
ENTRYPOINT ["/opt/alcf/entrypoint.sh"]
