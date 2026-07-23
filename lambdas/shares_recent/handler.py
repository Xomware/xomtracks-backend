"""
GET /shares/recent?limit=5 -- compact, most-recent shares for the xomware.com
hub widget's "powered by Xomtracks" strip (authed, Cognito-gated -- the hub user
is signed into xomware).

Returns a SMALL set of the newest shares in each direction:
  - sharedWithMe  <- direction=in  (tracks dropped into the group chat)
  - sharedByMe    <- direction=out (tracks Dom shared out)
each projected to just what a compact strip renders:
  {title, artist, albumArtUrl, platform, sharer, direction, date}.

`limit` caps EACH direction (default 5, max 20). This is intentionally lighter
than /shares/list -- no rating/heard/genre enrichment, no windowing -- because
the hub strip only needs a handful of cards, not the full feed.

ROUTE NOTE: GET /shares/recent -- a sibling path_part under the `shares` prefix
(same 2-path-level module constraint as GET /shares/list). The handler reads the
Cognito authorizer context + querystring only, not the URL path.
"""

from typing import Any

from lambdas.common.errors import ValidationError, handle_errors
from lambdas.common.logger import get_logger
from lambdas.common.shares_dynamo import query_shares_by_direction
from lambdas.common.utility_helpers import get_caller_email, get_query_params, success_response

log = get_logger(__file__)

HANDLER = "shares_recent"

DEFAULT_LIMIT = 5
MAX_LIMIT = 20


def _parse_limit(raw: str | None) -> int:
    if raw is None or str(raw).strip() == "":
        return DEFAULT_LIMIT
    try:
        value = int(str(raw).strip())
    except (TypeError, ValueError):
        raise ValidationError(
            message="limit must be a positive integer",
            handler=HANDLER,
            function="_parse_limit",
            field="limit",
        )
    if value < 1:
        raise ValidationError(
            message="limit must be a positive integer",
            handler=HANDLER,
            function="_parse_limit",
            field="limit",
        )
    return min(value, MAX_LIMIT)


def _compact(share: dict) -> dict:
    """Project a share to the compact fields the hub strip renders."""
    return {
        "title": share.get("trackTitle"),
        "artist": share.get("trackArtist"),
        "albumArtUrl": share.get("albumArtUrl"),
        "platform": share.get("platform"),
        # Outbound shares (Dom is the sender) carry no sharerHandle/Name -> None;
        # the hub renders that as "You".
        "sharer": share.get("sharerName") or share.get("sharerHandle"),
        "direction": share.get("direction"),
        "date": int(share.get("messageDate", 0) or 0),
    }


def _recent_for_direction(direction: str, limit: int) -> list[dict]:
    shares = query_shares_by_direction(direction, 0)
    shares.sort(key=lambda s: s.get("messageDate", 0), reverse=True)
    return [_compact(s) for s in shares[:limit]]


@handle_errors(HANDLER)
def handler(event: dict, context: Any) -> dict:
    # Authed route -- 401 if the Cognito authorizer context is absent.
    get_caller_email(event)

    params = get_query_params(event)
    limit = _parse_limit(params.get("limit"))

    shared_with_me = _recent_for_direction("in", limit)
    shared_by_me = _recent_for_direction("out", limit)

    return success_response({
        "sharedWithMe": shared_with_me,
        "sharedByMe": shared_by_me,
        "limit": limit,
        "count": len(shared_with_me) + len(shared_by_me),
    })
