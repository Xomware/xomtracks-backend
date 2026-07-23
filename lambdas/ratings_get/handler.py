"""
GET /ratings/get?trackKeys=a,b,c -- batch-fetch aggregate ratings for a set of
songs plus the CALLER's own rating for each (authed, Cognito-gated).

Query: `trackKeys` = comma-separated list of normalized song keys.
Returns: {"ratings": {"<trackKey>": {"avg", "count", "myRating"}, ...}} -- a map
keyed by trackKey for O(1) frontend lookup. Unrated keys still appear, with the
empty aggregate ({avg:0, count:0, myRating:null}), so the client can render
every requested key without a missing-key check.

Batch endpoint so a browse UI can hydrate a screen of cards in ONE call rather
than N. (The feed's own /shares/list already carries `rating` inline; this
endpoint covers cases where the client holds trackKeys without the shares --
e.g. a playlist/detail view, or refreshing a single card after a rate.)

ROUTE NOTE: GET /ratings/get, not GET /ratings -- the api-gateway-service module
supports exactly two path levels (same constraint as GET /shares/list).
"""

from typing import Any

from lambdas.common.errors import ValidationError, handle_errors
from lambdas.common.logger import get_logger
from lambdas.common.ratings_dynamo import EMPTY_AGGREGATE, batch_ratings_for_track_keys
from lambdas.common.utility_helpers import get_caller_email, get_query_params, success_response

log = get_logger(__file__)

HANDLER = "ratings_get"

# Guardrail: one BatchGetItem-scale page. Well above a real feed screen; keeps a
# malformed/abusive query from fanning out into an unbounded number of queries.
MAX_TRACK_KEYS = 200


@handle_errors(HANDLER)
def handler(event: dict, context: Any) -> dict:
    # Authed route -- 401 if the Cognito authorizer context is absent.
    email = get_caller_email(event)

    params = get_query_params(event)
    raw = params.get("trackKeys", "") or ""
    track_keys = [k.strip() for k in raw.split(",") if k.strip()]

    if not track_keys:
        raise ValidationError(
            message="trackKeys is required (comma-separated list of track keys)",
            handler=HANDLER,
            function="handler",
            field="trackKeys",
        )

    if len(track_keys) > MAX_TRACK_KEYS:
        raise ValidationError(
            message=f"trackKeys is limited to {MAX_TRACK_KEYS} per request",
            handler=HANDLER,
            function="handler",
            field="trackKeys",
        )

    aggregates = batch_ratings_for_track_keys(set(track_keys), email)

    # Include every requested key (preserving request order intent via the map),
    # filling unrated keys with the empty aggregate so the client never misses one.
    ratings = {key: aggregates.get(key, dict(EMPTY_AGGREGATE)) for key in track_keys}

    return success_response({"ratings": ratings, "count": len(ratings)})
