"""
POST /me/link-phone -- request to link a phone number to the CALLER's Cognito
identity (authed, Cognito-gated). ADMIN-APPROVAL model.
========================================================================
Multi-user attribution: shares are keyed by the raw iMessage handle
(sharerHandle) while users sign in via Cognito (email). Linking a member's phone
number to their Cognito identity is what lets them see + be attributed for THEIR
own shares.

This endpoint NO LONGER links immediately. The old trust-based auto-link is
replaced by an admin-approval gate: any group member CAN request a link, but only
the admin (Dom) can grant it -- so a member can't attribute someone else's
number to themselves.

Flow:
  1. Resolve the caller's identity (normalized email) from the verified xomify
     token (WS-AUTH).
  2. Normalize the submitted phone number to last-10 digits (phone.normalize_
     phone -- the same rule the extractor uses on Contacts/handles).
  3. Resolve the SAVED NAME for that number: look up any share whose normalized
     sharerHandle matches and use its sharerName (None if that number has no
     shares). This is the contact name Dom has for the number -- surfaced to the
     admin so they can recognize who's asking.
  4. Create a PENDING request (link_requests.create_request) -- NOT a link.
  5. Email the admin (best-effort, SES) so they can approve/deny in the admin
     portal.
  6. Return {status:"pending", requestId}. The actual link is written only when
     the admin approves via POST /admin/approve.
"""

from typing import Any

from pydantic import ValidationError as PydanticValidationError

from lambdas.common import email_notify
from lambdas.common.errors import ValidationError, handle_errors
from lambdas.common.link_requests import create_request
from lambdas.common.logger import get_logger
from lambdas.common.models import LinkPhoneRequest
from lambdas.common.phone import normalize_phone
from lambdas.common.shares_dynamo import scan_shares_by_normalized_handles
from lambdas.common.utility_helpers import get_caller_owner, parse_body, success_response

log = get_logger(__file__)

HANDLER = "me_link_phone"


def _resolve_saved_name(handle: str) -> str | None:
    """Dom's saved contact name for a number, taken from any share whose
    normalized sharerHandle matches. None if the number has no shares (or no
    share carries a name)."""
    matched = scan_shares_by_normalized_handles({handle})
    for share in matched:
        name = share.get("sharerName")
        if name:
            return name
    return None


@handle_errors(HANDLER)
def handler(event: dict, context: Any) -> dict:
    # Authed route -- 401 if the caller's xomify token is missing/invalid.
    # Under WS-AUTH the caller's normalized email IS their durable identity
    # (there is no separate Cognito sub any more), so `sub` is left None on the
    # request row -- attribution keys on requesterEmail.
    email = get_caller_owner(event)
    sub = None

    body = parse_body(event)
    try:
        req = LinkPhoneRequest(**body)
    except PydanticValidationError as err:
        raise ValidationError(
            message=f"Invalid link-phone payload: {err}",
            handler=HANDLER,
            function="handler",
            field="phoneNumber",
        ) from err

    handle = normalize_phone(req.phoneNumber)
    if not handle:
        raise ValidationError(
            message="phoneNumber must contain a usable set of digits",
            handler=HANDLER,
            function="handler",
            field="phoneNumber",
        )

    saved_name = _resolve_saved_name(handle)

    request = create_request(
        requester_email=email,
        phone=handle,
        saved_name=saved_name,
        sub=sub,
    )

    # Best-effort admin notification -- a send failure does not fail the request
    # (it's already stored and visible in the admin portal).
    email_notify.send_link_request_notification(
        requester_email=email,
        phone=handle,
        saved_name=saved_name,
    )

    log.info(f"Link request pending for {email}: {handle} (saved name: {saved_name or 'unknown'})")

    return success_response({
        "status": request["status"],
        "requestId": request["requestId"],
    })
