"""
XOMTRACKS Auto-Heard Cron
=========================
Reads Dom's Spotify recently-played history and auto-marks the matching tracks
heard for Dom, so the feed's "unheard" filter reflects what he has actually
listened to without him tapping "heard" on every card.

PER-USER (self-serve foundation Phase 2): each owner who connected their Spotify
(a xomtracks-users row with a refreshToken) is auto-marked heard from THEIR OWN
`/me/player/recently-played`, keyed to THEIR Cognito login email so it surfaces
in THEIR feed filter. Dom, until he re-connects via OAuth, is served by the
shared service account with heard rows keyed to AUTO_HEARD_RATER_EMAIL (his
Cognito LOGIN email) -- identical to before. When OWNER_SCOPING_ENABLED is off
the cron reverts to the single service/default owner (exact pre-Phase-2 path).

SCOPE: `/me/player/recently-played` requires the `user-read-recently-played`
scope on the service token. The reused xomify refresh token already carries it
(verified). A regression surfaces as a 403 from Spotify, which this cron raises
(alarms on the cron error metric) rather than silently no-op'ing.

Flow:
  1. build the service Spotify client (USER token) and GET recently-played.
  2. dedup to newest occurrence per track -> `spotify:<id>` trackKeys.
  3. upsert each heard=True for AUTO_HEARD_RATER_EMAIL, persisting the Spotify
     `played_at` as heardAt ("when heard").

EventBridge-scheduled (xomtracks-cron-auto-heard) -- also runnable locally
(`python -m lambdas.cron_auto_heard.handler`) with the seeded token + SSM creds.
Pure logic (track_keys_from_recently_played / run_auto_heard) is network-free so
it unit-tests without live Spotify.
"""

import asyncio
from datetime import datetime, timezone
from typing import Any, Callable

import aiohttp

from lambdas.common.constants import (
    AUTO_HEARD_RATER_EMAIL,
    DEFAULT_OWNER_ID,
    OWNER_SCOPING_ENABLED,
)
from lambdas.common.dynamo_helpers import list_spotify_connected_users
from lambdas.common.errors import ValidationError, handle_errors
from lambdas.common.heard_dynamo import set_heard
from lambdas.common.logger import get_logger
from lambdas.common.playlist_service import build_owner_client

log = get_logger(__file__)

HANDLER = "cron_auto_heard"


def _played_at_epoch(played_at: str | None) -> int | None:
    """Parse Spotify's `played_at` ISO-8601 (…Z) timestamp to a unix epoch."""
    if not played_at:
        return None
    try:
        normalized = played_at.replace("Z", "+00:00")
        return int(datetime.fromisoformat(normalized).timestamp())
    except (TypeError, ValueError):
        log.warning(f"Unparseable played_at: {played_at!r}")
        return None


def track_keys_from_recently_played(items: list[dict]) -> list[tuple[str, int | None]]:
    """
    Map recently-played items to (trackKey, playedAtEpoch), deduped to the
    NEWEST occurrence of each track (Spotify returns items newest-first, so the
    first occurrence wins). Items without a track id are skipped. Pure -- no I/O.
    """
    out: list[tuple[str, int | None]] = []
    seen: set[str] = set()
    for item in items or []:
        track = item.get("track") or {}
        track_id = track.get("id")
        if not track_id:
            continue
        track_key = f"spotify:{track_id}"
        if track_key in seen:
            continue
        seen.add(track_key)
        out.append((track_key, _played_at_epoch(item.get("played_at"))))
    return out


def run_auto_heard(
    items: list[dict],
    rater_email: str,
    persist: Callable[[str, str, int | None], Any],
) -> dict:
    """
    Mark every recently-played track heard for `rater_email` via the injected
    `persist(trackKey, raterEmail, playedAtEpoch)`. Returns a summary. Pure
    orchestration over an injectable persist edge (mirrors the matching sweep),
    so it unit-tests against a moto table with no live Spotify.
    """
    keyed = track_keys_from_recently_played(items)
    examples: list[dict] = []
    for track_key, played_at in keyed:
        persist(track_key, rater_email, played_at)
        if len(examples) < 8:
            examples.append({"trackKey": track_key, "playedAt": played_at})

    return {
        "processed": len(items or []),
        "marked": len(keyed),
        "rater": rater_email,
        "examples": examples,
    }


def _auto_heard_owners() -> list[dict]:
    """
    The owners to auto-mark heard for this run -- Phase 2 (per-user OAuth).

    Owner scoping ON: every connected owner (their OWN recently-played, keyed to
    THEIR Cognito login email), plus Dom via the shared service account (rater =
    AUTO_HEARD_RATER_EMAIL) if he hasn't connected via OAuth yet -- Dom served
    exactly as today.

    Owner scoping OFF: the single service/default owner only (rater =
    AUTO_HEARD_RATER_EMAIL) -- an exact revert to the pre-multi-tenant cron.

    Each entry: {ownerId, rater} where `rater` is the Cognito email the heard
    rows are keyed by (so they surface in THAT user's own "unheard" filter).
    """
    if not OWNER_SCOPING_ENABLED:
        return [{"ownerId": DEFAULT_OWNER_ID, "rater": AUTO_HEARD_RATER_EMAIL}]

    owners: list[dict] = []
    seen: set[str] = set()
    for row in list_spotify_connected_users():
        owner_id = row.get("ownerId")
        rater = row.get("email")
        if not owner_id or owner_id in seen or not rater:
            continue
        seen.add(owner_id)
        owners.append({"ownerId": owner_id, "rater": rater})

    if DEFAULT_OWNER_ID not in seen and AUTO_HEARD_RATER_EMAIL:
        owners.append({"ownerId": DEFAULT_OWNER_ID, "rater": AUTO_HEARD_RATER_EMAIL})
    return owners


async def _fetch_recently_played_for_owners(owners: list[dict], limit: int = 50) -> list[tuple[str, list[dict]]]:
    """Fetch each owner's recently-played via THEIR token (service fallback for Dom)."""
    out: list[tuple[str, list[dict]]] = []
    async with aiohttp.ClientSession() as session:
        for owner in owners:
            spotify, _user_id, _is_fallback = await build_owner_client(session, owner["ownerId"])
            items = await spotify.aiohttp_get_recently_played(limit=limit)
            out.append((owner["rater"], items))
    return out


def auto_mark_heard() -> dict:
    """Runnable core shared by the Lambda handler and local invocation."""
    owners = _auto_heard_owners()
    if not owners:
        # Fail loud: no connected owner AND no configured rater email means every
        # heard row would be keyed to nobody, invisible to every user's filter.
        raise ValidationError(
            message="No auto-heard owners resolved (no connected users, no AUTO_HEARD_RATER_EMAIL)",
            handler=HANDLER,
            function="auto_mark_heard",
            field="AUTO_HEARD_RATER_EMAIL",
        )

    log.info(f"Auto-heard starting for {len(owners)} owner(s)")
    fetched = asyncio.run(_fetch_recently_played_for_owners(owners))

    def persist(track_key: str, email: str, played_at: int | None) -> None:
        set_heard(track_key, email, True, heard_at=played_at)

    summaries: list[dict] = []
    total_processed = 0
    total_marked = 0
    for rater_email, items in fetched:
        summary = run_auto_heard(items, rater_email, persist)
        summaries.append(summary)
        total_processed += summary["processed"]
        total_marked += summary["marked"]

    log.info(f"Auto-heard complete: owners={len(summaries)} processed={total_processed} marked={total_marked}")
    return {"owners": summaries, "processed": total_processed, "marked": total_marked}


@handle_errors(HANDLER)
def handler(event: dict, context: Any) -> dict:
    """EventBridge entry point. Returns the summary directly."""
    return auto_mark_heard()


if __name__ == "__main__":
    import json

    print(json.dumps(auto_mark_heard(), indent=2))
