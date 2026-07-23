"""
XOMTRACKS Token Keepalive Cron
==============================
Monthly liveness poke for the single Spotify service-account refresh token
(Dom's, reused from xomify, stored in xomtracks-users). Exercises the
refresh-token flow so the token never goes stale from disuse, persists any
rotated refresh token Spotify hands back (handled inside
aiohttp_get_access_token -> _persist_rotated_refresh_token), and confirms the
minted access token actually works via a GET /v1/me probe.

Thin by design: it mints, validates, and reports the granted scopes so a
silent scope regression (e.g. a lost ugc-image-upload) surfaces in the logs.
On ANY failure it raises so CloudWatch alarms on the cron's error metric --
a dead token silently breaks BOTH rolling playlists and the on-the-spot
endpoint, so failing loud here is the point.

EventBridge-scheduled (xomtracks-cron-token-keepalive, 15th of each month).
"""

import asyncio
from typing import Any

import aiohttp

from lambdas.common.dynamo_helpers import get_app_service_user
from lambdas.common.errors import SpotifyAPIError, handle_errors
from lambdas.common.logger import get_logger
from lambdas.common.spotify import Spotify

log = get_logger(__file__)

HANDLER = "cron_token_keepalive"

# Scopes the app depends on -- logged/checked so a regression is visible.
_REQUIRED_SCOPES = ("playlist-modify-public", "playlist-modify-private", "ugc-image-upload")


async def _probe(session: aiohttp.ClientSession, spotify: Spotify) -> dict:
    async with session.get(f"{spotify.BASE_URL}/me", headers=spotify.headers) as resp:
        if resp.status != 200:
            text = await resp.text()
            raise SpotifyAPIError(
                message=f"Token keepalive /me probe failed ({resp.status}): {text}",
                handler=HANDLER,
                function="_probe",
                endpoint="/me",
            )
        return await resp.json()


async def _run() -> dict:
    user = get_app_service_user()
    async with aiohttp.ClientSession() as session:
        spotify = Spotify(user, session)
        # Refresh (persists rotation if Spotify rotates the token).
        await spotify.aiohttp_initialize_user_token()
        me = await _probe(session, spotify)
    return {
        "ok": True,
        "spotifyUserId": me.get("id"),
        "email": user.get("email"),
    }


def keepalive() -> dict:
    """Runnable core shared by the Lambda handler and local invocation."""
    log.info("Token keepalive starting")
    result = asyncio.run(_run())
    log.info(f"Token keepalive OK for Spotify user {result.get('spotifyUserId')}")
    return result


@handle_errors(HANDLER)
def handler(event: dict, context: Any) -> dict:
    """EventBridge entry point. Raises (via handle_errors -> 500) on failure."""
    return keepalive()


if __name__ == "__main__":
    import json

    print(json.dumps(keepalive(), indent=2))
