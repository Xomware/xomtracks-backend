"""
GET /me -- the caller's linked phone handle(s) + a count of shares attributed
to them (authed, Cognito-gated).
========================================================================
Backs the "Link your number" UI: lets a signed-in member see whether they've
linked a number and how many of their shares the app can attribute to them.
Returns linked=false with an empty handle list for a member who hasn't linked
yet, so the UI can show the link prompt.
"""

from typing import Any

from lambdas.common.errors import handle_errors
from lambdas.common.logger import get_logger
from lambdas.common.shares_dynamo import scan_shares_by_normalized_handles
from lambdas.common.user_links import get_linked_handles
from lambdas.common.utility_helpers import get_caller_email, success_response

log = get_logger(__file__)

HANDLER = "me_get"


@handle_errors(HANDLER)
def handler(event: dict, context: Any) -> dict:
    # Authed route -- 401 if the Cognito authorizer context is absent.
    email = get_caller_email(event)

    handles = get_linked_handles(email)
    matched = scan_shares_by_normalized_handles(handles) if handles else []

    return success_response({
        "email": email,
        "linked": bool(handles),
        "linkedHandles": sorted(handles),
        "shareCount": len(matched),
    })
