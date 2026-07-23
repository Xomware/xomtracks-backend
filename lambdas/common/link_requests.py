"""
XOMTRACKS Phone-Link Request Store
==================================
Pending phone-link requests under the ADMIN-APPROVAL model (replaces the old
trust-based auto-link). A signed-in member's POST /me/link-phone no longer writes
a link -- it creates a PENDING row here; the admin (Dom) then approves or denies
it via the /admin/* routes. Approval is what finally writes the real link (via
user_links.link_phone); denial writes nothing.

Table: xomtracks-link-requests (LINK_REQUESTS_TABLE_NAME)
  PK requestId (uuid4)
  attrs: requesterEmail (Cognito caller email), phone (NORMALIZED last-10 digits,
         see phone.normalize_phone), savedName (Dom's saved contact name for that
         number, or None), sub (Cognito sub, optional), status
         ("pending"|"approved"|"denied"), createdAt (epoch), updatedAt (epoch).

Reads are filtered Scans (list pending / has-pending-for-email). At friend-group
scale a Scan is the right tool -- the same rationale used for
shares_dynamo.scan_shares_by_match_status. A status GSI is the documented
fast-follow if request volume ever grows.
"""

import time
import uuid

import boto3
from boto3.dynamodb.conditions import Attr

from lambdas.common.constants import AWS_DEFAULT_REGION, LINK_REQUESTS_TABLE_NAME
from lambdas.common.errors import DynamoDBError, ValidationError
from lambdas.common.logger import get_logger

log = get_logger(__file__)

_dynamodb = None


def _get_dynamodb():
    """
    Lazily create (and cache) the DynamoDB resource on FIRST USE rather than at
    import time. Deferring construction until a function actually runs keeps
    import order from resolving/leaking AWS credentials -- tests import this
    module freely and only bind to (mocked) AWS when they call a helper. Behavior
    is identical to a module-level resource for real Lambda invocations.
    """
    global _dynamodb
    if _dynamodb is None:
        _dynamodb = boto3.resource("dynamodb", region_name=AWS_DEFAULT_REGION)
    return _dynamodb


STATUS_PENDING = "pending"
STATUS_APPROVED = "approved"
STATUS_DENIED = "denied"
STATUSES = (STATUS_PENDING, STATUS_APPROVED, STATUS_DENIED)


def create_request(
    requester_email: str,
    phone: str,
    saved_name: str | None,
    sub: str | None = None,
) -> dict:
    """
    Create a PENDING link request and return the stored item.

    `phone` is expected already NORMALIZED (last-10 digits) by the caller.
    `saved_name` is Dom's saved contact name for that number (or None).
    """
    if not requester_email:
        raise ValidationError(
            message="Cannot create a request without a requester email",
            handler="link_requests",
            function="create_request",
            field="requesterEmail",
        )
    if not phone:
        raise ValidationError(
            message="Cannot create a request with an empty phone handle",
            handler="link_requests",
            function="create_request",
            field="phone",
        )

    now = int(time.time())
    item = {
        "requestId": str(uuid.uuid4()),
        "requesterEmail": requester_email,
        "phone": phone,
        "savedName": saved_name,
        "sub": sub,
        "status": STATUS_PENDING,
        "createdAt": now,
        "updatedAt": now,
    }
    # DynamoDB rejects None-valued attributes on write -- drop them (savedName /
    # sub are legitimately absent for numbers Dom has no contact for / callers
    # with no sub claim). get_request() defaults them back to None on read.
    clean = {k: v for k, v in item.items() if v is not None}

    try:
        table = _get_dynamodb().Table(LINK_REQUESTS_TABLE_NAME)
        table.put_item(Item=clean)
        log.info(f"Link request created: {item['requestId']} for {requester_email} ({phone})")
        return item
    except Exception as err:
        log.error(f"Create link request failed: {err}")
        raise DynamoDBError(message=str(err), function="create_request", table=LINK_REQUESTS_TABLE_NAME)


def get_request(request_id: str) -> dict | None:
    """Fetch a single request by id. None if not found. Missing savedName/sub are
    normalized back to None so callers get a consistent shape."""
    try:
        table = _get_dynamodb().Table(LINK_REQUESTS_TABLE_NAME)
        item = table.get_item(Key={"requestId": request_id}).get("Item")
        if item is None:
            return None
        item.setdefault("savedName", None)
        item.setdefault("sub", None)
        return item
    except Exception as err:
        log.error(f"Get link request failed: {err}")
        raise DynamoDBError(message=str(err), function="get_request", table=LINK_REQUESTS_TABLE_NAME)


def list_by_status(status: str) -> list[dict]:
    """Every request with the given status (filtered Scan, paginated)."""
    try:
        table = _get_dynamodb().Table(LINK_REQUESTS_TABLE_NAME)
        items: list[dict] = []
        kwargs = {"FilterExpression": Attr("status").eq(status)}
        while True:
            res = table.scan(**kwargs)
            items.extend(res.get("Items", []))
            last_key = res.get("LastEvaluatedKey")
            if not last_key:
                break
            kwargs["ExclusiveStartKey"] = last_key
        return items
    except Exception as err:
        log.error(f"List link requests by status failed: {err}")
        raise DynamoDBError(message=str(err), function="list_by_status", table=LINK_REQUESTS_TABLE_NAME)


def list_pending() -> list[dict]:
    """Pending requests, oldest-first (createdAt asc) for the admin queue."""
    pending = list_by_status(STATUS_PENDING)
    return sorted(pending, key=lambda r: r.get("createdAt", 0))


def has_pending_for_email(email: str) -> bool:
    """True if the caller has a request still awaiting the admin's decision.
    Powers GET /me/get's "pending" state."""
    if not email:
        return False
    try:
        table = _get_dynamodb().Table(LINK_REQUESTS_TABLE_NAME)
        kwargs = {
            "FilterExpression": Attr("status").eq(STATUS_PENDING) & Attr("requesterEmail").eq(email),
            "Limit": 1,
        }
        while True:
            res = table.scan(**kwargs)
            if res.get("Items"):
                return True
            last_key = res.get("LastEvaluatedKey")
            if not last_key:
                return False
            kwargs["ExclusiveStartKey"] = last_key
    except Exception as err:
        log.error(f"Has-pending-for-email check failed: {err}")
        raise DynamoDBError(
            message=str(err), function="has_pending_for_email", table=LINK_REQUESTS_TABLE_NAME
        )


def set_status(request_id: str, status: str) -> dict:
    """
    Transition a request to a new status and return the updated item. Only
    updates a row that already exists (condition on requestId) -- callers that
    need a not-found signal should get_request() first.
    """
    if status not in STATUSES:
        raise ValidationError(
            message=f"status must be one of {STATUSES}",
            handler="link_requests",
            function="set_status",
            field="status",
        )
    now = int(time.time())
    try:
        table = _get_dynamodb().Table(LINK_REQUESTS_TABLE_NAME)
        res = table.update_item(
            Key={"requestId": request_id},
            UpdateExpression="SET #s = :s, #u = :u",
            ConditionExpression="attribute_exists(requestId)",
            ExpressionAttributeNames={"#s": "status", "#u": "updatedAt"},
            ExpressionAttributeValues={":s": status, ":u": now},
            ReturnValues="ALL_NEW",
        )
        return res.get("Attributes", {})
    except Exception as err:
        log.error(f"Set link request status failed: {err}")
        raise DynamoDBError(message=str(err), function="set_status", table=LINK_REQUESTS_TABLE_NAME)
