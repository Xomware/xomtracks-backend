"""
GET /admin/requests -- list PENDING phone-link requests (admin-gated).
========================================================================
Backs the admin portal's approval queue. Cognito-authed at the API Gateway
layer AND gated in-handler to the single admin (Dom): a caller whose Cognito
email != ADMIN_EMAIL is 403'd (utility_helpers.require_admin). Returns the
pending requests oldest-first so the admin works the queue FIFO.
"""

from typing import Any

from lambdas.common.errors import handle_errors
from lambdas.common.link_requests import list_pending
from lambdas.common.logger import get_logger
from lambdas.common.utility_helpers import require_admin, success_response

log = get_logger(__file__)

HANDLER = "admin_requests"


@handle_errors(HANDLER)
def handler(event: dict, context: Any) -> dict:
    # 401 if not signed in, 403 if signed in but not the admin.
    require_admin(event)

    pending = list_pending()
    requests = [
        {
            "requestId": r.get("requestId"),
            "requesterEmail": r.get("requesterEmail"),
            "phone": r.get("phone"),
            "savedName": r.get("savedName"),
            "createdAt": r.get("createdAt"),
        }
        for r in pending
    ]

    log.info(f"Admin listed {len(requests)} pending link request(s)")

    return success_response({"requests": requests})
