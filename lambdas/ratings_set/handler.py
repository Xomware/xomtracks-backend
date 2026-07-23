"""
POST /ratings/set -- upsert the CALLER's 1-5 rating for a song (authed,
Cognito-gated). Whole-group model: any logged-in Xomware member may rate any
song; the rating is keyed by the song's normalized trackKey and the caller's
Cognito email, so it follows the SONG across all of its share instances and a
member has exactly one rating per song (re-rating overwrites).

Body: {"trackKey": "<song key>", "rating": 1..5}
Returns: {"trackKey": ..., "rating": {"avg", "count", "myRating"}} -- the fresh
aggregate (already including this write) plus the caller's own rating.

ROUTE NOTE: exposed as POST /ratings/set (not POST /ratings) because the
api-gateway-service module supports exactly two path levels -- same constraint
that made GET /shares into GET /shares/list. The handler reads the Cognito
authorizer context + body only, not the URL path.
"""

from typing import Any

from pydantic import ValidationError as PydanticValidationError

from lambdas.common.errors import ValidationError, handle_errors
from lambdas.common.logger import get_logger
from lambdas.common.models import SetRatingRequest
from lambdas.common.ratings_dynamo import set_rating
from lambdas.common.utility_helpers import get_caller_email, parse_body, success_response

log = get_logger(__file__)

HANDLER = "ratings_set"


@handle_errors(HANDLER)
def handler(event: dict, context: Any) -> dict:
    # Authed route -- 401 if the Cognito authorizer context is absent.
    email = get_caller_email(event)

    body = parse_body(event)
    try:
        req = SetRatingRequest(**body)
    except PydanticValidationError as err:
        raise ValidationError(
            message=f"Invalid rating payload: {err}",
            handler=HANDLER,
            function="handler",
            field="rating",
        ) from err

    aggregate = set_rating(req.trackKey, email, req.rating)
    log.info(f"Rating set by {email} for {req.trackKey}: {req.rating} (count={aggregate['count']})")

    return success_response({"trackKey": req.trackKey, "rating": aggregate})
