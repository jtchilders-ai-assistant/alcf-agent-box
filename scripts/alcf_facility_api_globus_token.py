#!/usr/bin/env python3
"""ALCF IRI Facility API token helper — COMPATIBILITY SHIM.

Historically this file ran its OWN Globus login for the IRI Facility API. The
agent-in-a-box now uses a SINGLE combined Globus consent for all ALCF services
(see ``alcf_combined_auth.py``), so this module delegates to that shared token
authority. It keeps the same public API + CLI so existing callers (notably
``alcf_facility.py``, which shells out to ``get_access_token``) work unchanged.

    get_access_token()                -> str  (IRI access token)
    get_auth_object(force=False)      -> refresh-token authorizer for IRI
    get_time_until_token_expiration() -> float

CLI: authenticate | get_access_token | get_time_until_token_expiration
"""
from __future__ import annotations

import os
import sys
import time

# Make the combined-auth module importable from /opt/alcf or a repo checkout.
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import alcf_combined_auth as _combined  # noqa: E402

# Back-compat constants.
AUTH_CLIENT_ID = _combined.AUTH_CLIENT_ID
SCOPE_CLIENT_ID = _combined.IRI_RS
SCOPE_STRING = _combined.IRI_SCOPE
TOKENS_PATH = _combined.TOKENS_PATH

_SERVICE = "iri"


class FacilityAPIAuthError(Exception):
    pass


def _require_iri_enabled():
    if not _combined.iri_enabled():
        raise FacilityAPIAuthError(
            "IRI Facility API is DISABLED (ALCF_ENABLE_IRI=0). Re-enable it and "
            "re-run the combined login: python3 alcf_combined_auth.py authenticate")


def get_auth_object(force=False):
    """Refresh-token authorizer for the IRI service, from the combined consent."""
    _require_iri_enabled()
    return _combined.get_authorizer_for(_SERVICE, force=force)


def get_access_token() -> str:
    _require_iri_enabled()
    return _combined.get_access_token(_SERVICE)


def get_time_until_token_expiration(units: str = "seconds"):
    auth = get_auth_object(force=False)
    delta_t = auth.expires_at - time.time()
    if units == "seconds":
        pass
    elif units == "minutes":
        delta_t = delta_t / 60
    elif units == "hours":
        delta_t = delta_t / 3600
    else:
        return "Error: units must be 'seconds', 'minutes', or 'hours'."
    return round(delta_t, 2)


if __name__ == "__main__":
    import argparse

    AUTHENTICATE_ACTION = "authenticate"
    GET_ACCESS_TOKEN_ACTION = "get_access_token"
    GET_TOKEN_EXPIRATION_ACTION = "get_time_until_token_expiration"

    parser = argparse.ArgumentParser()
    parser.add_argument("action", choices=[AUTHENTICATE_ACTION, GET_ACCESS_TOKEN_ACTION,
                                           GET_TOKEN_EXPIRATION_ACTION])
    parser.add_argument("--units", choices=["seconds", "minutes", "hours"], default="seconds",
                        help="Units for the time until token expiration")
    args = parser.parse_args()

    if args.action == AUTHENTICATE_ACTION:
        # Delegate to the ONE combined login (covers IRI + the other enabled services).
        sys.exit(_combined._cli_authenticate())

    elif args.action == GET_ACCESS_TOKEN_ACTION:
        if not _combined.has_tokens():
            raise FacilityAPIAuthError('Access token does not exist. '
                'Please authenticate by running "python3 alcf_combined_auth.py authenticate".')
        print(get_access_token())

    elif args.action == GET_TOKEN_EXPIRATION_ACTION:
        if not _combined.has_tokens():
            raise FacilityAPIAuthError('Access token does not exist. '
                'Please authenticate by running "python3 alcf_combined_auth.py authenticate".')
        print(get_time_until_token_expiration(args.units))
