"""
GET /admin/calls?windowDays=<n>&recentLimit=<m> -- calls & errors dashboard
(admin-gated).
========================================================================
Aggregates the TTL'd xomtracks-request-log (written fail-open by the shared
request_log hook on every authed request) over a trailing window:
  {windowDays, totalCalls, errorCount, byPath[], byStatus{}, recentErrors[]}

Gated in-handler to the admin (Dom). See docs/features/xomtracks-xomify-merge/
PLAN.md WS6.
"""

from typing import Any

from lambdas.common.errors import handle_errors
from lambdas.common.logger import get_logger
from lambdas.common.request_log import aggregate
from lambdas.common.utility_helpers import (
    get_query_params,
    require_admin,
    success_response,
)

log = get_logger(__file__)

HANDLER = "admin_calls"

_DEFAULT_WINDOW_DAYS = 7
_MAX_WINDOW_DAYS = 30
_DEFAULT_RECENT_LIMIT = 25
_MAX_RECENT_LIMIT = 100


def _clamp_int(value, default: int, lo: int, hi: int) -> int:
    try:
        n = int(value)
    except (TypeError, ValueError):
        return default
    return max(lo, min(hi, n))


@handle_errors(HANDLER)
def handler(event: dict, context: Any) -> dict:
    # 401 if not signed in, 403 if signed in but not the admin.
    require_admin(event)

    params = get_query_params(event)
    window_days = _clamp_int(params.get("windowDays"), _DEFAULT_WINDOW_DAYS, 1, _MAX_WINDOW_DAYS)
    recent_limit = _clamp_int(params.get("recentLimit"), _DEFAULT_RECENT_LIMIT, 1, _MAX_RECENT_LIMIT)

    summary = aggregate(window_days=window_days, recent_limit=recent_limit)
    log.info(
        f"Admin calls dashboard: {summary['totalCalls']} calls / "
        f"{summary['errorCount']} errors over {window_days}d"
    )

    return success_response(summary)
