#!/usr/bin/env python3
"""ALCF Inference Service token helper — COMPATIBILITY SHIM.

Historically this file ran its OWN Globus login for the inference service. The
agent-in-a-box now uses a SINGLE combined Globus consent for all ALCF services
(see ``alcf_combined_auth.py``), so this module delegates to that shared token
authority. It keeps the exact same public API + CLI so every existing caller
(entrypoint render_config, populate_models.py, resolve_context_length.py, and
anyone running ``inference_auth_token.py get_access_token``) works unchanged.

    get_access_token()                -> str  (inference access token)
    get_auth_object(force=False)      -> refresh-token authorizer for inference
    get_time_until_token_expiration() -> float
    revoke_access_token()             -> logout / revoke the combined app

CLI: authenticate | get_access_token | get_time_until_token_expiration |
     revoke_access_token
"""
from __future__ import annotations

import os
import sys
import time

# Make the combined-auth module importable whether we run from /opt/alcf (image)
# or a repo checkout (scripts/ dir).
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import alcf_combined_auth as _combined  # noqa: E402

# Back-compat constants some tooling may import.
AUTH_CLIENT_ID = _combined.AUTH_CLIENT_ID
GATEWAY_CLIENT_ID = _combined.INFERENCE_RS
GATEWAY_SCOPE = _combined.INFERENCE_SCOPE
# The combined app owns the token store now; expose ITS path so existence checks
# (e.g. entrypoint's authed_inference / CLI guards) point at the right file.
TOKENS_PATH = _combined.TOKENS_PATH

_SERVICE = "inference"


def get_auth_object(force=False):
    """Refresh-token authorizer for the inference service, from the combined
    consent. ``force=True`` triggers the ONE combined interactive login."""
    return _combined.get_authorizer_for(_SERVICE, force=force)


def get_access_token() -> str:
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


def revoke_access_token():
    """Log out the combined app and revoke its tokens. NOTE: because all three
    ALCF services now share one consent, this revokes IRI + Globus Compute too."""
    app = _combined.build_user_app(interactive=False)
    app.logout()
    print("Done. The Inference Gateway API can take up to ~10 minutes to "
          "incorporate the revocation. (This also revoked the shared IRI + "
          "Globus Compute tokens, since all three share one login.)")


if __name__ == "__main__":
    import argparse

    class InferenceAuthError(Exception):
        pass

    AUTHENTICATE_ACTION = "authenticate"
    GET_ACCESS_TOKEN_ACTION = "get_access_token"
    REVOKE_ACCESS_TOKEN_ACTION = "revoke_access_token"
    GET_TOKEN_EXPIRATION_ACTION = "get_time_until_token_expiration"

    parser = argparse.ArgumentParser()
    parser.add_argument("action", choices=[AUTHENTICATE_ACTION, GET_ACCESS_TOKEN_ACTION,
                                           REVOKE_ACCESS_TOKEN_ACTION, GET_TOKEN_EXPIRATION_ACTION])
    parser.add_argument("--units", choices=["seconds", "minutes", "hours"], default="seconds",
                        help="Units for the time until token expiration")
    parser.add_argument("-f", "--force", action="store_true", help="authenticate from scratch")
    args = parser.parse_args()

    if args.action == AUTHENTICATE_ACTION:
        # Delegate to the ONE combined login (covers inference + enabled services).
        sys.exit(_combined._cli_authenticate())

    elif args.action == GET_ACCESS_TOKEN_ACTION:
        if not _combined.has_tokens():
            raise InferenceAuthError('Access token does not exist. '
                'Please authenticate by running "python3 inference_auth_token.py authenticate".')
        if args.force:
            raise InferenceAuthError(f"The --force flag cannot be used with the {GET_ACCESS_TOKEN_ACTION} action.")
        print(get_access_token())

    elif args.action == GET_TOKEN_EXPIRATION_ACTION:
        if not _combined.has_tokens():
            raise InferenceAuthError('Access token does not exist. '
                'Please authenticate by running "python3 inference_auth_token.py authenticate".')
        print(get_time_until_token_expiration(args.units))

    elif args.action == REVOKE_ACCESS_TOKEN_ACTION:
        if not _combined.has_tokens():
            raise InferenceAuthError('Access token not found.')
        revoke_access_token()
