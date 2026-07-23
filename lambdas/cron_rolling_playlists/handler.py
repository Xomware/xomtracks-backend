"""
XOMTRACKS Rolling Playlists Cron
================================
Rebuilds TWO public playlists on Dom's Spotify profile from the trailing
30 days of MATCHED shares, updated IN PLACE each run (playlist ids persisted
in SSM -- create once when absent, else atomic PUT-replace). Never creates
duplicates.

- "Xomtracks — Shared With Me (Last Month)"  <- direction=in
- "Xomtracks — Shared By Me (Last Month)"     <- direction=out

Each = last ROLLING_WINDOW_DAYS days of shares in that direction that
resolved to a Spotify track (matchStatus in matched/manual, resolvedSpotifyUri
set), newest-first, ordered-unique. Cover art = the committed Xomtracks logo.

Flow per direction:
  1. GSI-1 query shares in the direction+window, sort newest-first.
  2. Collect ordered-unique resolvedSpotifyUri.
  3. Read the direction's playlist id from SSM.
     - "unset"/absent -> create (cover + tracks) -> PutParameter the new id.
     - present        -> replace tracks in place + re-assert cover.
  4. Recreate-on-failure fallback: if an in-place update throws, rebuild a
     fresh playlist and persist the new id (mirrors release-radar's
     self-healing).

EventBridge-scheduled (xomtracks-cron-rolling-playlists, Saturdays) -- but
also runnable locally (`python -m lambdas.cron_rolling_playlists.handler`)
with the seeded token + SSM creds.
"""

import asyncio
import time
from datetime import datetime, timezone
from typing import Any

import aiohttp

from lambdas.common.constants import (
    PLAYABLE_MATCH_STATUSES,
    ROLLING_IN_PLAYLIST_PARAM,
    ROLLING_OUT_PLAYLIST_PARAM,
    ROLLING_PLAYLIST_NAMES,
    ROLLING_WINDOW_DAYS,
)
from lambdas.common.errors import handle_errors
from lambdas.common.logger import get_logger
from lambdas.common.constants import PLAYLIST_ID_UNSET
from lambdas.common.playlist_service import (
    build_service_client,
    ordered_unique_uris,
    playlist_exists,
    playlist_url,
    upsert_playlist,
)
from lambdas.common.shares_dynamo import query_shares_by_direction
from lambdas.common.ssm_helpers import get_ssm_param, put_ssm_param

log = get_logger(__file__)

HANDLER = "cron_rolling_playlists"

_DIRECTION_PARAM = {
    "in": ROLLING_IN_PLAYLIST_PARAM,
    "out": ROLLING_OUT_PLAYLIST_PARAM,
}
_DIRECTION_BLURB = {
    "in": "shared with you",
    "out": "shared by you",
}


def _description(direction: str) -> str:
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    return (
        f"The last {ROLLING_WINDOW_DAYS} days of tracks {_DIRECTION_BLURB[direction]} "
        f"on iMessage. Auto-updated by Xomtracks · {today}."
    )


def _playable_uris(direction: str, since_epoch: int) -> list[str]:
    """Newest-first, ordered-unique Spotify URIs for the direction's window."""
    shares = query_shares_by_direction(direction, since_epoch)
    shares.sort(key=lambda s: s.get("messageDate", 0), reverse=True)
    playable = [
        s
        for s in shares
        if s.get("matchStatus") in PLAYABLE_MATCH_STATUSES and s.get("resolvedSpotifyUri")
    ]
    return ordered_unique_uris(playable)


async def _sync_direction(
    session: aiohttp.ClientSession,
    spotify,
    user_id: str,
    direction: str,
    since_epoch: int,
) -> dict:
    param = _DIRECTION_PARAM[direction]
    name = ROLLING_PLAYLIST_NAMES[direction]
    description = _description(direction)
    uris = _playable_uris(direction, since_epoch)
    existing_id = get_ssm_param(param)

    try:
        playlist_id = await upsert_playlist(
            session, spotify, user_id,
            playlist_id=existing_id, name=name, description=description, uris=uris,
        )
    except Exception as err:
        # Recreate-on-failure, but ONLY when the existing playlist is
        # genuinely gone. A transient in-place update error must NEVER spawn
        # a duplicate for a playlist that's actually fine -- so we verify
        # existence first and re-raise (let the run alarm + retry next week)
        # if it's still there.
        had_id = bool(existing_id) and existing_id != PLAYLIST_ID_UNSET
        if had_id and await playlist_exists(session, spotify, existing_id):
            log.error(
                f"{direction} playlist update failed but {existing_id} still exists; "
                f"NOT recreating (avoids duplicate): {err}"
            )
            raise
        log.warning(f"{direction} playlist missing/uncreated ({err}); creating fresh")
        playlist_id = await upsert_playlist(
            session, spotify, user_id,
            playlist_id=None, name=name, description=description, uris=uris,
        )

    if playlist_id != existing_id:
        put_ssm_param(param, playlist_id)
        log.info(f"Persisted {direction} playlist id to SSM: {param}")

    return {
        "direction": direction,
        "playlistId": playlist_id,
        "url": playlist_url(playlist_id),
        "name": name,
        "trackCount": len(uris),
        "created": playlist_id != existing_id,
    }


async def _run() -> dict:
    since_epoch = int(time.time()) - ROLLING_WINDOW_DAYS * 24 * 3600
    results: dict[str, dict] = {}
    async with aiohttp.ClientSession() as session:
        spotify, user_id = await build_service_client(session)
        for direction in ("in", "out"):
            results[direction] = await _sync_direction(
                session, spotify, user_id, direction, since_epoch
            )
    return results


def rebuild_rolling_playlists() -> dict:
    """Runnable core shared by the Lambda handler and local invocation."""
    log.info(f"Rolling playlists rebuild starting (window={ROLLING_WINDOW_DAYS}d)")
    results = asyncio.run(_run())
    for direction, r in results.items():
        log.info(
            f"Rolling {direction}: {r['trackCount']} track(s) -> {r['url']} "
            f"({'created' if r['created'] else 'updated'})"
        )
    return {"playlists": results}


@handle_errors(HANDLER)
def handler(event: dict, context: Any) -> dict:
    """EventBridge entry point. Returns the per-direction summary directly."""
    return rebuild_rolling_playlists()


if __name__ == "__main__":
    import json

    print(json.dumps(rebuild_rolling_playlists(), indent=2))
