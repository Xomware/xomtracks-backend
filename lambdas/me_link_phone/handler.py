"""
POST /me/link-phone -- link a phone number to the CALLER's Cognito identity
(authed, Cognito-gated).
========================================================================
Multi-user attribution: shares are keyed by the raw iMessage handle
(sharerHandle) while users sign in via Cognito (email). This endpoint links a
signed-in group member's phone number to their Cognito identity so they can
see + be attributed for THEIR own shares.

Flow:
  1. Resolve the caller's identity from the Cognito authorizer claims
     (email is required; sub is stored too when present).
  2. Normalize the submitted phone number to last-10 digits (phone.normalize_
     phone -- the same rule the extractor uses on Contacts/handles).
  3. Additively link the handle to the caller (user_links.link_phone; ADD to a
     String Set -- idempotent re-link, appends a second number).
  4. TRUST-BASED VERIFICATION: we link unconditionally, then report how many
     existing shares already carry this handle as their sharerHandle, so the
     UI can say "Linked -- found N of your shares." A count of 0 still links,
     but is flagged so the UI can say "no shares found yet."

     HARDENING OPTION (not built): SMS-OTP verification via SNS / a 10DLC
     number -- send a one-time code to the submitted number and require the
     caller to echo it back before the link is written. That proves possession
     of the number; the trust-based default assumes a bounded friend group and
     is enough for MVP. Flip to OTP here if the trust boundary widens.
"""

from typing import Any

from pydantic import ValidationError as PydanticValidationError

from lambdas.common.errors import ValidationError, handle_errors
from lambdas.common.logger import get_logger
from lambdas.common.models import LinkPhoneRequest
from lambdas.common.phone import normalize_phone
from lambdas.common.shares_dynamo import scan_shares_by_normalized_handles
from lambdas.common.user_links import link_phone
from lambdas.common.utility_helpers import get_caller_email, get_caller_sub, parse_body, success_response

log = get_logger(__file__)

HANDLER = "me_link_phone"


@handle_errors(HANDLER)
def handler(event: dict, context: Any) -> dict:
    # Authed route -- 401 if the Cognito authorizer context is absent.
    email = get_caller_email(event)
    sub = get_caller_sub(event)

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

    linked_handles = link_phone(email, handle, sub)

    # Trust-based verification signal: how many shares we already see under
    # this handle. Zero is a valid link, just flagged for the UI.
    matched = scan_shares_by_normalized_handles({handle})
    count = len(matched)

    log.info(f"Linked handle for {email}: {handle} ({count} matched shares)")

    return success_response({
        "handle": handle,
        "linkedHandles": sorted(linked_handles),
        "matchedShareCount": count,
        "flagged": count == 0,
    })
