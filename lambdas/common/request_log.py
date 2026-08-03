"""
XOMTRACKS Request Log (admin portal WS6)
========================================
Lightweight, TTL'd per-request telemetry backing the admin portal's calls &
errors dashboard. Preferred over raw CloudWatch because it gives per-user (email)
granularity and a trivial aggregation surface at friend-group scale.

Write path: `record_from_event(event, response, error=None)` is called centrally
from errors.handle_errors AFTER the final response is built (both success and
error paths), so it always sees the real path + method + final status + caller
email. It is FAIL-OPEN and low-overhead:
  - NO-OP when REQUEST_LOG_TABLE_NAME is unset (unconfigured/local/test) or when
    the event is not an HTTP request (e.g. an EventBridge cron -- no path/method).
  - Every item is run through errors.mask_sensitive_data before write, so a token
    value / secret can never land in the log even if one appears in a message.
  - Any failure is swallowed -- telemetry must never break a request.

Read path: `aggregate(window_days, recent_limit)` scans the (TTL-bounded, low-
volume) table and returns per-path counts, per-status counts, an error count, and
the most-recent N errors. A filtered Scan is the right tool here (same rationale
as the other scans in this codebase); a GSI is a documented fast-follow if volume
ever grows.

Item shape (xomtracks-request-log):
  PK id (uuid4); ts (ISO8601 Z); tsEpoch (int, for window filtering); path;
  method; status (int); email (caller, or absent); error (message, on failure);
  expiresAt (epoch TTL attribute = now + REQUEST_LOG_TTL_DAYS days).
"""

import time
import uuid
from datetime import datetime, timezone

import boto3

from lambdas.common.constants import AWS_DEFAULT_REGION, REQUEST_LOG_TTL_DAYS
from lambdas.common.logger import get_logger

log = get_logger(__file__)

# Private key used to stash the resolved caller email on the event during
# get_caller_owner, so record_from_event reads it without re-verifying the token.
CALLER_EMAIL_EVENT_KEY = "_xt_caller_email"

# Private key used to stash the IMPERSONATED target email on the event when the
# admin-only impersonation override is active (see utility_helpers.get_caller_
# owner). Recorded alongside the real caller so impersonation is auditable.
IMPERSONATED_EMAIL_EVENT_KEY = "_xt_impersonated_email"

_dynamodb = None


def _get_dynamodb():
    """Lazily create + cache the DynamoDB resource on first use (test-friendly)."""
    global _dynamodb
    if _dynamodb is None:
        _dynamodb = boto3.resource("dynamodb", region_name=AWS_DEFAULT_REGION)
    return _dynamodb


def _table_name() -> str:
    # Read the constant LIVE via the module (not the import-time snapshot) so it
    # reflects env at runtime and tests can monkeypatch it.
    from lambdas.common import constants

    return constants.REQUEST_LOG_TABLE_NAME


def _table():
    return _get_dynamodb().Table(_table_name())


def _extract_method(event: dict) -> str | None:
    return event.get("httpMethod") or (
        event.get("requestContext", {}).get("http", {}).get("method")
    )


def _extract_path(event: dict) -> str | None:
    return event.get("path") or event.get("rawPath") or (
        event.get("requestContext", {}).get("resourcePath")
    )


def record(
    path: str,
    method: str,
    status: int,
    email: str | None = None,
    error: str | None = None,
    impersonating: str | None = None,
) -> None:
    """
    Persist one request-log item. FAIL-OPEN + NO-OP when unconfigured. Masks the
    item so no secret/token value is ever written.

    When the admin-only impersonation override is active, `email` is the REAL
    admin caller and `impersonating` is the impersonated target -- both are
    recorded so the action is auditable.
    """
    if not _table_name():
        return
    try:
        from lambdas.common.errors import mask_sensitive_data

        now = int(time.time())
        item = {
            "id": str(uuid.uuid4()),
            "ts": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
            "tsEpoch": now,
            "path": path,
            "method": method,
            "status": int(status),
            "expiresAt": now + REQUEST_LOG_TTL_DAYS * 86400,
        }
        if email:
            item["email"] = email
        if error:
            item["error"] = error
        if impersonating:
            item["impersonating"] = impersonating
        # Defense-in-depth: strip anything that looks sensitive before write.
        item = mask_sensitive_data(item)
        _table().put_item(Item=item)
    except Exception as err:  # noqa: BLE001 -- fail-open: telemetry never breaks a request
        log.warning("request_log.record skipped: %s", err.__class__.__name__)


def record_from_event(event: dict, response: dict, error: str | None = None) -> None:
    """
    Record a request's outcome from its API Gateway event + final response.
    NO-OP for non-HTTP events (no method/path -- e.g. EventBridge crons) and when
    the log table is unconfigured. FAIL-OPEN throughout.
    """
    if not _table_name() or not isinstance(event, dict):
        return
    method = _extract_method(event)
    path = _extract_path(event)
    if not method or not path:
        # Not an HTTP request (cron / direct invoke) -- nothing to log here.
        return
    try:
        status = int(response.get("statusCode", 0)) if isinstance(response, dict) else 0
    except (TypeError, ValueError):
        status = 0
    email = event.get(CALLER_EMAIL_EVENT_KEY)
    impersonating = event.get(IMPERSONATED_EMAIL_EVENT_KEY)
    record(
        path=path,
        method=method,
        status=status,
        email=email,
        error=error,
        impersonating=impersonating,
    )


# ============================================
# Read path -- dashboard aggregation
# ============================================

def _scan_recent(since_epoch: int) -> list[dict]:
    """All request-log items with tsEpoch >= since_epoch (paginated Scan)."""
    from boto3.dynamodb.conditions import Attr

    table = _table()
    items: list[dict] = []
    kwargs = {"FilterExpression": Attr("tsEpoch").gte(since_epoch)}
    while True:
        res = table.scan(**kwargs)
        items.extend(res.get("Items", []))
        last_key = res.get("LastEvaluatedKey")
        if not last_key:
            break
        kwargs["ExclusiveStartKey"] = last_key
    return items


def aggregate(window_days: int = 7, recent_limit: int = 25) -> dict:
    """
    Aggregate the request log over the trailing `window_days`:
      {
        windowDays, totalCalls, errorCount,
        byPath:   [{path, count, errors}, ...]   (desc by count),
        byStatus: {"200": n, "404": n, ...},
        recentErrors: [{id, ts, path, method, status, email, error}, ...]
                      (newest first, capped at recent_limit)
      }
    Empty/zeroed result when the table is unconfigured.
    """
    if not _table_name():
        return {
            "windowDays": window_days,
            "totalCalls": 0,
            "errorCount": 0,
            "byPath": [],
            "byStatus": {},
            "recentErrors": [],
        }

    since = int(time.time()) - window_days * 86400
    items = _scan_recent(since)

    by_path: dict[str, dict] = {}
    by_status: dict[str, int] = {}
    errors: list[dict] = []

    for it in items:
        try:
            status = int(it.get("status", 0))
        except (TypeError, ValueError):
            status = 0
        path = it.get("path") or "unknown"

        bucket = by_path.setdefault(path, {"path": path, "count": 0, "errors": 0})
        bucket["count"] += 1

        skey = str(status)
        by_status[skey] = by_status.get(skey, 0) + 1

        is_error = status >= 400 or bool(it.get("error"))
        if is_error:
            bucket["errors"] += 1
            errors.append({
                "id": it.get("id"),
                "ts": it.get("ts"),
                "tsEpoch": it.get("tsEpoch"),
                "path": path,
                "method": it.get("method"),
                "status": status,
                "email": it.get("email"),
                "error": it.get("error"),
            })

    errors.sort(key=lambda e: e.get("tsEpoch") or 0, reverse=True)
    by_path_list = sorted(by_path.values(), key=lambda b: b["count"], reverse=True)

    # Drop the internal sort key from the returned recent errors.
    recent = [{k: v for k, v in e.items() if k != "tsEpoch"} for e in errors[:recent_limit]]

    return {
        "windowDays": window_days,
        "totalCalls": len(items),
        "errorCount": len(errors),
        "byPath": by_path_list,
        "byStatus": by_status,
        "recentErrors": recent,
    }
