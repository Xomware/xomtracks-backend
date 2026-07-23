"""
POST /playlists/create -- on-the-spot playlist builder (authed, Cognito).
========================================================================
Takes a hand-picked selection (shareIds and/or raw Spotify trackIds) + a
playlist name, resolves it to an ordered-unique list of Spotify track URIs,
and creates ONE public playlist on Dom's profile (single-service-account
model) with the Xomtracks logo cover.

This backs the feed's future multi-select "make your own playlist from
history" action. Resolution rules:
  - shareIds  -> looked up in xomtracks-shares; only shares that resolved to
    a Spotify track (resolvedSpotifyUri present) contribute. Unmatched /
    not-found shares are skipped (reported in `skipped`), never fatal.
  - trackIds  -> normalized (bare id / URL / URI) -> spotify:track:{id}.
  - Selection order is preserved (shareIds first, then trackIds), deduped.

Authed: any signed-in xomware user may build a playlist (no per-share
ownership -- same trust boundary as the rest of the app). Playlists are
owned by the service account (Dom), consistent with the rolling playlists.
"""

import asyncio
from typing import Any

import aiohttp
from pydantic import ValidationError as PydanticValidationError

from lambdas.common.errors import ValidationError, handle_errors
from lambdas.common.logger import get_logger
from lambdas.common.models import CreatePlaylistRequest
from lambdas.common.playlist_service import create_playlist, playlist_url
from lambdas.common.shares_dynamo import get_share
from lambdas.common.utility_helpers import get_caller_email, parse_body, success_response

log = get_logger(__file__)

HANDLER = "playlists_create"


def _resolve_uris(req: CreatePlaylistRequest) -> tuple[list[str], list[str]]:
    """
    Resolve the selection to ordered-unique Spotify URIs.

    Returns (uris, skipped_share_ids). A shareId is skipped when it doesn't
    exist or hasn't matched to a Spotify track yet.
    """
    seen: set[str] = set()
    uris: list[str] = []
    skipped: list[str] = []

    for share_id in req.shareIds:
        share = get_share(share_id)
        uri = share.get("resolvedSpotifyUri") if share else None
        if not uri:
            skipped.append(share_id)
            continue
        if uri not in seen:
            seen.add(uri)
            uris.append(uri)

    for track_id in req.trackIds:
        uri = f"spotify:track:{track_id}"
        if uri not in seen:
            seen.add(uri)
            uris.append(uri)

    return uris, skipped


def _default_description(name: str) -> str:
    return f"{name} — built on Xomtracks."


@handle_errors(HANDLER)
def handler(event: dict, context: Any) -> dict:
    # Authed route -- 401 if the Cognito authorizer context is absent.
    get_caller_email(event)

    body = parse_body(event)
    try:
        req = CreatePlaylistRequest(**body)
    except PydanticValidationError as err:
        raise ValidationError(
            message=f"Invalid create-playlist payload: {err}",
            handler=HANDLER,
            function="handler",
        ) from err

    if not req.has_selection():
        raise ValidationError(
            message="At least one of shareIds or trackIds is required",
            handler=HANDLER,
            function="handler",
            field="shareIds",
        )

    uris, skipped = _resolve_uris(req)
    if not uris:
        raise ValidationError(
            message="None of the selected shares/tracks resolved to a Spotify track",
            handler=HANDLER,
            function="handler",
            field="shareIds",
        )

    description = req.description or _default_description(req.name)
    playlist_id = asyncio.run(
        _create(req.name, description, uris)
    )

    log.info(f"On-the-spot playlist created: {playlist_id} ({len(uris)} tracks, {len(skipped)} skipped)")

    return success_response(
        {
            "playlistId": playlist_id,
            "url": playlist_url(playlist_id),
            "name": req.name,
            "trackCount": len(uris),
            "skippedShareIds": skipped,
        }
    )


async def _create(name: str, description: str, uris: list[str]) -> str:
    async with aiohttp.ClientSession() as session:
        return await create_playlist(session, name, description, uris)
