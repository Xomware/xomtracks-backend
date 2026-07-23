"""
POST /admin/approve -- approve a pending phone-link request (admin-gated).
========================================================================
Approving creates the ACTUAL link: it adds the request's normalized phone handle
to the requester's linkedHandles via the existing user_links.link_phone logic
(carrying the requester's Cognito sub if one was captured), then marks the
request approved. Only a PENDING request can be approved.

Cognito-authed at the API Gateway layer AND gated in-handler to the single admin
(Dom) -- a caller whose Cognito email != ADMIN_EMAIL is 403'd.
"""

from typing import Any

from pydantic import ValidationError as PydanticValidationError

from lambdas.common.errors import NotFoundError, ValidationError, handle_errors
from lambdas.common.link_requests import (
    STATUS_APPROVED,
    STATUS_PENDING,
    get_request,
    set_status,
)
from lambdas.common.logger import get_logger
from lambdas.common.models import AdminRequestDecision
from lambdas.common.user_links import link_phone
from lambdas.common.utility_helpers import parse_body, require_admin, success_response

log = get_logger(__file__)

HANDLER = "admin_approve"


@handle_errors(HANDLER)
def handler(event: dict, context: Any) -> dict:
    # 401 if not signed in, 403 if signed in but not the admin.
    require_admin(event)

    body = parse_body(event)
    try:
        decision = AdminRequestDecision(**body)
    except PydanticValidationError as err:
        raise ValidationError(
            message=f"Invalid approve payload: {err}",
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

    # Create the real link -- additive ADD to the requester's linkedHandles set.
    linked_handles = link_phone(
        request["requesterEmail"],
        request["phone"],
        request.get("sub"),
    )

    updated = set_status(decision.requestId, STATUS_APPROVED)

    log.info(
        f"Approved link request {decision.requestId}: linked {request['phone']} "
        f"to {request['requesterEmail']}"
    )

    return success_response({
        "requestId": decision.requestId,
        "status": updated.get("status", STATUS_APPROVED),
        "requesterEmail": request["requesterEmail"],
        "linkedHandles": sorted(linked_handles),
    })
