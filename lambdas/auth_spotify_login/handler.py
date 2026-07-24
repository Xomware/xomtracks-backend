"""
POST /auth/spotify-login -- start the per-user Spotify connect flow (authed).
============================================================================
Self-serve foundation Phase 2. There is NO OAuth flow in the app today -- Dom's
refresh token was seeded by hand. This mints the Spotify Authorization-Code
consent URL the frontend redirects the caller to.

Cognito-authed: the caller's identity (email + sub) comes from the native
COGNITO_USER_POOLS authorizer. We generate a random `state` (CSRF token), stamp
it on the caller's OWN xomtracks-users row (with a short expiry), and return the
authorize URL carrying that state + the server-configured redirect URI + the
scopes the consumers need. /auth/spotify-callback later verifies the returned
state against the caller's row before exchanging the code.

The state is bound to the authed caller, so a forged callback for a different
user can't succeed even if an attacker guesses a code.
"""

import secrets
import time
from typing import Any

from lambdas.common import ssm_helpers
from lambdas.common.constants import SPOTIFY_AUTH_STATE_TTL_SECONDS, SPOTIFY_OAUTH_SCOPES
from lambdas.common.dynamo_helpers import store_spotify_auth_state
from lambdas.common.errors import handle_errors
from lambdas.common.logger import get_logger
from lambdas.common.spotify_oauth import build_authorize_url
from lambdas.common.utility_helpers import get_caller_email, success_response

log = get_logger(__file__)

HANDLER = "auth_spotify_login"


@handle_errors(HANDLER)
def handler(event: dict, context: Any) -> dict:
    # Authed route -- 401 if the Cognito authorizer context is absent.
    email = get_caller_email(event)

    redirect_uri = ssm_helpers.SPOTIFY_REDIRECT_URI
    state = secrets.token_urlsafe(32)
    expires_at = int(time.time()) + SPOTIFY_AUTH_STATE_TTL_SECONDS

    store_spotify_auth_state(email, state, expires_at)

    authorize_url = build_authorize_url(state, redirect_uri)

    log.info("spotify-login: minted authorize URL for %s (state stamped)", email)

    return success_response(
        {
            "authorizeUrl": authorize_url,
            "state": state,
            "redirectUri": redirect_uri,
            "scopes": list(SPOTIFY_OAUTH_SCOPES),
            "expiresAt": expires_at,
        }
    )
