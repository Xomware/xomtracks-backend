"""
POST /admin/deny -- deny a pending phone-link request (admin-gated).
========================================================================
Denying marks the request denied and creates NO link. Only a PENDING request can
be denied.

Cognito-authed at the API Gateway layer AND gated in-handler to the single admin
(Dom) -- a caller whose Cognito email != ADMIN_EMAIL is 403'd.
"""

from typing import Any

from pydantic import ValidationError as PydanticValidationError

from lambdas.common.errors import NotFoundError, ValidationError, handle_errors
from lambdas.common.link_requests import (
    STATUS_DENIED,
    STATUS_PENDING,
    get_request,
    set_status,
)
from lambdas.common.logger import get_logger
from lambdas.common.models import AdminRequestDecision
from lambdas.common.utility_helpers import parse_body, require_admin, success_response

log = get_logger(__file__)

HANDLER = "admin_deny"


@handle_errors(HANDLER)
def handler(event: dict, context: Any) -> dict:
    # 401 if not signed in, 403 if signed in but not the admin.
    require_admin(event)

    body = parse_body(event)
    try:
        decision = AdminRequestDecision(**body)
    except PydanticValidationError as err:
        raise ValidationError(
            message=f"Invalid deny payload: {err}",
            handler=HANDLER,
            function="handler",
            field="requestId",
        ) from err

    request = get_request(decision.requestId)
    if request is None:
        raise NotFoundError(
            message=f"Link request not found: {decision.requestId}",
            handler=HANDLER,
            function="handler",
            resource="linkRequest",
        )

    if request.get("status") != STATUS_PENDING:
        raise ValidationError(
            message=f"Request is not pending (status: {request.get('status')})",
            handler=HANDLER,
            function="handler",
            field="requestId",
        )

    updated = set_status(decision.requestId, STATUS_DENIED)

    log.info(f"Denied link request {decision.requestId} for {request['requesterEmail']}")

    return success_response({
        "requestId": decision.requestId,
        "status": updated.get("status", STATUS_DENIED),
        "requesterEmail": request["requesterEmail"],
    })
