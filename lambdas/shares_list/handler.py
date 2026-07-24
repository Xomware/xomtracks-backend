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

from lambdas.common.constants import OWNER_SCOPING_ENABLED
from lambdas.common.errors import ValidationError, handle_errors
from lambdas.common.genres import ensure_genres
from lambdas.common.heard_dynamo import enrich_shares_with_heard
from lambdas.common.logger import get_logger
from lambdas.common.ratings_dynamo import enrich_shares_with_ratings
from lambdas.common.shares_dynamo import (
    query_shares_by_direction,
    query_shares_by_owner_direction,
)
from lambdas.common.utility_helpers import (
    get_caller_owner,
    get_query_params,
    success_response,
)

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
    # Authed route -- raises AuthorizationError (401) if the caller's xomify
    # token is missing/invalid. The verified email is BOTH the ownerId used for
    # owner-scoping below AND drives each share's rating.myRating.
    email = get_caller_owner(event)

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

    # Read cutover (Phase 1C), flag-gated for instant rollback. When owner
    # scoping is ON we scope the feed to the CALLER'S OWN ownerId (their
    # normalized email) via GSI-3 -- for Dom (caller email == the owner stamped
    # on every row post-migration) the result set is IDENTICAL to the legacy
    # GSI-1 direction query (proven by the parity test); a second user sees only
    # their own shares. If the flag is OFF we fall back to the legacy GSI-1 path
    # so the feed can never break. Flip OWNER_SCOPING_ENABLED off = instant revert.
    if OWNER_SCOPING_ENABLED and email:
        shares = query_shares_by_owner_direction(email, direction, since_epoch)
    else:
        shares = query_shares_by_direction(direction, since_epoch)

    # Newest first -- most useful default for a browse UI.
    shares.sort(key=lambda s: s.get("messageDate", 0), reverse=True)

    # Attach each share's trackKey + rating {avg, count, myRating} so the feed
    # renders whole-group ratings without N extra calls (batch-loaded for the
    # page's unique tracks).
    enrich_shares_with_ratings(shares, email)

    # Attach each share's `heard` (the caller's per-song listen state, default
    # False) so the feed can offer an "unheard" filter. Runs after the ratings
    # enrichment so it can reuse the trackKey it already set on each share.
    enrich_shares_with_heard(shares, email)

    # Guarantee every share carries `genres` as a string[] so the frontend
    # genre filter can read it unconditionally (historical shares default []).
    ensure_genres(shares)

    return success_response({"shares": shares, "direction": direction, "window": window, "count": len(shares)})
