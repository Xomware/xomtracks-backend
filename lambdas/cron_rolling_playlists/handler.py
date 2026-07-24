"""
XOMTRACKS Rolling Playlists Cron
================================
Rebuilds TWO public playlists PER OWNER from the trailing 30 days of that
owner's MATCHED shares, updated IN PLACE each run. Never creates duplicates.

- "Xomtracks — Shared With Me (Last Month)"  <- direction=in
- "Xomtracks — Shared By Me (Last Month)"     <- direction=out

Self-serve foundation Phase 2 -- per-user Spotify OAuth + owner scoping:
  * OWNER ENUMERATION: every user who connected their Spotify (a xomtracks-users
    row with a refreshToken) gets their OWN pair of rolling playlists built on
    THEIR account, with the playlist ids persisted on THEIR user row. Dom, until
    he re-connects via OAuth, is served by the shared service account (his seeded
    token) with ids in the two legacy SSM params -- so his experience is byte-for
    -byte identical.
  * OWNER SCOPING: each owner's playlist is built from THEIR OWN shares
    (query_shares_by_owner_direction over GSI-3) when OWNER_SCOPING_ENABLED. When
    the flag is off, the cron reverts to the pre-multi-tenant path: the single
    service owner, the legacy direction query (GSI-1), the SSM ids -- an instant,
    code-free rollback.

Flow per (owner, direction):
  1. query the owner's shares in the direction+window, newest-first.
  2. collect ordered-unique resolvedSpotifyUri.
  3. read that owner's playlist id (their user row, or SSM for the service
     account). "unset"/absent -> create -> persist id. present -> replace tracks
     in place + re-assert cover.
  4. recreate-on-failure fallback: if an in-place update throws AND the playlist
     is genuinely gone, rebuild fresh + persist. A transient error on a still
     -present playlist re-raises (never spawns a duplicate).

EventBridge-scheduled (xomtracks-cron-rolling-playlists, Saturdays).
"""

import asyncio
import time
from datetime import datetime, timezone
from typing import Any

import aiohttp

from lambdas.common.constants import (
    DEFAULT_OWNER_ID,
    OWNER_SCOPING_ENABLED,
    PLAYABLE_MATCH_STATUSES,
    PLAYLIST_ID_UNSET,
    ROLLING_IN_PLAYLIST_PARAM,
    ROLLING_OUT_PLAYLIST_PARAM,
    ROLLING_PLAYLIST_NAMES,
    ROLLING_WINDOW_DAYS,
    USERS_TABLE_NAME,
)
from lambdas.common.dynamo_helpers import (
    list_spotify_connected_users,
    update_table_item_field,
)
from lambdas.common.errors import handle_errors
from lambdas.common.logger import get_logger
from lambdas.common.playlist_service import (
    build_owner_client,
    ordered_unique_uris,
    playlist_exists,
    playlist_url,
    upsert_playlist,
)
from lambdas.common.shares_dynamo import (
    query_shares_by_direction,
    query_shares_by_owner_direction,
)
from lambdas.common.ssm_helpers import get_ssm_param, put_ssm_param

log = get_logger(__file__)

HANDLER = "cron_rolling_playlists"

_DIRECTION_PARAM = {
    "in": ROLLING_IN_PLAYLIST_PARAM,
    "out": ROLLING_OUT_PLAYLIST_PARAM,
}
# Per-owner rolling-playlist ids live on the owner's xomtracks-users row (the
# service account keeps using the two SSM params instead -- see _playlist_id_*).
_DIRECTION_ROW_ATTR = {
    "in": "rollingInPlaylistId",
    "out": "rollingOutPlaylistId",
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


def _rolling_owners() -> list[dict]:
    """
    The owners to build rolling playlists for this run.

    Owner scoping ON: every connected owner (their OWN token + their OWN
    per-row playlist ids), plus Dom via the shared service account (SSM ids) if
    he hasn't connected via OAuth yet -- so Dom is served exactly as today.

    Owner scoping OFF: the single service/default owner only, legacy SSM ids --
    an exact revert to the pre-multi-tenant cron.

    Each entry: {ownerId, email|None, isService: bool}.
    """
    if not OWNER_SCOPING_ENABLED:
        return [{"ownerId": DEFAULT_OWNER_ID, "email": None, "isService": True}]

    owners: list[dict] = []
    seen: set[str] = set()
    for row in list_spotify_connected_users():
        owner_id = row.get("ownerId")
        if not owner_id or owner_id in seen:
            continue
        seen.add(owner_id)
        owners.append({"ownerId": owner_id, "email": row.get("email"), "isService": False, "row": row})

    if DEFAULT_OWNER_ID not in seen:
        owners.append({"ownerId": DEFAULT_OWNER_ID, "email": None, "isService": True})
    return owners


def _get_playlist_id(owner: dict, direction: str) -> str | None:
    """Read the owner's current playlist id for a direction (None/"unset" => none)."""
    if owner.get("isService"):
        return get_ssm_param(_DIRECTION_PARAM[direction])

    existing = (owner.get("row") or {}).get(_DIRECTION_ROW_ATTR[direction])
    # Dom connected but hasn't migrated his two legacy SSM ids onto his row yet:
    # seed from SSM so the cron UPDATES his existing rolling playlists instead of
    # creating duplicates. One-time; once persisted to his row it wins.
    if not existing and owner.get("ownerId") == DEFAULT_OWNER_ID:
        try:
            existing = get_ssm_param(_DIRECTION_PARAM[direction])
        except Exception as err:  # noqa: BLE001 -- best-effort seed, never fatal
            log.warning(f"Could not seed rolling id from SSM for default owner: {err}")
            existing = None
    return existing


def _set_playlist_id(owner: dict, direction: str, playlist_id: str) -> None:
    """Persist a (new/changed) playlist id back to the owner's store."""
    if owner.get("isService"):
        put_ssm_param(_DIRECTION_PARAM[direction], playlist_id)
        log.info(f"Persisted {direction} playlist id to SSM: {_DIRECTION_PARAM[direction]}")
        return

    update_table_item_field(
        USERS_TABLE_NAME, "email", owner["email"], _DIRECTION_ROW_ATTR[direction], playlist_id
    )
    # Keep the legacy SSM param coherent for the default owner so a flag-off
    # rollback (which reads SSM) still finds the current id.
    if owner.get("ownerId") == DEFAULT_OWNER_ID:
        try:
            put_ssm_param(_DIRECTION_PARAM[direction], playlist_id)
        except Exception as err:  # noqa: BLE001 -- mirror is best-effort
            log.warning(f"Could not mirror default-owner rolling id to SSM: {err}")


def _playable_uris(owner_id: str, direction: str, since_epoch: int) -> list[str]:
    """Newest-first, ordered-unique Spotify URIs for the owner's direction window."""
    if OWNER_SCOPING_ENABLED:
        shares = query_shares_by_owner_direction(owner_id, direction, since_epoch)
    else:
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
    owner: dict,
    direction: str,
    since_epoch: int,
) -> dict:
    name = ROLLING_PLAYLIST_NAMES[direction]
    description = _description(direction)
    uris = _playable_uris(owner["ownerId"], direction, since_epoch)
    existing_id = _get_playlist_id(owner, direction)

    try:
        playlist_id = await upsert_playlist(
            session, spotify, user_id,
            playlist_id=existing_id, name=name, description=description, uris=uris,
        )
    except Exception as err:
        # Recreate-on-failure, but ONLY when the existing playlist is genuinely
        # gone. A transient in-place update error must NEVER spawn a duplicate
        # for a playlist that's actually fine -- verify existence first and
        # re-raise (let the run alarm + retry next week) if it's still there.
        had_id = bool(existing_id) and existing_id != PLAYLIST_ID_UNSET
        if had_id and await playlist_exists(session, spotify, existing_id):
            log.error(
                f"{owner['ownerId']} {direction} playlist update failed but {existing_id} "
                f"still exists; NOT recreating (avoids duplicate): {err}"
            )
            raise
        log.warning(f"{owner['ownerId']} {direction} playlist missing/uncreated ({err}); creating fresh")
        playlist_id = await upsert_playlist(
            session, spotify, user_id,
            playlist_id=None, name=name, description=description, uris=uris,
        )

    if playlist_id != existing_id:
        _set_playlist_id(owner, direction, playlist_id)

    return {
        "direction": direction,
        "playlistId": playlist_id,
        "url": playlist_url(playlist_id),
        "name": name,
        "trackCount": len(uris),
        "created": playlist_id != existing_id,
    }


async def _run() -> list[dict]:
    since_epoch = int(time.time()) - ROLLING_WINDOW_DAYS * 24 * 3600
    owners = _rolling_owners()
    results: list[dict] = []
    async with aiohttp.ClientSession() as session:
        for owner in owners:
            spotify, user_id, is_fallback = await build_owner_client(session, owner["ownerId"])
            per_direction: dict[str, dict] = {}
            for direction in ("in", "out"):
                per_direction[direction] = await _sync_direction(
                    session, spotify, user_id, owner, direction, since_epoch
                )
            results.append(
                {
                    "ownerId": owner["ownerId"],
                    "serviceFallback": is_fallback,
                    "playlists": per_direction,
                }
            )
    return results


def rebuild_rolling_playlists() -> dict:
    """Runnable core shared by the Lambda handler and local invocation."""
    log.info(f"Rolling playlists rebuild starting (window={ROLLING_WINDOW_DAYS}d)")
    owners = asyncio.run(_run())
    for owner in owners:
        for direction, r in owner["playlists"].items():
            log.info(
                f"Rolling[{owner['ownerId']}] {direction}: {r['trackCount']} track(s) -> "
                f"{r['url']} ({'created' if r['created'] else 'updated'})"
            )
    return {"owners": owners}


@handle_errors(HANDLER)
def handler(event: dict, context: Any) -> dict:
    """EventBridge entry point. Returns the per-owner/per-direction summary."""
    return rebuild_rolling_playlists()


if __name__ == "__main__":
    import json

    print(json.dumps(rebuild_rolling_playlists(), indent=2))
