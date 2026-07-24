"""
XOMTRACKS Spotify OAuth (Authorization-Code flow) -- Phase 2
===========================================================
The per-user connect flow that replaces Dom's single hand-seeded refresh
token. Kept SEPARATE from the vendored `spotify.py` (a hand-synced copy of
xomify's client -- see .claude/CLAUDE.md) so the confidential-client OAuth
code exchange is a first-class, testable xomtracks module rather than drift
against the vendored file.

Two pure-ish edges, both reading the reused `/xomtracks/spotify/*` app
credentials lazily from SSM (module-object access, never `from ... import
NAME`, so the SSM fetch stays deferred/testable -- same rationale as
spotify.py's docstring):

- build_authorize_url(state, redirect_uri) -> the Spotify consent URL the
  frontend sends the user to (client_id + scopes + state + redirect_uri).
- exchange_code(code, redirect_uri) -> POSTs grant_type=authorization_code to
  Spotify's token endpoint with the app client_id/secret, returning the
  {access_token, refresh_token} payload. Then fetch_spotify_user_id(access)
  resolves the connected account's Spotify user id via GET /me.

CLIENT_SECRET never leaves the server; tokens are never logged.
"""

import urllib.parse

import requests

from lambdas.common import ssm_helpers
from lambdas.common.constants import SPOTIFY_OAUTH_SCOPES
from lambdas.common.errors import AuthorizationError, SpotifyAPIError
from lambdas.common.logger import get_logger

log = get_logger(__file__)

_AUTHORIZE_URL = "https://accounts.spotify.com/authorize"
_TOKEN_URL = "https://accounts.spotify.com/api/token"
_ME_URL = "https://api.spotify.com/v1/me"
_TIMEOUT_SECONDS = 5


def build_authorize_url(state: str, redirect_uri: str, scopes=SPOTIFY_OAUTH_SCOPES) -> str:
    """
    Build the Spotify Authorization-Code consent URL the frontend redirects the
    user to. `state` is the CSRF token bound to the caller's row; `redirect_uri`
    must match a URI registered on the Spotify app dashboard exactly.
    """
    params = {
        "client_id": ssm_helpers.SPOTIFY_CLIENT_ID,
        "response_type": "code",
        "redirect_uri": redirect_uri,
        "scope": " ".join(scopes),
        "state": state,
        # Force the consent screen so a user connecting a DIFFERENT Spotify
        # account than one they previously approved isn't silently re-linked.
        "show_dialog": "true",
    }
    return f"{_AUTHORIZE_URL}?{urllib.parse.urlencode(params)}"


def exchange_code(code: str, redirect_uri: str) -> dict:
    """
    Exchange an authorization code for tokens (confidential client -- sends the
    app client_id + client_secret). Returns Spotify's token payload, which
    carries `refresh_token` (long-lived, stored per owner) and `access_token`.

    Raises:
        AuthorizationError (401): Spotify rejected the code/redirect (400 from
            the token endpoint -- a bad/expired code or mismatched redirect_uri).
        SpotifyAPIError (502): transport failure or a non-JSON/other-status body.
    """
    data = {
        "grant_type": "authorization_code",
        "code": code,
        "redirect_uri": redirect_uri,
        "client_id": ssm_helpers.SPOTIFY_CLIENT_ID,
        "client_secret": ssm_helpers.SPOTIFY_CLIENT_SECRET,
    }
    try:
        response = requests.post(_TOKEN_URL, data=data, timeout=_TIMEOUT_SECONDS)
    except requests.RequestException as err:
        raise SpotifyAPIError(
            message=f"Failed to reach Spotify token endpoint: {err}",
            handler="spotify_oauth",
            function="exchange_code",
            endpoint="/api/token",
        ) from err

    if response.status_code == 400:
        # Bad/expired code or redirect mismatch -- a caller/CSRF problem, not a
        # server fault. Surface as 401 (never log the code or Spotify body).
        raise AuthorizationError(
            message="Spotify rejected the authorization code (expired or redirect mismatch).",
            handler="spotify_oauth",
            function="exchange_code",
        )
    if response.status_code != 200:
        raise SpotifyAPIError(
            message=f"Spotify token exchange failed (status {response.status_code}).",
            handler="spotify_oauth",
            function="exchange_code",
            endpoint="/api/token",
        )

    try:
        payload = response.json()
    except ValueError as err:
        raise SpotifyAPIError(
            message=f"Spotify token endpoint returned non-JSON: {err}",
            handler="spotify_oauth",
            function="exchange_code",
            endpoint="/api/token",
        ) from err

    if not payload.get("refresh_token"):
        # No refresh_token means we can't act on the user's behalf later --
        # treat as a hard failure rather than storing a useless connection.
        raise SpotifyAPIError(
            message="Spotify token exchange returned no refresh_token.",
            handler="spotify_oauth",
            function="exchange_code",
            endpoint="/api/token",
        )
    return payload


def fetch_spotify_user_id(access_token: str) -> str:
    """
    Resolve the connected account's Spotify user id via GET /me (needs no extra
    scope -- `id` is always returned). Used to stamp `spotifyUserId`/`userId` on
    the owner's row so playlists are created under their own account.
    """
    headers = {"Authorization": f"Bearer {access_token}"}
    try:
        response = requests.get(_ME_URL, headers=headers, timeout=_TIMEOUT_SECONDS)
    except requests.RequestException as err:
        raise SpotifyAPIError(
            message=f"Failed to reach Spotify /me: {err}",
            handler="spotify_oauth",
            function="fetch_spotify_user_id",
            endpoint="/me",
        ) from err

    if response.status_code != 200:
        raise SpotifyAPIError(
            message=f"Spotify /me failed (status {response.status_code}).",
            handler="spotify_oauth",
            function="fetch_spotify_user_id",
            endpoint="/me",
        )

    try:
        me = response.json()
    except ValueError as err:
        raise SpotifyAPIError(
            message=f"Spotify /me returned non-JSON: {err}",
            handler="spotify_oauth",
            function="fetch_spotify_user_id",
            endpoint="/me",
        ) from err

    user_id = me.get("id")
    if not user_id:
        raise SpotifyAPIError(
            message="Spotify /me response missing id.",
            handler="spotify_oauth",
            function="fetch_spotify_user_id",
            endpoint="/me",
        )
    return user_id
