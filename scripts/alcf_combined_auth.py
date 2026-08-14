#!/usr/bin/env python3
"""ALCF combined Globus authentication — ONE login for all three ALCF services.

The agent-in-a-box needs three separate ALCF capabilities, each historically a
SEPARATE Globus browser login:

    inference   ALCF Inference Service (the LLM gateway; the token IS the api_key)
    iri         IRI Facility API       (job status / output / allocations)
    compute     Globus Compute         (build/run software on ALCF compute nodes)

This module collapses those THREE logins into ONE Globus consent: a single
``globus_sdk.UserApp`` that requests all enabled scopes at once. Globus shows a
single consent screen and returns a SEPARATE access+refresh token per resource
server; ``get_authorizer_for(service)`` hands back the right one for each API.

Why one UserApp / one client_id
-------------------------------
Refresh tokens are CLIENT-BOUND: a refresh token minted for client A cannot be
refreshed by a UserApp configured with client B (Globus returns HTTP 400). So we
cannot mint under one client and let the old per-service helpers (which used
different client_ids) refresh them. Instead this ONE app (fronted by the public
inference client) owns every token and every refresh. All token storage lives in
a SINGLE store, keyed by resource server.

Two ALCF identity policies
--------------------------
The Inference Service and the IRI Facility API each pin a DIFFERENT Globus
``session_required_policies`` value. If IRI is enabled, BOTH policies must be
satisfied in the one login, or IRI rejects an otherwise-valid token with
"Facility Specific authentication failed". Globus Compute needs no policy.

Opt-out flags (consistent with the rest of the agent-box)
---------------------------------------------------------
    ALCF_ENABLE_IRI=0             drop the IRI scope + its identity policy
    ALCF_ENABLE_GLOBUS_COMPUTE=0  drop the Globus Compute scope
Inference is always included (the agent cannot chat without it).

CLI
---
    authenticate                 run the ONE combined interactive login
    check                        report which service tokens are present (no net)
    get_access_token --service inference|iri|compute
                                 print a fresh access token for one service
    status                       human-readable per-service token summary
"""
from __future__ import annotations

import os
import sys
import time

import globus_sdk
# Importing a login-flow manager also ensures globus_sdk.gare is available.
from globus_sdk.login_flows import CommandLineLoginFlowManager  # noqa: F401

# --- one client app fronts the combined consent -----------------------------
APP_NAME = "alcf_combined_app"
# Public ALCF inference auth client (same one inference_auth_token.py uses).
AUTH_CLIENT_ID = "58fdd3bc-e1c3-4ce5-80ea-8d6b87cfb944"

# --- per-service resource servers + scope strings ---------------------------
INFERENCE_RS = "681c10cc-f684-4540-bcd7-0b4df3bc26ef"
INFERENCE_SCOPE = f"https://auth.globus.org/scopes/{INFERENCE_RS}/action_all"
INFERENCE_POLICY = "83732ff2-9c42-4548-b5ce-17e498c84f6a"

IRI_RS = "6be511f6-a071-471f-9bc0-02a0d0836723"
IRI_SCOPE = f"https://auth.globus.org/scopes/{IRI_RS}/filesystem"
IRI_POLICY = "a128e981-c9a5-417a-97ab-8571c9831bff"

# Globus Compute keys its tokens by the resource-server name "funcx_service".
COMPUTE_RS = "funcx_service"
COMPUTE_SCOPE = "https://auth.globus.org/scopes/facd7ccc-c5f4-42aa-916b-a0e270e2c2a9/all"

# Map the user-facing service name -> resource server we fetch the token for.
SERVICE_RS = {
    "inference": INFERENCE_RS,
    "iri": IRI_RS,
    "compute": COMPUTE_RS,
}

# Token store shared by ALL services (single source of truth).
TOKENS_PATH = (
    f"{os.path.expanduser('~')}/.globus/app/{AUTH_CLIENT_ID}/{APP_NAME}/tokens.json"
)


def _enabled(var: str) -> bool:
    """A capability is enabled unless its flag is explicitly '0'."""
    return os.environ.get(var, "1") != "0"


def iri_enabled() -> bool:
    return _enabled("ALCF_ENABLE_IRI")


def compute_enabled() -> bool:
    return _enabled("ALCF_ENABLE_GLOBUS_COMPUTE")


def _scope_requirements() -> dict:
    """Scopes to request in the combined consent, gated by the enable flags.
    Inference is always present."""
    req = {INFERENCE_RS: [INFERENCE_SCOPE]}
    if iri_enabled():
        req[IRI_RS] = [IRI_SCOPE]
    if compute_enabled():
        req[COMPUTE_RS] = [COMPUTE_SCOPE]
    return req


def _session_policies() -> list:
    """Identity policies the consent must satisfy. Inference always; IRI only if
    IRI is enabled. Globus Compute needs none."""
    pols = [INFERENCE_POLICY]
    if iri_enabled():
        pols.append(IRI_POLICY)
    return pols


def _auth_params() -> "globus_sdk.gare.GlobusAuthorizationParameters":
    return globus_sdk.gare.GlobusAuthorizationParameters(
        session_required_policies=_session_policies()
    )


class _DomainBasedErrorHandler:
    """Re-drive the combined login (with the right policies) if the SDK reports a
    token-validation error, mirroring the stock ALCF helpers' behavior."""

    def __call__(self, app, error):
        print(f"Encountered error '{error}', initiating login...", file=sys.stderr)
        app.login(auth_params=_auth_params())


def build_user_app(interactive: bool = False) -> "globus_sdk.UserApp":
    """Construct the combined UserApp. When ``interactive`` is True the manual
    command-line login flow (print URL / paste code) is used, which works over a
    plain TTY / ``docker exec -it`` and needs no loopback browser redirect."""
    cfg_kwargs = dict(
        request_refresh_tokens=True,
        token_validation_error_handler=_DomainBasedErrorHandler(),
    )
    if interactive:
        cfg_kwargs["login_flow_manager"] = CommandLineLoginFlowManager
    return globus_sdk.UserApp(
        APP_NAME,
        client_id=AUTH_CLIENT_ID,
        scope_requirements=_scope_requirements(),
        config=globus_sdk.GlobusAppConfig(**cfg_kwargs),
    )


def get_authorizer_for(service: str, force: bool = False):
    """Return a refresh-token authorizer for one service ('inference'|'iri'|
    'compute'), reusing the shared combined-consent tokens."""
    if service not in SERVICE_RS:
        raise ValueError(f"unknown service {service!r} (expected {list(SERVICE_RS)})")
    app = build_user_app(interactive=force)
    if force:
        app.login(auth_params=_auth_params())
    return app.get_authorizer(SERVICE_RS[service])


def get_access_token(service: str) -> str:
    """Load tokens, refresh if needed, return a valid access token for ``service``."""
    auth = get_authorizer_for(service, force=False)
    auth.ensure_valid_token()
    return auth.access_token


def has_tokens() -> bool:
    return os.path.isfile(TOKENS_PATH) and os.path.getsize(TOKENS_PATH) > 0


def _service_token_present(service: str) -> bool:
    """Best-effort: is a usable (refreshable) token cached for this service?"""
    if not has_tokens():
        return False
    try:
        auth = get_authorizer_for(service, force=False)
        auth.ensure_valid_token()
        return bool(auth.access_token)
    except Exception:
        return False


# --- CLI --------------------------------------------------------------------
def _cli_authenticate() -> int:
    services = ["inference"]
    if iri_enabled():
        services.append("iri")
    if compute_enabled():
        services.append("compute")
    print("[alcf-auth] ONE combined Globus login for: " + ", ".join(services))
    print("[alcf-auth] A URL will be printed — open it, log in with your ALCF/Globus")
    print("[alcf-auth] account, and paste the authorization code back here.\n")
    app = build_user_app(interactive=True)
    app.login(auth_params=_auth_params())
    # Verify each enabled service now resolves a token.
    ok = True
    for svc in services:
        present = _service_token_present(svc)
        print(f"[alcf-auth]   {svc:9s}: {'OK' if present else 'MISSING'}")
        ok = ok and present
    if ok:
        print("\n[alcf-auth] Combined authentication OK "
              f"(tokens cached at {TOKENS_PATH}).")
        return 0
    print("\n[alcf-auth] Some services did not receive a token — see above.",
          file=sys.stderr)
    return 1


def _cli_check() -> int:
    print(f"combined token store : {'present' if has_tokens() else 'MISSING (run: authenticate)'}")
    print(f"store path           : {TOKENS_PATH}")
    print(f"inference            : always enabled")
    print(f"iri                  : {'enabled' if iri_enabled() else 'DISABLED (ALCF_ENABLE_IRI=0)'}")
    print(f"globus compute       : {'enabled' if compute_enabled() else 'DISABLED (ALCF_ENABLE_GLOBUS_COMPUTE=0)'}")
    # Exit 0 only when the combined store exists (fast, no network).
    return 0 if has_tokens() else 1


def _cli_status() -> int:
    for svc in ("inference", "iri", "compute"):
        enabled = (svc == "inference"
                   or (svc == "iri" and iri_enabled())
                   or (svc == "compute" and compute_enabled()))
        if not enabled:
            print(f"{svc:9s}: disabled")
            continue
        print(f"{svc:9s}: {'token present' if _service_token_present(svc) else 'MISSING (run: authenticate)'}")
    return 0


def main() -> int:
    import argparse

    p = argparse.ArgumentParser(description="ALCF combined Globus auth (one login for all services).")
    sub = p.add_subparsers(dest="action", required=True)
    sub.add_parser("authenticate", help="run the ONE combined interactive login")
    sub.add_parser("check", help="report combined-store presence + enable flags (no network)")
    sub.add_parser("status", help="per-service token summary")
    g = sub.add_parser("get_access_token", help="print a fresh access token for one service")
    g.add_argument("--service", required=True, choices=list(SERVICE_RS),
                   help="which service's token to print")
    args = p.parse_args()

    if args.action == "authenticate":
        return _cli_authenticate()
    if args.action == "check":
        return _cli_check()
    if args.action == "status":
        return _cli_status()
    if args.action == "get_access_token":
        if not has_tokens():
            print("ERROR: no combined token store. Authenticate first:\n"
                  "    python3 alcf_combined_auth.py authenticate", file=sys.stderr)
            return 3
        print(get_access_token(args.service))
        return 0
    return 2


if __name__ == "__main__":
    sys.exit(main())
