"""
GET /me/shares -- the caller's OWN shares (authed, Cognito-gated).
========================================================================
Returns the shares whose sharerHandle normalizes to one of the caller's linked
handles, newest-first, within a time window (week/month/6mo/all). This is what
a linked group member sees on the feed's "Mine" tab.

NOTE on what a member can actually see: the extractor reads Dom's chat.db, so
the only per-member signal that exists is what a member shared INTO the group
(direction="in", carrying that member's sharerHandle). "Shared BY me / out"
data exists only for Dom (he's the sender on his own device). So for any member
other than Dom, /me/shares surfaces the tracks they dropped into the group
chat -- which is exactly the attribution this feature promises.

Unlinked callers get an empty list flagged linked=false so the UI prompts them
to link their number instead of showing an empty feed.
"""

import time
from typing import Any

from lambdas.common.errors import ValidationError, handle_errors
from lambdas.common.logger import get_logger
from lambdas.common.ratings_dynamo import enrich_shares_with_ratings
from lambdas.common.shares_dynamo import scan_shares_by_normalized_handles
from lambdas.common.user_links import get_linked_handles
from lambdas.common.utility_helpers import get_caller_email, get_query_params, success_response

log = get_logger(__file__)

HANDLER = "me_shares"

# Mirror shares_list's windows so the "Mine" tab behaves identically to the
# main feed's time-window control.
_WINDOW_SECONDS = {
    "week": 7 * 24 * 3600,
    "month": 30 * 24 * 3600,
    "6mo": 6 * 30 * 24 * 3600,
    "all": None,
}


def _since_epoch_for_window(window: str) -> int:
    seconds = _WINDOW_SECONDS[window]
    if seconds is None:
        return 0
    return int(time.time()) - seconds


@handle_errors(HANDLER)
def handler(event: dict, context: Any) -> dict:
    # Authed route -- 401 if the Cognito authorizer context is absent.
    email = get_caller_email(event)

    params = get_query_params(event)
    window = params.get("window", "all")
    if window not in _WINDOW_SECONDS:
        raise ValidationError(
            message=f"window must be one of {list(_WINDOW_SECONDS)}",
            handler=HANDLER,
            function="handler",
            field="window",
        )

    handles = get_linked_handles(email)
    if not handles:
        # No number linked yet -- the UI shows a "link your number" prompt.
        return success_response({
            "shares": [],
            "linked": False,
            "linkedHandles": [],
            "window": window,
            "count": 0,
        })

    since_epoch = _since_epoch_for_window(window)
    shares = scan_shares_by_normalized_handles(handles, since_epoch)
    shares.sort(key=lambda s: s.get("messageDate", 0), reverse=True)

    # Same rating enrichment as the main feed so the "Mine" tab shows each
    # song's whole-group aggregate + the caller's own rating inline.
    enrich_shares_with_ratings(shares, email)

    return success_response({
        "shares": shares,
        "linked": True,
        "linkedHandles": sorted(handles),
        "window": window,
        "count": len(shares),
    })
