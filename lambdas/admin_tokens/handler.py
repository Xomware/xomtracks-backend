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

    log.info(f"Admin listed {len(tokens)} ingest token(s) across {len(by_owner)} owner(s)")

    return success_response({
        "tokens": tokens,
        "byOwner": by_owner,
        "count": len(tokens),
    })
