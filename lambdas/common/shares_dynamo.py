"""
XOMTRACKS Shares DynamoDB Helpers
=================================
Database operations for the xomtracks-shares table.

Table structure:
- PK: shareId (string) -- a deterministic hash of (messageGuid, sourceUrl),
  NOT messageGuid alone. A single iMessage can contain more than one music
  link (rare, but real); each is a distinct share. Deriving shareId this
  way makes ingest idempotent per-link while keeping messageGuid as the
  human-meaningful "which text message did this come from" field on every
  row (per PLAN.md's Data Model table).
- GSI-1 (SHARES_DIRECTION_INDEX): PK direction, SK messageDate -- MVP
  time-window-per-direction browse query.
- GSI-2 (SHARES_SHARER_INDEX): PK sharerHandle, SK messageDate -- reserved
  for the by-sharer fast-follow (FF.2). Provisioned now, not wired to any
  handler yet.
"""

import uuid
from decimal import Decimal

import boto3
from boto3.dynamodb.conditions import Attr, Key

from lambdas.common.constants import (
    SHARES_TABLE_NAME,
    SHARES_DIRECTION_INDEX,
    SHARES_SHARER_INDEX,
    SHARES_OWNER_DIRECTION_INDEX,
)
from lambdas.common.errors import DynamoDBError
from lambdas.common.logger import get_logger

log = get_logger(__file__)

# Fixed namespace so derive_share_id() is deterministic across processes/runs.
_SHARE_ID_NAMESPACE = uuid.UUID("6f6e0d9e-6d9a-4c8d-9b7a-2a6c9c8f7a11")

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
        _dynamodb = boto3.resource("dynamodb", region_name="us-east-1")
    return _dynamodb


def derive_share_id(message_guid: str, source_url: str) -> str:
    """
    Deterministic shareId for (messageGuid, sourceUrl). Same inputs always
    produce the same id -- this is what makes put_share_idempotent() safe
    to call repeatedly (extractor re-scans, backfills, retries after a
    failed push all naturally dedup).
    """
    key = f"{message_guid}::{source_url}"
    # uuid5 over a fixed namespace -- stable, no external state needed.
    return str(uuid.uuid5(_SHARE_ID_NAMESPACE, key))


def compute_owner_direction(owner_id: str | None, direction: str | None) -> str | None:
    """
    Derive the GSI-3 hash key `<ownerId>#<direction>` for an owner-scoped share.

    Returns None when either input is absent -- a legacy (unowned) write then
    omits ownerDirection entirely, keeping GSI-3 sparse (same rationale as
    _strip_none for the GSI-2 sharerHandle key). Multi-tenant Phase 1.
    """
    if not owner_id or not direction:
        return None
    return f"{owner_id}#{direction}"


def _apply_owner_direction(item: dict) -> dict:
    """
    Ensure `ownerDirection` is consistent with `ownerId` + `direction` on write.

    When an ownerId is present we always (re)derive ownerDirection so the GSI-3
    key can never drift from the record. When ownerId is absent the record stays
    legacy/unowned and ownerDirection is left off (and _strip_none drops any
    None). Returns a shallow copy -- never mutates the caller's dict.
    """
    owner_direction = compute_owner_direction(item.get("ownerId"), item.get("direction"))
    if owner_direction is None:
        return item
    return {**item, "ownerDirection": owner_direction}


def _strip_none(item: dict) -> dict:
    """
    Drop None-valued attributes before writing to DynamoDB.

    Required because `sharerHandle` is a GSI-2 key attribute: DynamoDB
    rejects a NULL-typed value for any attribute used as a GSI key
    (ValidationException: type mismatch, expected S). `direction=out`
    shares (Dom is the sender) legitimately have no sharerHandle -- simply
    omitting the attribute makes the item correctly absent from GSI-2 (a
    sparse index) rather than erroring on write.
    """
    return {k: v for k, v in item.items() if v is not None}


def put_share_idempotent(share: dict) -> tuple[dict, bool]:
    """
    Conditionally put a share item -- creates it if shareId doesn't exist
    yet, otherwise returns the existing item unchanged.

    Returns:
        (item, created) where created is False if the item already existed
        (idempotent re-ingest, not an error).
    """
    table = _get_dynamodb().Table(SHARES_TABLE_NAME)
    clean_share = _strip_none(_apply_owner_direction(share))
    try:
        table.put_item(
            Item=clean_share,
            ConditionExpression="attribute_not_exists(shareId)",
        )
        log.info(f"Share written: {share.get('shareId')}")
        return clean_share, True
    except table.meta.client.exceptions.ConditionalCheckFailedException:
        log.info(f"Share already exists (idempotent no-op): {share.get('shareId')}")
        existing = table.get_item(Key={"shareId": share["shareId"]}).get("Item", share)
        return existing, False
    except Exception as err:
        log.error(f"Put share failed: {err}")
        raise DynamoDBError(message=str(err), function="put_share_idempotent", table=SHARES_TABLE_NAME)


def get_share(share_id: str) -> dict | None:
    """Fetch a single share by id. Returns None if not found."""
    try:
        table = _get_dynamodb().Table(SHARES_TABLE_NAME)
        res = table.get_item(Key={"shareId": share_id})
        return res.get("Item")
    except Exception as err:
        log.error(f"Get share failed: {err}")
        raise DynamoDBError(message=str(err), function="get_share", table=SHARES_TABLE_NAME)


def _to_dynamo_value(value):
    """DynamoDB rejects native `float` -- convert via str() to avoid binary
    floating-point drift (e.g. matchConfidence=0.8 from rapidfuzz)."""
    if isinstance(value, float):
        return Decimal(str(value))
    return value


def update_match_result(share_id: str, **fields) -> dict:
    """
    Update a share's matching result fields (matchStatus, matchConfidence,
    resolvedSpotifyId, resolvedSpotifyUri, trackTitle, trackArtist). Used
    by both the async matcher and the manual override endpoint.
    """
    if not fields:
        raise DynamoDBError(message="No fields to update", function="update_match_result", table=SHARES_TABLE_NAME)

    try:
        table = _get_dynamodb().Table(SHARES_TABLE_NAME)
        update_expr = "SET " + ", ".join(f"#{k} = :{k}" for k in fields)
        expr_names = {f"#{k}": k for k in fields}
        expr_values = {f":{k}": _to_dynamo_value(v) for k, v in fields.items()}

        res = table.update_item(
            Key={"shareId": share_id},
            UpdateExpression=update_expr,
            ExpressionAttributeNames=expr_names,
            ExpressionAttributeValues=expr_values,
            ReturnValues="ALL_NEW",
        )
        return res.get("Attributes", {})
    except Exception as err:
        log.error(f"Update match result failed: {err}")
        raise DynamoDBError(message=str(err), function="update_match_result", table=SHARES_TABLE_NAME)


def scan_shares_by_match_status(match_status: str) -> list[dict]:
    """
    Scan the whole xomtracks-shares table for items with the given
    matchStatus (e.g. 'pending'). There is no GSI on matchStatus -- the
    matching sweep is an infrequent, whole-table backfill/cron pass, so a
    filtered Scan (paginated) is the right tool rather than provisioning an
    index for a low-frequency read.

    Returns every matching item across all Scan pages.
    """
    try:
        table = _get_dynamodb().Table(SHARES_TABLE_NAME)
        items: list[dict] = []
        kwargs = {"FilterExpression": Attr("matchStatus").eq(match_status)}
        while True:
            res = table.scan(**kwargs)
            items.extend(res.get("Items", []))
            last_key = res.get("LastEvaluatedKey")
            if not last_key:
                break
            kwargs["ExclusiveStartKey"] = last_key
        return items
    except Exception as err:
        log.error(f"Scan shares by match status failed: {err}")
        raise DynamoDBError(message=str(err), function="scan_shares_by_match_status", table=SHARES_TABLE_NAME)


def scan_shares_by_normalized_handles(handles: set[str], since_epoch: int = 0) -> list[dict]:
    """
    Return every share whose sharerHandle NORMALIZES to one of the given
    last-10-digit handles, with messageDate >= since_epoch.

    Powers "my shares" for a linked member and the matched-count reported on
    link. sharerHandle is stored RAW (E.164, e.g. "+13364042196") while linked
    handles are normalized (last-10 digits, see phone.normalize_phone), so a
    GSI-2 lookup on the raw value can't be keyed by the normalized form -- we
    scan and normalize each row's handle in Python. A filtered Scan is fine at
    friend-group scale (same rationale as scan_shares_by_match_status); a
    normalized-handle GSI is a fast-follow if volume grows.
    """
    from lambdas.common.phone import normalize_phone

    wanted = {h for h in handles if h}
    if not wanted:
        return []

    try:
        table = _get_dynamodb().Table(SHARES_TABLE_NAME)
        items: list[dict] = []
        kwargs = {
            "FilterExpression": Attr("sharerHandle").exists() & Attr("messageDate").gte(since_epoch),
        }
        while True:
            res = table.scan(**kwargs)
            for item in res.get("Items", []):
                if normalize_phone(item.get("sharerHandle")) in wanted:
                    items.append(item)
            last_key = res.get("LastEvaluatedKey")
            if not last_key:
                break
            kwargs["ExclusiveStartKey"] = last_key
        return items
    except Exception as err:
        log.error(f"Scan shares by normalized handles failed: {err}")
        raise DynamoDBError(
            message=str(err), function="scan_shares_by_normalized_handles", table=SHARES_TABLE_NAME
        )


def query_shares_by_direction(direction: str, since_epoch: int) -> list[dict]:
    """
    Query all shares in a direction ('in' | 'out') with messageDate >=
    since_epoch, via GSI-1. Powers GET /shares?direction=&window=.
    """
    try:
        table = _get_dynamodb().Table(SHARES_TABLE_NAME)
        items: list[dict] = []
        kwargs = {
            "IndexName": SHARES_DIRECTION_INDEX,
            "KeyConditionExpression": Key("direction").eq(direction) & Key("messageDate").gte(since_epoch),
        }
        while True:
            res = table.query(**kwargs)
            items.extend(res.get("Items", []))
            last_key = res.get("LastEvaluatedKey")
            if not last_key:
                break
            kwargs["ExclusiveStartKey"] = last_key
        return items
    except Exception as err:
        log.error(f"Query shares by direction failed: {err}")
        raise DynamoDBError(message=str(err), function="query_shares_by_direction", table=SHARES_TABLE_NAME)


def query_shares_by_owner_direction(owner_id: str, direction: str, since_epoch: int) -> list[dict]:
    """
    Query all shares owned by `owner_id` in a direction ('in' | 'out') with
    messageDate >= since_epoch, via GSI-3 (ownerDirection-messageDate-index).

    The OWNER-SCOPED analogue of query_shares_by_direction (GSI-1). For the sole
    legacy owner (Dom) the returned set is IDENTICAL to the GSI-1 query once the
    ownerId backfill has stamped every row -- proven by the parity test. A second
    owner gets only their own shares. `query_shares_by_direction` is left intact
    as the instant-rollback read path (flip OWNER_SCOPING_ENABLED off).
    """
    owner_direction = compute_owner_direction(owner_id, direction)
    if owner_direction is None:
        return []
    try:
        table = _get_dynamodb().Table(SHARES_TABLE_NAME)
        items: list[dict] = []
        kwargs = {
            "IndexName": SHARES_OWNER_DIRECTION_INDEX,
            "KeyConditionExpression": Key("ownerDirection").eq(owner_direction)
            & Key("messageDate").gte(since_epoch),
        }
        while True:
            res = table.query(**kwargs)
            items.extend(res.get("Items", []))
            last_key = res.get("LastEvaluatedKey")
            if not last_key:
                break
            kwargs["ExclusiveStartKey"] = last_key
        return items
    except Exception as err:
        log.error(f"Query shares by owner direction failed: {err}")
        raise DynamoDBError(
            message=str(err), function="query_shares_by_owner_direction", table=SHARES_TABLE_NAME
        )


def query_shares_by_sharer(sharer_handle: str, since_epoch: int) -> list[dict]:
    """
    Query all shares from a given sharerHandle with messageDate >=
    since_epoch, via GSI-2. RESERVED for the by-sharer fast-follow (FF.2)
    -- implemented + tested now since the GSI is cheap, but no handler
    calls this yet.
    """
    try:
        table = _get_dynamodb().Table(SHARES_TABLE_NAME)
        items: list[dict] = []
        kwargs = {
            "IndexName": SHARES_SHARER_INDEX,
            "KeyConditionExpression": Key("sharerHandle").eq(sharer_handle) & Key("messageDate").gte(since_epoch),
        }
        while True:
            res = table.query(**kwargs)
            items.extend(res.get("Items", []))
            last_key = res.get("LastEvaluatedKey")
            if not last_key:
                break
            kwargs["ExclusiveStartKey"] = last_key
        return items
    except Exception as err:
        log.error(f"Query shares by sharer failed: {err}")
        raise DynamoDBError(message=str(err), function="query_shares_by_sharer", table=SHARES_TABLE_NAME)
