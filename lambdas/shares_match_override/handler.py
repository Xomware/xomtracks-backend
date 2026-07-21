"""
POST /shares/{shareId}/match-override - manual override endpoint.

Accepts a Spotify track id (or full URL/URI), hydrates it, and sets
matchStatus=manual on the share. Backs the UI "pick the match" affordance
for permanently-unmatched (SC remix/bootlegs-not-on-Spotify) shares.

Authed route -- any signed-in xomtracks user can override (there is no
per-share ownership concept; Dom is the only real participant across every
conversation, per PLAN.md's "why one extractor covers everything").
"""

import asyncio
from typing import Any

import aiohttp
from pydantic import ValidationError as PydanticValidationError

from lambdas.common.errors import NotFoundError, ValidationError, handle_errors
from lambdas.common.logger import get_logger
from lambdas.common.matching import apply_manual_override
from lambdas.common.models import MatchOverrideRequest
from lambdas.common.shares_dynamo import get_share, update_match_result
from lambdas.common.spotify import Spotify
from lambdas.common.utility_helpers import get_caller_email, get_path_params, parse_body, success_response

log = get_logger(__file__)

HANDLER = "shares_match_override"


def _build_spotify_client(session: aiohttp.ClientSession) -> Spotify:
    """
    Build xomtracks' Spotify client for the app's own service-account user
    row (the app has a single Spotify-connected account it plays/searches
    through -- see PLAN.md's "own token-keepalive and users/token row").
    Broken out as a module-level function so tests can patch it without
    needing a real DynamoDB users table.
    """
    from lambdas.common.dynamo_helpers import get_app_service_user

    user = get_app_service_user()
    return Spotify(user, session)


async def _resolve_override(share_id: str, spotify_track_id: str) -> dict:
    async with aiohttp.ClientSession() as session:
        spotify = _build_spotify_client(session)
        return await apply_manual_override(spotify, spotify_track_id)


@handle_errors(HANDLER)
def handler(event: dict, context: Any) -> dict:
    get_caller_email(event)

    path_params = get_path_params(event)
    share_id = path_params.get("shareId")
    if not share_id:
        raise ValidationError(
            message="Missing required path parameter: shareId",
            handler=HANDLER,
            function="handler",
            field="shareId",
        )

    existing = get_share(share_id)
    if not existing:
        raise NotFoundError(
            message=f"Share not found: {share_id}",
            handler=HANDLER,
            function="handler",
            resource=f"shares/{share_id}",
        )

    body = parse_body(event)
    try:
        req = MatchOverrideRequest(**body)
    except PydanticValidationError as err:
        raise ValidationError(
            message=f"Invalid override payload: {err}",
            handler=HANDLER,
            function="handler",
        ) from err

    match_result = asyncio.run(_resolve_override(share_id, req.spotifyTrackId))
    updated = update_match_result(share_id, **match_result)

    log.info(f"Manual override applied: shareId={share_id} spotifyTrackId={req.spotifyTrackId}")

    return success_response(updated)
