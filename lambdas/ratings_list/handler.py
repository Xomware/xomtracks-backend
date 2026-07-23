"""
GET /ratings/list -- every track the CALLER has rated, across BOTH directions,
with the track info + their own rating value (authed, Cognito-gated). Powers a
true cross-direction "My Rated" view.

Unlike GET /shares/list (scoped to ONE direction) and GET /me/shares (scoped to
the caller's OWN shares), this is keyed off the caller's RATINGS: it returns
one entry per song the caller has rated, regardless of who shared it or which
direction it came in on. Ratings live keyed by trackKey (the normalized SONG
identity), so track metadata (title/artist/art/platform) is joined back in from
a representative share for each rated trackKey.

Returns:
  {"rated": [ {trackKey, rating, ratedAt, trackTitle, trackArtist, albumArtUrl,
               albumName, platform, direction, date}, ... ],
   "count": <n>}
Newest-rated first. A rated track whose share is no longer present still
appears (rating + trackKey) with null track info rather than being dropped.

ROUTE NOTE: GET /ratings/list, not GET /ratings -- the api-gateway-service
module supports exactly two path levels (same constraint as GET /shares/list /
GET /ratings/get). The handler reads the Cognito authorizer context only.
"""

from typing import Any

from lambdas.common.errors import handle_errors
from lambdas.common.logger import get_logger
from lambdas.common.ratings_dynamo import list_ratings_for_rater
from lambdas.common.shares_dynamo import query_shares_by_direction
from lambdas.common.track_key import derive_track_key
from lambdas.common.utility_helpers import get_caller_email, success_response

log = get_logger(__file__)

HANDLER = "ratings_list"


def _richness(share: dict) -> int:
    """
    Score how good a representative a share is for its track's card. Prefer a
    Spotify-resolved share with album art (the richest metadata) so a track
    rated across a SoundCloud + a matched Spotify share renders from the matched
    one. Ties fall back to newest messageDate at the call site.
    """
    score = 0
    if share.get("resolvedSpotifyId"):
        score += 2
    if share.get("albumArtUrl"):
        score += 1
    if share.get("trackTitle"):
        score += 1
    return score


def _build_track_index() -> dict[str, dict]:
    """
    Map every track's normalized key -> its best representative share, across
    BOTH directions (this is what makes "My Rated" cross-direction). GSI-1
    queries (in + out, all-time) cover every share exactly once.
    """
    index: dict[str, dict] = {}
    for direction in ("in", "out"):
        for share in query_shares_by_direction(direction, 0):
            track_key = derive_track_key(share)
            existing = index.get(track_key)
            if existing is None:
                index[track_key] = share
                continue
            if _richness(share) > _richness(existing) or (
                _richness(share) == _richness(existing)
                and share.get("messageDate", 0) > existing.get("messageDate", 0)
            ):
                index[track_key] = share
    return index


def _entry(track_key: str, rating: int, rated_at: int, share: dict | None) -> dict:
    return {
        "trackKey": track_key,
        "rating": rating,
        "ratedAt": rated_at,
        "trackTitle": share.get("trackTitle") if share else None,
        "trackArtist": share.get("trackArtist") if share else None,
        "albumArtUrl": share.get("albumArtUrl") if share else None,
        "albumName": share.get("albumName") if share else None,
        "platform": share.get("platform") if share else None,
        "direction": share.get("direction") if share else None,
        "date": int(share.get("messageDate", 0) or 0) if share else None,
    }


@handle_errors(HANDLER)
def handler(event: dict, context: Any) -> dict:
    # Authed route -- 401 if the Cognito authorizer context is absent.
    email = get_caller_email(event)

    rating_rows = list_ratings_for_rater(email)
    if not rating_rows:
        return success_response({"rated": [], "count": 0})

    track_index = _build_track_index()

    rated: list[dict] = []
    for row in rating_rows:
        track_key = row.get("trackKey")
        if not track_key:
            continue
        rated.append(
            _entry(
                track_key=track_key,
                rating=int(row.get("rating", 0)),
                rated_at=int(row.get("updatedAt", 0) or 0),
                share=track_index.get(track_key),
            )
        )

    # Newest-rated first -- the most useful default for a "My Rated" screen.
    rated.sort(key=lambda r: r["ratedAt"], reverse=True)

    return success_response({"rated": rated, "count": len(rated)})
