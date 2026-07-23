"""
XOMTRACKS Auto-Heard Cron
=========================
Reads Dom's Spotify recently-played history and auto-marks the matching tracks
heard for Dom, so the feed's "unheard" filter reflects what he has actually
listened to without him tapping "heard" on every card.

DOM-ONLY for now (documented fast-follow): the app has a SINGLE Spotify
service-account token (Dom's, reused from xomify, stored in xomtracks-users).
That token's `/me/player/recently-played` is DOM's listening history, so this
cron can only auto-mark heard for Dom. Per-user Spotify OAuth -- which would let
each member's recently-played auto-mark THEIR own heard state -- is the
fast-follow. The heard rows are keyed by AUTO_HEARD_RATER_EMAIL (Dom's Cognito
LOGIN email, NOT the Spotify service-account row email) so they surface in Dom's
own feed filter.

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

from lambdas.common.constants import AUTO_HEARD_RATER_EMAIL
from lambdas.common.errors import ValidationError, handle_errors
from lambdas.common.heard_dynamo import set_heard
from lambdas.common.logger import get_logger
from lambdas.common.playlist_service import build_service_client

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


async def _fetch_recently_played(limit: int = 50) -> list[dict]:
    async with aiohttp.ClientSession() as session:
        spotify, _user_id = await build_service_client(session)
        return await spotify.aiohttp_get_recently_played(limit=limit)


def auto_mark_heard() -> dict:
    """Runnable core shared by the Lambda handler and local invocation."""
    rater_email = AUTO_HEARD_RATER_EMAIL
    if not rater_email:
        # Fail loud: without a rater email the cron would write heard rows keyed
        # to nobody, invisible to every user's filter. Alarm instead.
        raise ValidationError(
            message="AUTO_HEARD_RATER_EMAIL is not configured",
            handler=HANDLER,
            function="auto_mark_heard",
            field="AUTO_HEARD_RATER_EMAIL",
        )

    log.info(f"Auto-heard starting for rater {rater_email}")
    items = asyncio.run(_fetch_recently_played())

    def persist(track_key: str, email: str, played_at: int | None) -> None:
        set_heard(track_key, email, True, heard_at=played_at)

    summary = run_auto_heard(items, rater_email, persist)
    log.info(f"Auto-heard complete: processed={summary['processed']} marked={summary['marked']}")
    return summary


@handle_errors(HANDLER)
def handler(event: dict, context: Any) -> dict:
    """EventBridge entry point. Returns the summary directly."""
    return auto_mark_heard()


if __name__ == "__main__":
    import json

    print(json.dumps(auto_mark_heard(), indent=2))
