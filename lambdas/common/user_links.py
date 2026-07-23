"""
XOMTRACKS User <-> Phone Link Store
===================================
Additive mapping from a Cognito identity (email = PK, plus sub) to one or more
iMessage phone handles. Stored as rows on the SAME xomtracks-users table that
holds the single Spotify service-account row -- keyed by the caller's Cognito
email, so a member's link record is a distinct item that never collides with
the service-account row or anyone else's.

Why this exists: shares are keyed by the raw iMessage handle (sharerHandle,
e.g. "+13364042196"); Cognito users sign in by email. Linking a member's phone
to their Cognito email is what lets a signed-in group member see THEIR own
shares and be attributed for them.

Handles are stored NORMALIZED (last-10 digits, see phone.normalize_phone) so
they compare equal to a normalized sharerHandle regardless of formatting.
`linkedHandles` is a DynamoDB String Set updated additively via ADD -- so
re-linking the same number is idempotent and linking a second number appends.

VERIFICATION is trust-based here (the store just links). The link handler
reports how many existing shares carry the handle; hardened SMS-OTP
verification (SNS / 10DLC) is a deliberate fast-follow, not built.
"""

import time

import boto3

from lambdas.common.constants import AWS_DEFAULT_REGION, USERS_TABLE_NAME
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


RECORD_TYPE_USER_LINK = "userLink"
LINKED_HANDLES_ATTR = "linkedHandles"


def get_user_record(email: str) -> dict | None:
    """Fetch a caller's user-link row by Cognito email. None if not linked."""
    try:
        table = _get_dynamodb().Table(USERS_TABLE_NAME)
        res = table.get_item(Key={"email": email})
        return res.get("Item")
    except Exception as err:
        log.error(f"Get user record failed: {err}")
        raise DynamoDBError(message=str(err), function="get_user_record", table=USERS_TABLE_NAME)


def get_linked_handles(email: str) -> set[str]:
    """The set of normalized handles linked to this caller (empty if none)."""
    record = get_user_record(email)
    if not record:
        return set()
    handles = record.get(LINKED_HANDLES_ATTR)
    return set(handles) if handles else set()


def link_phone(email: str, normalized_handle: str, sub: str | None = None) -> set[str]:
    """
    Additively link one normalized handle to the caller's Cognito email.

    Uses ADD on the linkedHandles String Set (idempotent re-link, appends a
    new number), SETs recordType/updatedAt/sub, and only writes createdAt on
    first link (if_not_exists). Returns the full set of linked handles after
    the write.
    """
    if not normalized_handle:
        raise ValidationError(
            message="Cannot link an empty handle",
            handler="user_links",
            function="link_phone",
            field="handle",
        )

    now = int(time.time())
    try:
        table = _get_dynamodb().Table(USERS_TABLE_NAME)

        expr_names = {
            "#h": LINKED_HANDLES_ATTR,
            "#rt": "recordType",
            "#u": "updatedAt",
            "#c": "createdAt",
        }
        expr_values = {
            ":h": {normalized_handle},
            ":rt": RECORD_TYPE_USER_LINK,
            ":u": now,
            ":c": now,
        }
        set_parts = ["#rt = :rt", "#u = :u", "#c = if_not_exists(#c, :c)"]
        if sub:
            expr_names["#s"] = "sub"
            expr_values[":s"] = sub
            set_parts.append("#s = :s")

        update_expr = "ADD #h :h SET " + ", ".join(set_parts)
        res = table.update_item(
            Key={"email": email},
            UpdateExpression=update_expr,
            ExpressionAttributeNames=expr_names,
            ExpressionAttributeValues=expr_values,
            ReturnValues="ALL_NEW",
        )
        attrs = res.get("Attributes", {})
        return set(attrs.get(LINKED_HANDLES_ATTR) or [])
    except ValidationError:
        raise
    except Exception as err:
        log.error(f"Link phone failed: {err}")
        raise DynamoDBError(message=str(err), function="link_phone", table=USERS_TABLE_NAME)
