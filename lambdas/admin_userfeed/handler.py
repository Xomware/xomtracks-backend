"""
GET /admin/user-feed?email=<target>&direction=in|out&window=<w> -- impersonation
(view-as, READ-ONLY), admin-gated.
========================================================================
Returns the TARGET user's feed EXACTLY as they would see it -- the always-on
global union of Dom's baseline shares (DEFAULT_OWNER_ID) + the target's own
owner-scoped shares (shares_dynamo.query_owner_feed with the target as the
caller-owner), enriched with the target's own ratings/heard state. Strictly
read-only: no writes are ever performed AS the target (the directory auto-upsert
+ request-log record the ADMIN caller, never the target).

Gated in-handler to the admin (Dom). 404 if the target is not in the directory
(user_directory.is_known_user). See docs/features/xomtracks-xomify-merge/PLAN.md
WS6.
"""

import time
from typing import Any

from lambdas.common.errors import NotFoundError, ValidationError, handle_errors
from lambdas.common.genres import ensure_genres
from lambdas.common.heard_dynamo import enrich_shares_with_heard
from lambdas.common.logger import get_logger
from lambdas.common.ratings_dynamo import enrich_shares_with_ratings
from lambdas.common.shares_dynamo import query_owner_feed
from lambdas.common.user_directory import is_known_user
from lambdas.common.utility_helpers import (
    get_query_params,
    require_admin,
    success_response,
)

log = get_logger(__file__)

HANDLER = "admin_userfeed"

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
    # 401 if not signed in, 403 if signed in but not the admin.
    require_admin(event)

    params = get_query_params(event)
    target = (params.get("email") or "").strip().lower()
    direction = params.get("direction")
    window = params.get("window", "all")

    if not target:
        raise ValidationError(
            message="email query param is required (the target user to view as)",
            handler=HANDLER,
            function="handler",
            field="email",
        )
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

    if not is_known_user(target):
        raise NotFoundError(
            message=f"User not in directory: {target}",
            handler=HANDLER,
            function="handler",
            resource="user",
        )

    since_epoch = _since_epoch_for_window(window)

    # The target's feed AS THEY see it: union of Dom's global baseline + the
    # target's own shares, deduped by shareId (identical logic to /shares/list).
    shares = query_owner_feed(target, direction, since_epoch)
    shares.sort(key=lambda s: s.get("messageDate", 0), reverse=True)

    # Enrich with the TARGET's own rating/heard state (read-only) so the view-as
    # matches exactly what they'd see -- including their myRating + heard flags.
    enrich_shares_with_ratings(shares, target)
    enrich_shares_with_heard(shares, target)
    ensure_genres(shares)

    log.info(f"Admin viewed feed as {target} (direction={direction}, window={window}, count={len(shares)})")

    return success_response({
        "email": target,
        "shares": shares,
        "direction": direction,
        "window": window,
        "count": len(shares),
    })
