"""
GET /me/get -- the caller's phone-link STATE + a count of shares attributed to
them (authed, Cognito-gated).
========================================================================
Backs the "Link your number" UI under the admin-approval model. Reports
linkStatus so the frontend can show the right state:

  - "none"    -- no link and no pending request; show the link prompt.
  - "pending" -- the caller has a request awaiting the admin's decision; show
                 "waiting for approval".
  - "linked"  -- the admin approved; the caller is linked. linkedHandles +
                 shareCount are populated so the UI can say "N of your shares".

`linked` (bool) is kept for backwards compatibility (== linkStatus == "linked").
"""

from typing import Any

from lambdas.common.dynamo_helpers import SPOTIFY_REFRESH_TOKEN_ATTR, SPOTIFY_USER_ID_ATTR
from lambdas.common.errors import handle_errors
from lambdas.common.link_requests import has_pending_for_email
from lambdas.common.logger import get_logger
from lambdas.common.shares_dynamo import scan_shares_by_normalized_handles
from lambdas.common.user_links import LINKED_HANDLES_ATTR, get_user_record
from lambdas.common.utility_helpers import get_caller_email, success_response

log = get_logger(__file__)

HANDLER = "me_get"


@handle_errors(HANDLER)
def handler(event: dict, context: Any) -> dict:
    # Authed route -- 401 if the Cognito authorizer context is absent.
    email = get_caller_email(event)

    # One read of the caller's own row backs BOTH link state and the Spotify
    # connection flag (the Phase 2 per-user OAuth writes refreshToken/spotifyUserId
    # onto this same row).
    record = get_user_record(email) or {}
    linked_handles = record.get(LINKED_HANDLES_ATTR)
    handles = set(linked_handles) if linked_handles else set()

    if handles:
        link_status = "linked"
        matched = scan_shares_by_normalized_handles(handles)
    elif has_pending_for_email(email):
        link_status = "pending"
        matched = []
    else:
        link_status = "none"
        matched = []

    # Spotify connection state -- authoritative across devices (the frontend
    # prefers this over its local flag). Degrades to False when no connection row
    # / refreshToken. The refreshToken itself is NEVER surfaced.
    spotify_connected = bool(record.get(SPOTIFY_REFRESH_TOKEN_ATTR))
    spotify_user_id = record.get(SPOTIFY_USER_ID_ATTR) if spotify_connected else None

    return success_response({
        "email": email,
        "linkStatus": link_status,
        "linked": bool(handles),
        "linkedHandles": sorted(handles),
        "shareCount": len(matched),
        "spotifyConnected": spotify_connected,
        "spotifyUserId": spotify_user_id,
    })
