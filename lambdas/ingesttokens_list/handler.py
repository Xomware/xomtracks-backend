"""
GET /ingest-tokens/list -- list the caller's OWN active ingest tokens (authed).
================================================================================
Self-serve onboarding "Connected" panel. A xomify-authed caller gets the
non-revoked ingest tokens they own -- one per device running their extractor --
so the UI can render a device list (label + last scan) and let them revoke any
of them from ANY browser/device (the previous UI could only revoke the token
whose non-secret hash the minting browser happened to remember in localStorage).

SCOPED to the caller's ownerId (their normalized email -- WS-AUTH): only ever
the caller's own tokens; no other user's devices are visible. NEVER returns a
plaintext token (the table holds only irreversible hashes anyway) or the
`revoked` flag (revoked rows are filtered out entirely).

Under admin impersonation (`?impersonate=<email>` honored by get_caller_owner),
this returns the TARGET's devices -- the admin's read-only "step through as
them" view. Write actions (revoke) stay gated on the frontend.

Timestamps are surfaced as Z-suffixed ISO 8601 (converted from the stored
epoch) -- the single timestamp shape the frontend renders.
"""

from typing import Any

from lambdas.common import ingest_tokens
from lambdas.common.errors import handle_errors
from lambdas.common.logger import get_logger
from lambdas.common.utility_helpers import epoch_to_iso, get_caller_owner, success_response

log = get_logger(__file__)

HANDLER = "ingesttokens_list"


@handle_errors(HANDLER)
def handler(event: dict, context: Any) -> dict:
    # Authed route -- 401 if the caller's xomify token is missing/invalid. The
    # verified email is the ownerId the listing is scoped to (or, for an admin,
    # the impersonated target).
    owner_id = get_caller_owner(event)

    tokens = ingest_tokens.list_active_tokens_for_owner(owner_id)

    devices = [
        {
            "tokenHash": t.get("tokenHash"),
            "label": t.get("label"),
            "createdAt": epoch_to_iso(t.get("createdAt")),
            "lastScanAt": epoch_to_iso(t.get("lastUsedAt")),
        }
        for t in tokens
    ]

    # Most-recently-active first so the device list reads naturally; devices that
    # have never scanned (lastScanAt is None) sort to the bottom.
    devices.sort(key=lambda d: d["lastScanAt"] or "", reverse=True)

    log.info(f"Listed {len(devices)} active ingest token(s) for owner={owner_id}")

    return success_response({"devices": devices})
