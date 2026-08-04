"""
GET /admin/runs -- recent extractor run summaries, grouped by owner (admin-gated).
==================================================================================
Backs the admin Extractor-status "recent runs" feed. For each owner that holds
an ingest token, returns their most-recent runs (compact:
runAt/scanned/ingested/newWatermark/durationMs). Per-owner Query with a small
limit -- scales with owners (friend-group scale), not total runs.

Gated in-handler to the admin (Dom).
"""

from typing import Any

from lambdas.common import ingest_runs
from lambdas.common.errors import handle_errors
from lambdas.common.ingest_tokens import list_all_tokens
from lambdas.common.logger import get_logger
from lambdas.common.utility_helpers import require_admin, success_response

log = get_logger(__file__)

HANDLER = "admin_runs"

# Recent runs per owner in the feed -- enough to see cadence + freshness without
# pulling the whole (TTL'd) history.
_PER_OWNER_LIMIT = 20


@handle_errors(HANDLER)
def handler(event: dict, context: Any) -> dict:
    # 401 if not signed in, 403 if signed in but not the admin.
    require_admin(event)

    # Owners that have an extractor set up (any non-revoked-or-revoked token) --
    # the set worth showing runs for. Distinct, so we query each once.
    owners = sorted({t.get("ownerEmail") for t in list_all_tokens() if t.get("ownerEmail")})

    by_owner: dict[str, list[dict]] = {}
    for owner in owners:
        runs = ingest_runs.list_runs_for_owner(owner, limit=_PER_OWNER_LIMIT)
        if runs:
            by_owner[owner] = runs

    total = sum(len(r) for r in by_owner.values())
    log.info(f"Admin listed {total} recent run(s) across {len(by_owner)} owner(s)")

    return success_response({"byOwner": by_owner, "ownerCount": len(by_owner)})
