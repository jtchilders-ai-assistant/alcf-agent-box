#!/usr/bin/env python3
"""Red Shirt Polaris: fail-closed secret validation, live-model selection, and
Hermes configuration rendering for the compute-resident deployment.

Design: docs/superpowers/specs/2026-09-16-red-shirt-polaris-design.md
Plan:   docs/superpowers/plans/2026-09-16-red-shirt-polaris.md (Task 3)

Subcommands
-----------
    validate-secrets --headscale-key FILE --inbound-a2a FILE --outbound-a2a FILE
    render --home DIR --template FILE --token-helper FILE
           --inbound-a2a FILE --outbound-a2a FILE
           --preferred-model MODEL_ID [--model-preference MODEL_ID ...]
           [--catalog-fixture FILE] [--jobs-fixture FILE]
           [--cluster {sophia,metis,minerva}] [--a2a-port PORT]
           [--a2a-public-url URL] [--wesley-url URL]

Fail-closed rules
-----------------
- Every secret path must be a regular file, mode exactly 0600, readable, and
  nonempty. A2A bearer tokens must additionally be >= 16 characters. Secret
  VALUES are never printed, logged, or included in an exception message.
- The inference access token always comes from the supplied ``--token-helper``
  executable (never read from a literal or embedded in output).
- Only chat models with a real serving context >= 64000 tokens are eligible
  (reuses populate_models.py's per-cluster selectors and MIN_CONTEXT floor).
- The requested/preferred model is used only if it is currently LIVE;
  otherwise the first LIVE model from an explicit ordered preference list is
  chosen. If no eligible model is LIVE, render exits 78 (EX_CONFIG) with an
  actionable message rather than emitting a config that can't answer.
- Rendered config.yaml and $HERMES_HOME/.env are written atomically (temp
  file + os.replace). .env is written mode 0600. config.yaml never contains a
  literal token value — token fields are left as ``${VAR}`` references that
  Hermes's own config loader resolves from the environment .env populates.
  Manager decision reconciling the "no unresolved ${...} placeholders"
  acceptance criterion with the pinned Hermes v2026.9.14 A2A contract (no
  outbound token_env/token_file resolver exists in
  plugins/platforms/a2a/tools.py — ``_auth_header`` reads only the already
  env-expanded ``auth.token``): the ONLY two ``${...}`` references allowed to
  remain unresolved in generated config.yaml are ``${ALCF_ACCESS_TOKEN}`` and
  ``${A2A_OUTBOUND_WESLEY_TOKEN}``. Every other templated field must resolve
  to a literal value.

Catalog/jobs fixtures
----------------------
``--catalog-fixture`` / ``--jobs-fixture`` point at JSON files shaped exactly
like the ALCF ``.../<cluster>/models`` and ``.../<cluster>/jobs`` responses
(see scripts/populate_models.py), letting tests run fully offline and
deterministically. When omitted, render queries the live ALCF endpoints for
``--cluster`` using the token obtained from ``--token-helper``.
"""
from __future__ import annotations

import argparse
import json
import os
import stat
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Optional

import yaml

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import populate_models as pm  # noqa: E402  (reuse chat-model selection + context floor)

EXIT_OK = 0
EXIT_SECRET_INVALID = 1
EXIT_NO_LIVE_MODEL = 78  # sysexits.h EX_CONFIG

MIN_A2A_TOKEN_LENGTH = 16

CLUSTER_BASE_URLS = {
    "sophia": pm.SOPHIA_BASE,
    "metis": pm.METIS_BASE,
    "minerva": pm.MINERVA_BASE,
}

CLUSTER_SELECTORS = {
    "sophia": pm._select_sophia,
    "metis": pm._select_metis,
    "minerva": pm._select_minerva,
}

DEFAULT_A2A_PUBLIC_URL = "https://red-shirt-polaris.PLACEHOLDER-TAILNET.ts.net:9900/"
DEFAULT_WESLEY_URL = "http://PLACEHOLDER-WESLEY-TAILNET-ADDR:9900/"


class SecretValidationError(Exception):
    """A credential file failed validation. Never carries the secret value."""


class NoLiveModelError(Exception):
    """No configured chat model is currently LIVE and usable."""


# ---------------------------------------------------------------------------
# Secret validation (never discloses secret contents)
# ---------------------------------------------------------------------------

def _check_secret_file(path_str: str, *, label: str,
                       min_length: Optional[int] = None) -> Path:
    path = Path(path_str)
    if path.is_symlink():
        raise SecretValidationError(f"{label}: must not be a symlink: {path}")
    if not path.exists():
        raise SecretValidationError(f"{label}: file does not exist: {path}")
    if not path.is_file():
        raise SecretValidationError(f"{label}: not a regular file: {path}")
    st = path.stat()
    mode = stat.S_IMODE(st.st_mode)
    if mode != 0o600:
        raise SecretValidationError(
            f"{label}: must be mode 0600, found {oct(mode)}: {path}"
        )
    if not os.access(path, os.R_OK):
        raise SecretValidationError(f"{label}: not readable: {path}")
    if st.st_size == 0:
        raise SecretValidationError(f"{label}: file is empty: {path}")
    if min_length is not None:
        content = path.read_text(encoding="utf-8").strip()
        if len(content) < min_length:
            raise SecretValidationError(
                f"{label}: value is shorter than the required "
                f"{min_length} characters"
            )
    return path


def validate_secrets(*, headscale_key: Optional[str] = None,
                     inbound_a2a: Optional[str] = None,
                     outbound_a2a: Optional[str] = None) -> None:
    """Validate every supplied secret path; raise on the first failure.

    Any path left as None is skipped (callers pass only what they have).
    Never logs or returns secret contents.
    """
    if headscale_key is not None:
        _check_secret_file(headscale_key, label="headscale join key")
    if inbound_a2a is not None:
        _check_secret_file(
            inbound_a2a, label="inbound A2A token (wesley -> red-shirt-polaris)",
            min_length=MIN_A2A_TOKEN_LENGTH,
        )
    if outbound_a2a is not None:
        _check_secret_file(
            outbound_a2a, label="outbound A2A token (red-shirt-polaris -> wesley)",
            min_length=MIN_A2A_TOKEN_LENGTH,
        )


def _read_secret(path_str: str) -> str:
    return Path(path_str).read_text(encoding="utf-8").strip()


# ---------------------------------------------------------------------------
# Inference token + live-model selection
# ---------------------------------------------------------------------------

def get_inference_token(token_helper: str) -> str:
    """Run the supplied token-helper executable; return the printed token.

    Mirrors inference_auth_token.py's own CLI (``get_access_token``). Never
    logs the returned value.
    """
    out = subprocess.check_output(
        [sys.executable, str(token_helper), "get_access_token"],
        text=True, timeout=60,
    )
    lines = [line.strip() for line in out.splitlines() if line.strip()]
    if not lines:
        raise RuntimeError("token helper produced no output")
    return lines[-1]


def _live_ids_from_jobs(jobs_doc: dict) -> set:
    """{model ids currently RUNNING} from a /jobs-shaped document.

    Mirrors populate_models._fetch_job_states' "running" classification
    without requiring a network call, so fixtures can drive it directly.
    """
    live: set = set()
    for job in (jobs_doc or {}).get("running", []) or []:
        if not isinstance(job, dict):
            continue
        if str(job.get("Model Status", "")).lower() not in ("", "running"):
            continue
        live.update(pm._split_models_field(job))
    return live


def select_live_model(catalog: list, jobs: dict, *, preferred: str,
                      preferences: list, cluster: str = "sophia") -> tuple:
    """Return ``(model_id, context_length, is_reasoning)`` for a LIVE model.

    Only chat models with a real serving context >= 64000 are eligible (the
    cluster's populate_models selector already enforces MIN_CONTEXT).
    ``preferred`` is chosen only when it is LIVE; otherwise the first LIVE
    entry in ``preferences`` (in order) is chosen. Raises NoLiveModelError
    if nothing eligible is LIVE.
    """
    selector = CLUSTER_SELECTORS.get(cluster)
    if selector is None:
        raise ValueError(f"unknown cluster {cluster!r}")
    chat_models = selector(catalog)
    live = _live_ids_from_jobs(jobs) & set(chat_models)

    chosen = None
    if preferred and preferred in live:
        chosen = preferred
    else:
        for candidate in preferences or []:
            if candidate in live:
                chosen = candidate
                break

    if chosen is None:
        raise NoLiveModelError(
            "no live >=64000-token chat model available on cluster "
            f"{cluster!r} (requested={preferred!r}, "
            f"preferences={list(preferences or [])!r}, "
            f"eligible_live={sorted(live)!r})"
        )

    context_length, is_reasoning = chat_models[chosen]
    return chosen, context_length, is_reasoning


def _provider_name(cluster: str, is_reasoning: bool) -> str:
    base = f"alcf-{cluster}"
    return f"{base}-reasoning" if is_reasoning else base


# ---------------------------------------------------------------------------
# Atomic writes
# ---------------------------------------------------------------------------

def _atomic_write_text(path: Path, content: str, mode: Optional[int] = None) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=f".{path.name}.",
                               suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(content)
            f.flush()
            os.fsync(f.fileno())
        if mode is not None:
            os.chmod(tmp, mode)
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


# ---------------------------------------------------------------------------
# Config rendering
# ---------------------------------------------------------------------------

def _render_config_dict(template_doc: dict, *, model_id: str, base_url: str,
                        context_length: int, provider_name: str,
                        max_tokens: int, a2a_port: int, wesley_url: str) -> dict:
    """Return a fully-rendered config dict (deep copy of the template, with
    every generated field set to a literal value except the two credential
    fields, which are LEFT as ``${VAR}`` references for Hermes to expand)."""
    cfg = json.loads(json.dumps(template_doc or {}))

    model = cfg.setdefault("model", {})
    model["provider"] = f"custom:{provider_name}"
    model["base_url"] = base_url
    model["api_key"] = "${ALCF_ACCESS_TOKEN}"
    model["model"] = model_id
    model["default"] = model_id
    model["strip_tool_message_name"] = True
    model["context_length"] = context_length

    gateway = cfg.setdefault("gateway", {})
    platforms = gateway.setdefault("platforms", {})
    a2a_platform = platforms.setdefault("a2a", {})
    a2a_platform["enabled"] = True
    extra = a2a_platform.setdefault("extra", {})
    extra["port"] = a2a_port

    a2a_section = cfg.setdefault("a2a", {})
    a2a_section["trusted_peers"] = ["wesley"]

    a2a_agents = cfg.setdefault("a2a_agents", {})
    wesley = a2a_agents.setdefault("wesley", {})
    wesley["url"] = wesley_url
    wesley["auth"] = {"type": "bearer", "token": "${A2A_OUTBOUND_WESLEY_TOKEN}"}
    wesley.setdefault("timeout", 120)

    cfg["custom_providers"] = [{
        "name": provider_name,
        "base_url": base_url,
        "api_key": "${ALCF_ACCESS_TOKEN}",
        "discover_models": False,
        "max_tokens": max_tokens,
        "models": {
            model_id: {"context_length": context_length},
        },
    }]

    return cfg


def render(args: argparse.Namespace) -> int:
    home = Path(args.home)
    home.mkdir(parents=True, exist_ok=True)

    # Fail closed on bad credentials before doing any network/token work.
    validate_secrets(inbound_a2a=args.inbound_a2a, outbound_a2a=args.outbound_a2a)

    inbound_token = _read_secret(args.inbound_a2a)
    outbound_token = _read_secret(args.outbound_a2a)

    inference_token = get_inference_token(args.token_helper)  # never printed

    if args.catalog_fixture:
        catalog = json.loads(Path(args.catalog_fixture).read_text(encoding="utf-8"))
    else:
        catalog = pm._fetch_models(args.cluster, inference_token)

    if args.jobs_fixture:
        jobs = json.loads(Path(args.jobs_fixture).read_text(encoding="utf-8"))
    else:
        live_ids, _queued = pm._fetch_job_states(args.cluster, inference_token)
        jobs = {"running": [{"Models": ",".join(sorted(live_ids)),
                             "Model Status": "running"}]} if live_ids else {"running": []}

    model_id, context_length, is_reasoning = select_live_model(
        catalog, jobs, preferred=args.preferred_model,
        preferences=args.model_preference, cluster=args.cluster,
    )

    base_url = CLUSTER_BASE_URLS[args.cluster]
    provider_name = _provider_name(args.cluster, is_reasoning)
    max_tokens = pm.REASONING_MAX_TOKENS if is_reasoning else pm.BASELINE_MAX_TOKENS

    template_doc = yaml.safe_load(Path(args.template).read_text(encoding="utf-8")) or {}
    cfg = _render_config_dict(
        template_doc, model_id=model_id, base_url=base_url,
        context_length=context_length, provider_name=provider_name,
        max_tokens=max_tokens, a2a_port=args.a2a_port, wesley_url=args.wesley_url,
    )

    config_path = home / "config.yaml"
    rendered_yaml = yaml.safe_dump(cfg, sort_keys=False, default_flow_style=False)
    _atomic_write_text(config_path, rendered_yaml)

    env_lines = [
        f"ALCF_ACCESS_TOKEN={inference_token}",
        f"A2A_PEER_TOKENS=wesley:{inbound_token}",
        f"A2A_OUTBOUND_WESLEY_TOKEN={outbound_token}",
        f"A2A_PUBLIC_URL={args.a2a_public_url}",
    ]
    env_path = home / ".env"
    _atomic_write_text(env_path, "\n".join(env_lines) + "\n", mode=0o600)

    # Non-secret status output only: model/provider names and paths.
    print(f"model: {model_id}")
    print(f"provider: custom:{provider_name}")
    print(f"context_length: {context_length}")
    print(f"config: {config_path}")
    print(f"env: {env_path}")
    return EXIT_OK


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="red_shirt_config.py",
        description="Red Shirt Polaris: fail-closed secret validation, "
                    "live-model selection, and Hermes config rendering.",
    )
    sub = p.add_subparsers(dest="command", required=True)

    vs = sub.add_parser("validate-secrets",
                        help="validate credential files without disclosing values")
    vs.add_argument("--headscale-key", required=True)
    vs.add_argument("--inbound-a2a", required=True)
    vs.add_argument("--outbound-a2a", required=True)

    rn = sub.add_parser("render", help="render Hermes config + secret .env")
    rn.add_argument("--home", required=True, help="$HERMES_HOME target directory")
    rn.add_argument("--template", required=True,
                    help="config.template.yaml to render from")
    rn.add_argument("--token-helper", required=True,
                    help="executable that prints the ALCF inference access "
                         "token for the 'get_access_token' action")
    rn.add_argument("--catalog-fixture", default=None,
                    help="JSON file shaped like .../<cluster>/models "
                         "(offline/deterministic testing)")
    rn.add_argument("--jobs-fixture", default=None,
                    help="JSON file shaped like .../<cluster>/jobs "
                         "(offline/deterministic testing)")
    rn.add_argument("--inbound-a2a", required=True,
                    help="file: bearer token wesley presents to us")
    rn.add_argument("--outbound-a2a", required=True,
                    help="file: bearer token we present to wesley")
    rn.add_argument("--preferred-model", required=True)
    rn.add_argument("--model-preference", action="append", default=[],
                    help="ordered fallback model id; may be repeated")
    rn.add_argument("--cluster", default="sophia",
                    choices=sorted(CLUSTER_BASE_URLS))
    rn.add_argument("--a2a-port", type=int, default=9900)
    rn.add_argument("--a2a-public-url", default=DEFAULT_A2A_PUBLIC_URL)
    rn.add_argument("--wesley-url", default=DEFAULT_WESLEY_URL)

    return p


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)
    try:
        if args.command == "validate-secrets":
            validate_secrets(headscale_key=args.headscale_key,
                             inbound_a2a=args.inbound_a2a,
                             outbound_a2a=args.outbound_a2a)
            print("secrets: OK")
            return EXIT_OK
        if args.command == "render":
            return render(args)
    except SecretValidationError as e:
        print(f"ERROR: {e}", file=sys.stderr)
        return EXIT_SECRET_INVALID
    except NoLiveModelError as e:
        print(f"ERROR: {e}", file=sys.stderr)
        return EXIT_NO_LIVE_MODEL
    return 2  # unreachable: argparse's required=True on subparsers covers this


if __name__ == "__main__":
    raise SystemExit(main())
