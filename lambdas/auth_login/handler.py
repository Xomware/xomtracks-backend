"""
POST /auth/login - Mint per-user Xomtracks JWT from a Spotify access token.

Ported from xomify-backend's auth_login/handler.py. Public route (no
authorizer). Verifies the caller against Spotify's `/me` endpoint, then
issues a short-lived HS256 JWT containing the caller's email and Spotify
user id. Downstream authed routes consume that JWT via the custom
authorizer. This is xomtracks' OWN token/user identity -- self-contained
per PLAN.md Option 3, separate from both xomify's user row and the shared
Cognito pool (which gates the frontend route, not this API).

The JWT itself and the Spotify access token are never logged.
"""

from datetime import datetime, timezone
from typing import Any

import jwt
import requests

from lambdas.common.errors import (
    AuthorizationError,
    SpotifyAPIError,
    ValidationError,
    handle_errors,
)
from lambdas.common.logger import get_logger
from lambdas.common import ssm_helpers
from lambdas.common.utility_helpers import parse_body, success_response

log = get_logger(__file__)

HANDLER = "auth_login"

SPOTIFY_ME_URL = "https://api.spotify.com/v1/me"
SPOTIFY_TIMEOUT_SECONDS = 5
JWT_ALGORITHM = "HS256"
JWT_TTL_SECONDS = 7 * 24 * 3600  # 7 days


def _get_source_ip(event: dict) -> str:
    """Extract the requester source IP from an API Gateway event, if present."""
    request_context = event.get("requestContext") or {}
    identity = request_context.get("identity") or {}
    return identity.get("sourceIp") or "unknown"


def _fetch_spotify_me(spotify_access_token: str) -> dict:
    """
    Call Spotify's `/me` endpoint with the caller's access token.

    Raises:
        AuthorizationError: Spotify rejected the token (any non-200 status).
        SpotifyAPIError: Network/transport problem reaching Spotify.
    """
    headers = {"Authorization": f"Bearer {spotify_access_token}"}
    try:
        response = requests.get(
            SPOTIFY_ME_URL,
            headers=headers,
            timeout=SPOTIFY_TIMEOUT_SECONDS,
        )
    except requests.RequestException as err:
        raise SpotifyAPIError(
            message=f"Failed to reach Spotify /me: {err}",
            handler=HANDLER,
            function="_fetch_spotify_me",
            endpoint="/me",
        ) from err

    if response.status_code != 200:
        raise AuthorizationError(
            message="Spotify rejected the provided access token.",
            handler=HANDLER,
            function="_fetch_spotify_me",
        )

    try:
        return response.json()
    except ValueError as err:
        raise SpotifyAPIError(
            message=f"Spotify /me returned non-JSON body: {err}",
            handler=HANDLER,
            function="_fetch_spotify_me",
            endpoint="/me",
        ) from err


def _mint_jwt(email: str, user_id: str) -> tuple[str, int, int]:
    """
    Mint an HS256 JWT for the caller.

    Returns:
        (token, iat, exp) where iat and exp are Unix epoch seconds.
    """
    iat = int(datetime.now(timezone.utc).timestamp())
    exp = iat + JWT_TTL_SECONDS
    payload: dict[str, Any] = {
        "email": email,
        "userId": user_id,
        "iat": iat,
        "exp": exp,
    }
    token = jwt.encode(payload, ssm_helpers.API_SECRET_KEY, algorithm=JWT_ALGORITHM)
    # PyJWT >= 2.0 returns a str; guard against the legacy bytes return.
    if isinstance(token, bytes):
        token = token.decode("utf-8")
    return token, iat, exp


@handle_errors(HANDLER)
def handler(event: dict, context: Any) -> dict:
    body = parse_body(event)
    spotify_access_token = body.get("spotifyAccessToken")

    if not isinstance(spotify_access_token, str) or not spotify_access_token.strip():
        raise ValidationError(
            message="Missing required field: spotifyAccessToken",
            handler=HANDLER,
            function="handler",
            field="spotifyAccessToken",
        )

    me = _fetch_spotify_me(spotify_access_token)

    email = me.get("email")
    user_id = me.get("id")
    if not email or not user_id:
        raise SpotifyAPIError(
            message="Spotify /me response missing required fields (email or id).",
            handler=HANDLER,
            function="handler",
            endpoint="/me",
        )

    token, iat, exp = _mint_jwt(email=email, user_id=user_id)

    source_ip = _get_source_ip(event)
    log.info(
        "auth_login mint email=%s ip=%s iat=%s exp=%s",
        email,
        source_ip,
        iat,
        exp,
    )

    expires_at = datetime.fromtimestamp(exp, tz=timezone.utc).isoformat()

    return success_response({"token": token, "expiresAt": expires_at})
