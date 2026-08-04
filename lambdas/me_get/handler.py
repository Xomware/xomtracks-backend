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

from lambdas.common import ingest_tokens
from lambdas.common.dynamo_helpers import SPOTIFY_REFRESH_TOKEN_ATTR, SPOTIFY_USER_ID_ATTR
from lambdas.common.errors import handle_errors
from lambdas.common.link_requests import has_pending_for_email
from lambdas.common.logger import get_logger
from lambdas.common.shares_dynamo import owner_has_shares, scan_shares_by_normalized_handles
from lambdas.common.user_links import LINKED_HANDLES_ATTR, get_user_record
from lambdas.common.utility_helpers import epoch_to_iso, get_caller_email, is_admin, success_response

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

    # Admin flag (WS6): true only for Dom (caller email == ADMIN_EMAIL). The
    # frontend uses it to HIDE the "set up your own" card (Dom is the global
    # baseline everyone already sees) and to gate the admin portal.
    caller_is_admin = is_admin(email)

    # Own-ingest flag (WS6): true if the caller runs their OWN extractor -- either
    # they hold a live ingest token OR they already own shares. Lets the frontend
    # hide the onboarding card for anyone who has self-served. Dom trivially owns
    # shares, so this is also True for him (and he's admin regardless).
    #
    # One scan of the caller's active tokens backs BOTH ownIngest and lastScanAt
    # (the onboarding "Connected" panel's "last scan" readout) -- max lastUsedAt
    # across those tokens, null until the first extractor push lands.
    active_tokens = ingest_tokens.list_active_tokens_for_owner(email)
    own_ingest = bool(active_tokens) or owner_has_shares(email)
    last_scan_epoch = max(
        (t["lastUsedAt"] for t in active_tokens if t.get("lastUsedAt")),
        default=None,
    )

    return success_response({
        "email": email,
        "linkStatus": link_status,
        "linked": bool(handles),
        "linkedHandles": sorted(handles),
        "shareCount": len(matched),
        "spotifyConnected": spotify_connected,
        "spotifyUserId": spotify_user_id,
        "isAdmin": caller_is_admin,
        "ownIngest": own_ingest,
        "lastScanAt": epoch_to_iso(last_scan_epoch),
    })
