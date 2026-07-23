"""
POST /heard/set -- upsert the CALLER's heard flag for a song (authed,
Cognito-gated). Whole-group listen model: any logged-in Xomware member may mark
any song heard/unheard; the flag is keyed by the song's normalized trackKey and
the caller's Cognito email, so it follows the SONG across all of its share
instances and a member has exactly one heard row per song (re-setting overwrites).

Body: {"trackKey": "<song key>", "heard": true|false}
Returns: {"trackKey": ..., "heard": bool, "heardAt": <epoch|null>} -- the fresh
per-caller heard state (heardAt is the "when heard" time, present only while
heard is True).

ROUTE NOTE: exposed as POST /heard/set (not POST /heard) because the
api-gateway-service module supports exactly two path levels -- same constraint
that made GET /shares into GET /shares/list and POST /ratings into
POST /ratings/set. The handler reads the Cognito authorizer context + body only,
not the URL path.
"""

from typing import Any

from pydantic import ValidationError as PydanticValidationError

from lambdas.common.errors import ValidationError, handle_errors
from lambdas.common.heard_dynamo import set_heard
from lambdas.common.logger import get_logger
from lambdas.common.models import SetHeardRequest
from lambdas.common.utility_helpers import get_caller_email, parse_body, success_response

log = get_logger(__file__)

HANDLER = "heard_set"


@handle_errors(HANDLER)
def handler(event: dict, context: Any) -> dict:
    # Authed route -- 401 if the Cognito authorizer context is absent.
    email = get_caller_email(event)

    body = parse_body(event)
    try:
        req = SetHeardRequest(**body)
    except PydanticValidationError as err:
        raise ValidationError(
            message=f"Invalid heard payload: {err}",
            handler=HANDLER,
            function="handler",
            field="heard",
        ) from err

    result = set_heard(req.trackKey, email, req.heard)
    log.info(f"Heard set by {email} for {req.trackKey}: {req.heard}")

    return success_response(result)
