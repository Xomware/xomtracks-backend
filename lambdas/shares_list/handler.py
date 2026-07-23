"""
GET /shares - query by direction + time window (week/month/6mo/all) via
GSI-1. Authed route -- user JWT via the custom authorizer (per-user
identity isn't actually used for scoping the query since Dom is the only
participant across all conversations, but the route stays authed per
PLAN.md's locked "authed, Cognito-gated" visibility decision).

By-sharer query (GSI-2, `?sharer=`) is a fast-follow (FF.2) -- not wired
here yet, even though shares_dynamo.query_shares_by_sharer already exists.
"""

import time
from typing import Any

from lambdas.common.errors import ValidationError, handle_errors
from lambdas.common.genres import ensure_genres
from lambdas.common.logger import get_logger
from lambdas.common.ratings_dynamo import enrich_shares_with_ratings
from lambdas.common.shares_dynamo import query_shares_by_direction
from lambdas.common.utility_helpers import get_caller_email, get_query_params, success_response

log = get_logger(__file__)

HANDLER = "shares_list"

_WINDOW_SECONDS = {
    "week": 7 * 24 * 3600,
    "month": 30 * 24 * 3600,
    "6mo": 6 * 30 * 24 * 3600,
    "all": None,
}
_VALID_DIRECTIONS = ("in", "out")


def _since_epoch_for_window(window: str) -> int:
    seconds = _WINDOW_SECONDS[window]
    if seconds is None:
        return 0
    return int(time.time()) - seconds


@handle_errors(HANDLER)
def handler(event: dict, context: Any) -> dict:
    # Authed route -- raises MissingCallerIdentityError (401) if the
    # custom authorizer context is absent. The caller email also drives
    # each share's rating.myRating below.
    email = get_caller_email(event)

    params = get_query_params(event)
    direction = params.get("direction")
    window = params.get("window", "all")

    if direction not in _VALID_DIRECTIONS:
        raise ValidationError(
            message=f"direction is required and must be one of {_VALID_DIRECTIONS}",
            handler=HANDLER,
            function="handler",
            field="direction",
        )

    if window not in _WINDOW_SECONDS:
        raise ValidationError(
            message=f"window must be one of {list(_WINDOW_SECONDS)}",
            handler=HANDLER,
            function="handler",
            field="window",
        )

    since_epoch = _since_epoch_for_window(window)
    shares = query_shares_by_direction(direction, since_epoch)

    # Newest first -- most useful default for a browse UI.
    shares.sort(key=lambda s: s.get("messageDate", 0), reverse=True)

    # Attach each share's trackKey + rating {avg, count, myRating} so the feed
    # renders whole-group ratings without N extra calls (batch-loaded for the
    # page's unique tracks).
    enrich_shares_with_ratings(shares, email)

    # Guarantee every share carries `genres` as a string[] so the frontend
    # genre filter can read it unconditionally (historical shares default []).
    ensure_genres(shares)

    return success_response({"shares": shares, "direction": direction, "window": window, "count": len(shares)})
