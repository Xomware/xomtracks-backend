"""
GET /admin/tokens -- ingest-token management, grouped by owner (admin-gated).
========================================================================
Lists every per-user extractor ingest token from xomtracks-ingest-tokens as
METADATA only -- NEVER the plaintext (the table holds only irreversible hashes).
Each entry: {ownerEmail, tokenHash, label, createdAt, lastUsedAt, revoked}.
Returned both as a flat list and grouped by ownerEmail for the portal UI.

Gated in-handler to the admin (Dom). See docs/features/xomtracks-xomify-merge/
PLAN.md WS6.
"""

from typing import Any

from lambdas.common.dynamo_helpers import list_spotify_connected_users
from lambdas.common.errors import handle_errors
from lambdas.common.ingest_tokens import list_all_tokens
from lambdas.common.logger import get_logger
from lambdas.common.utility_helpers import require_admin, success_response

log = get_logger(__file__)

HANDLER = "admin_tokens"


@handle_errors(HANDLER)
def handler(event: dict, context: Any) -> dict:
    # 401 if not signed in, 403 if signed in but not the admin.
    require_admin(event)

    tokens = list_all_tokens()

    by_owner: dict[str, list[dict]] = {}
    for t in tokens:
        owner = t.get("ownerEmail") or "unknown"
        by_owner.setdefault(owner, []).append(t)

    # Which token-owners are Spotify-connected in xomtracks -- i.e. the rolling
    # cron can build their OWN playlists. An owner with a token but NO connection
    # ingests shares but gets no own playlists, so the Extractor-status view
    # flags it. One scan; matched by email (== ownerId under WS-AUTH). Best-effort:
    # this enrichment must never break the (primary) token list.
    try:
        connected = {u.get("email") for u in list_spotify_connected_users() if u.get("email")}
        spotify_connected_owners = sorted(owner for owner in by_owner if owner in connected)
    except Exception as err:  # noqa: BLE001 -- secondary enrichment, degrade gracefully
        log.warning(f"admin_tokens: spotify-connected enrichment failed: {err}")
        spotify_connected_owners = []

    log.info(f"Admin listed {len(tokens)} ingest token(s) across {len(by_owner)} owner(s)")

    return success_response({
        "tokens": tokens,
        "byOwner": by_owner,
        "count": len(tokens),
        "spotifyConnectedOwners": spotify_connected_owners,
    })
