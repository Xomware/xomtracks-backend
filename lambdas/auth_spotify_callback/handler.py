"""
POST /auth/spotify-callback -- finish the per-user Spotify connect flow (authed).
================================================================================
Self-serve foundation Phase 2. The frontend, having sent the user through the
authorize URL from /auth/spotify-login, posts the {code, state} Spotify returned
on the redirect. This handler:

  1. resolves the authed caller (Cognito email + sub),
  2. verifies the presented `state` matches the one stamped on the caller's row
     and hasn't expired (CSRF defense -- state is bound to the authed caller),
  3. exchanges the code for the owner's refresh token (confidential client --
     CLIENT_SECRET stays server-side),
  4. resolves the connected Spotify account id via /me,
  5. stores refreshToken + spotifyUserId + ownerId (Cognito sub) on the caller's
     OWN xomtracks-users row, clearing the one-time state.

After this, the owner-scoped consumers (rolling playlists, auto-heard,
/playlists/create) act as THIS user on Spotify; until a given owner connects,
they fall back to Dom's service account. The refresh token is never logged and
never returned to the client.
"""

import time
from typing import Any

from pydantic import ValidationError as PydanticValidationError

from lambdas.common import ssm_helpers
from lambdas.common.dynamo_helpers import store_spotify_connection
from lambdas.common.errors import AuthorizationError, ValidationError, handle_errors
from lambdas.common.logger import get_logger
from lambdas.common.models import SpotifyCallbackRequest
from lambdas.common.spotify_oauth import exchange_code, fetch_spotify_user_id
from lambdas.common.user_links import get_user_record
from lambdas.common.utility_helpers import (
    get_caller_email,
    get_caller_sub,
    parse_body,
    success_response,
)

log = get_logger(__file__)

HANDLER = "auth_spotify_callback"


def _verify_state(email: str, presented_state: str) -> None:
    """
    Assert the presented CSRF state matches the one stamped on the caller's row
    at /auth/spotify-login and hasn't expired. Raises AuthorizationError (401) on
    any mismatch/absence/expiry -- never reveals which condition failed.
    """
    record = get_user_record(email)
    stored_state = (record or {}).get("spotifyAuthState")
    stored_exp = (record or {}).get("spotifyAuthStateExp")

    if not stored_state or not secrets_compare(stored_state, presented_state):
        raise AuthorizationError(
            message="Invalid or missing OAuth state.",
            handler=HANDLER,
            function="_verify_state",
        )
    if not stored_exp or int(stored_exp) < int(time.time()):
        raise AuthorizationError(
            message="OAuth state has expired; restart the Spotify connect flow.",
            handler=HANDLER,
            function="_verify_state",
        )


def secrets_compare(a: str, b: str) -> bool:
    """Constant-time string compare (avoid leaking the state via timing)."""
    import hmac

    return hmac.compare_digest(str(a), str(b))


@handle_errors(HANDLER)
def handler(event: dict, context: Any) -> dict:
    # Authed route -- 401 if the Cognito authorizer context is absent.
    email = get_caller_email(event)
    owner_id = get_caller_sub(event)
    if not owner_id:
        # ownerId (Cognito sub) is what every owner-scoped consumer keys by --
        # refuse to store a connection we can't attribute to an owner.
        raise AuthorizationError(
            message="Caller has no Cognito sub; cannot attribute Spotify connection.",
            handler=HANDLER,
            function="handler",
        )

    body = parse_body(event)
    try:
        req = SpotifyCallbackRequest(**body)
    except PydanticValidationError as err:
        raise ValidationError(
            message=f"Invalid spotify-callback payload: {err}",
            handler=HANDLER,
            function="handler",
        ) from err

    _verify_state(email, req.state)

    # Prefer the server-registered redirect URI (Spotify matches it exactly
    # against the authorize call). If the client supplied one it must match.
    redirect_uri = ssm_helpers.SPOTIFY_REDIRECT_URI
    if req.redirectUri and req.redirectUri != redirect_uri:
        raise ValidationError(
            message="redirectUri does not match the registered redirect URI.",
            handler=HANDLER,
            function="handler",
            field="redirectUri",
        )

    token_payload = exchange_code(req.code, redirect_uri)
    refresh_token = token_payload["refresh_token"]
    spotify_user_id = fetch_spotify_user_id(token_payload["access_token"])

    store_spotify_connection(email, owner_id, refresh_token, spotify_user_id)

    log.info("spotify-callback: connected Spotify for %s (spotifyUserId=%s)", email, spotify_user_id)

    return success_response(
        {
            "connected": True,
            "spotifyUserId": spotify_user_id,
            "ownerId": owner_id,
        }
    )
