"""
GET /admin/users -- the user directory (admin-gated).
========================================================================
Lists everyone who has signed into Xomtracks -- materialized from the
xomtracks-users table by the auto-upsert hook (user_directory.record_seen, fired
on every authed request via get_caller_owner). Each row:
  {email, firstSeen, lastSeen, ownIngest, spotifyConnected}

Gated in-handler to the single admin (Dom): a caller whose email != ADMIN_EMAIL
is 403'd (401 if unauthenticated). See docs/features/xomtracks-xomify-merge/
PLAN.md WS6.
"""

from typing import Any

from lambdas.common.errors import handle_errors
from lambdas.common.logger import get_logger
from lambdas.common.user_directory import list_directory
from lambdas.common.utility_helpers import require_admin, success_response

log = get_logger(__file__)

HANDLER = "admin_users"


@handle_errors(HANDLER)
def handler(event: dict, context: Any) -> dict:
    # 401 if not signed in, 403 if signed in but not the admin.
    require_admin(event)

    users = list_directory()
    log.info(f"Admin listed {len(users)} directory user(s)")

    return success_response({"users": users, "count": len(users)})
