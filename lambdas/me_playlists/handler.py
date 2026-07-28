"""
GET /me/playlists -- the caller's rolling playlists (authed, in-handler token).
========================================================================
Replaces the frontend's hardcoded rolling-playlist ids. Returns two pairs:

  {
    "own":      { "in": <entry|null>, "out": <entry|null> },
    "baseline": { "in": <entry|null>, "out": <entry|null> }
  }

where an entry is {"playlistId", "url", "name"} (or null when that playlist
hasn't been generated yet), and:

  - `baseline` is Dom's (DEFAULT_OWNER_ID) always-visible pair -- the global
    playlists every signed-in member sees on the feed regardless of whether
    they've self-served.
  - `own` is the CALLER's own pair, built by the rolling cron on their own
    Spotify once they connect + run it. Null entries until then.
  - For Dom (caller == DEFAULT_OWNER_ID) `own` IS `baseline` -- his own rolling
    playlists are the baseline, so both sides return the same pair.

WHERE THE IDS LIVE (see cron_rolling_playlists): a connected owner's ids are
persisted on their xomtracks-users row under rollingInPlaylistId /
rollingOutPlaylistId. Dom, until he re-connects via OAuth, is served by the
shared service account whose ids live in the two SSM params
(/xomtracks/playlists/ROLLING_{IN,OUT}_PLAYLIST_ID). So baseline resolution
prefers Dom's row attrs and falls back to SSM -- exactly the cron's own
_get_playlist_id logic for the default owner.
"""

from typing import Any

from lambdas.common.constants import (
    DEFAULT_OWNER_ID,
    PLAYLIST_ID_UNSET,
    ROLLING_IN_PLAYLIST_PARAM,
    ROLLING_OUT_PLAYLIST_PARAM,
    ROLLING_PLAYLIST_NAMES,
    ROLLING_PLAYLIST_ROW_ATTRS,
)
from lambdas.common.errors import handle_errors
from lambdas.common.logger import get_logger
from lambdas.common.playlist_service import playlist_url
from lambdas.common.ssm_helpers import get_ssm_param
from lambdas.common.user_links import get_user_record
from lambdas.common.utility_helpers import get_caller_owner, success_response

log = get_logger(__file__)

HANDLER = "me_playlists"

_DIRECTION_PARAM = {
    "in": ROLLING_IN_PLAYLIST_PARAM,
    "out": ROLLING_OUT_PLAYLIST_PARAM,
}


def _entry(playlist_id: str | None, direction: str) -> dict | None:
    """Shape one playlist entry, or None when it hasn't been generated yet."""
    if not playlist_id or playlist_id == PLAYLIST_ID_UNSET:
        return None
    return {
        "playlistId": playlist_id,
        "url": playlist_url(playlist_id),
        "name": ROLLING_PLAYLIST_NAMES[direction],
    }


def _row_playlist_id(row: dict | None, direction: str) -> str | None:
    """The rolling playlist id stored on a user row for a direction, if any."""
    if not row:
        return None
    return row.get(ROLLING_PLAYLIST_ROW_ATTRS[direction])


def _ssm_playlist_id(direction: str) -> str | None:
    """The service-account rolling playlist id from SSM. Fail-open to None so a
    missing/unreadable param degrades to 'not generated yet', never a 500."""
    try:
        return get_ssm_param(_DIRECTION_PARAM[direction])
    except Exception as err:  # noqa: BLE001 -- baseline is best-effort
        log.warning(f"Could not read baseline {direction} playlist id from SSM: {err}")
        return None


def _baseline_pair() -> dict:
    """
    Dom's (DEFAULT_OWNER_ID) rolling pair -- prefer his user-row attrs, fall
    back to the legacy SSM service-account ids. Mirrors the cron's
    _get_playlist_id for the default owner.
    """
    dom_row = get_user_record(DEFAULT_OWNER_ID)
    pair = {}
    for direction in ("in", "out"):
        playlist_id = _row_playlist_id(dom_row, direction) or _ssm_playlist_id(direction)
        pair[direction] = _entry(playlist_id, direction)
    return pair


def _own_pair(caller: str) -> dict:
    """The caller's own rolling pair from their user row (null entries until the
    cron has built their playlists on their connected Spotify)."""
    row = get_user_record(caller)
    return {direction: _entry(_row_playlist_id(row, direction), direction) for direction in ("in", "out")}


@handle_errors(HANDLER)
def handler(event: dict, context: Any) -> dict:
    # Authed route -- 401 if the caller's xomify Bearer token is absent/invalid.
    caller = get_caller_owner(event)

    baseline = _baseline_pair()

    # For Dom, his own rolling playlists ARE the baseline -- return the same pair
    # rather than re-reading (his row/SSM ids are the baseline ids).
    if caller == DEFAULT_OWNER_ID:
        own = baseline
    else:
        own = _own_pair(caller)

    return success_response({"own": own, "baseline": baseline})
