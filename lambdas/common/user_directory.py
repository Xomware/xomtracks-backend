"""
XOMTRACKS User Directory (admin portal WS6)
===========================================
Tracks everyone who signs into Xomtracks. xomify logins are stateless Spotify
JWTs with no user store of their own, so we materialize a lightweight directory
by piggy-backing on the EXISTING xomtracks-users table (PK email -- the same row
that already holds a caller's linkedHandles + per-user Spotify connection). Three
additive directory attributes live on that row:

  - firstSeen (epoch): first authed request we ever saw from this email.
  - lastSeen  (epoch): most recent authed request (bumped, throttled).
  - ownIngest (bool, computed at read time, not stored): whether the user runs
    their own extractor (holds a live ingest token OR already owns shares).

Auto-upsert hook: `record_seen(email)` is called from utility_helpers.get_caller_
owner on EVERY authed request. It is deliberately LIGHTWEIGHT and FAIL-OPEN --
it must never break the caller's request:
  - An in-process throttle skips the write when this warm container already
    bumped the same email within THROTTLE_SECONDS (avoids a write per call).
  - A conditional UpdateItem additionally skips the DB write cross-container when
    lastSeen is already fresh, so concurrent containers don't hammer the row.
  - EVERY failure (throttle miss, conditional fail, table hiccup) is swallowed.

No new table: extending the users row fits the directory cleanly (verified schema
-- email PK, already multi-purpose). See docs/features/xomtracks-xomify-merge/
PLAN.md WS6.
"""

import time

import boto3

from lambdas.common.constants import AWS_DEFAULT_REGION, USERS_TABLE_NAME
from lambdas.common.dynamo_helpers import SPOTIFY_REFRESH_TOKEN_ATTR
from lambdas.common.logger import get_logger

log = get_logger(__file__)

# Directory attribute names on the xomtracks-users row.
FIRST_SEEN_ATTR = "firstSeen"
LAST_SEEN_ATTR = "lastSeen"

# Skip re-writing lastSeen if it was bumped within this window (10 minutes). Kept
# as a plain int (seconds) so both the in-process throttle and the conditional
# UpdateItem cutoff use the same value.
THROTTLE_SECONDS = 600

_dynamodb = None

# In-process throttle: email -> epoch of the last write THIS container issued.
# Cleared on cold start; a warm container reuses it to skip redundant writes.
_last_written: dict[str, int] = {}


def _get_dynamodb():
    """Lazily create + cache the DynamoDB resource on first use (test-friendly)."""
    global _dynamodb
    if _dynamodb is None:
        _dynamodb = boto3.resource("dynamodb", region_name=AWS_DEFAULT_REGION)
    return _dynamodb


def _table():
    return _get_dynamodb().Table(USERS_TABLE_NAME)


def record_seen(email: str) -> None:
    """
    Auto-upsert directory hook -- stamp firstSeen (once) + bump lastSeen for the
    caller. FAIL-OPEN: any error (or an unconfigured table) is swallowed so the
    caller's request is never impacted. Throttled to at most one write per email
    per THROTTLE_SECONDS per container.
    """
    if not email or not USERS_TABLE_NAME:
        return

    now = int(time.time())

    # In-process throttle -- this container already bumped this email recently.
    last = _last_written.get(email)
    if last is not None and (now - last) < THROTTLE_SECONDS:
        return

    cutoff = now - THROTTLE_SECONDS
    try:
        _table().update_item(
            Key={"email": email},
            UpdateExpression=(
                "SET #fs = if_not_exists(#fs, :now), #ls = :now, "
                "#rt = if_not_exists(#rt, :rtype)"
            ),
            # Only write when lastSeen is absent or older than the throttle
            # window -- keeps concurrent containers from hammering the row.
            ConditionExpression="attribute_not_exists(#ls) OR #ls < :cutoff",
            ExpressionAttributeNames={
                "#fs": FIRST_SEEN_ATTR,
                "#ls": LAST_SEEN_ATTR,
                "#rt": "recordType",
            },
            ExpressionAttributeValues={
                ":now": now,
                ":cutoff": cutoff,
                ":rtype": "directory",
            },
        )
        _last_written[email] = now
    except Exception as err:  # noqa: BLE001 -- fail-open: never break the request
        # ConditionalCheckFailed (fresh lastSeen) is the common, expected "skip";
        # mark the throttle so we don't retry every call, and stay quiet on it.
        name = err.__class__.__name__
        if name == "ConditionalCheckFailedException":
            _last_written[email] = now
        else:
            log.warning("record_seen skipped for %s: %s", email, name)


def _own_ingest(email: str) -> bool:
    """
    Whether `email` runs their own extractor -- a live ingest token OR existing
    owned shares (mirrors GET /me/get's ownIngest). Fail-closed to False.
    """
    try:
        from lambdas.common import ingest_tokens
        from lambdas.common.shares_dynamo import owner_has_shares

        return ingest_tokens.owner_has_active_token(email) or owner_has_shares(email)
    except Exception as err:  # noqa: BLE001 -- non-critical flag
        log.warning("ownIngest lookup failed for %s: %s", email, err)
        return False


def list_directory() -> list[dict]:
    """
    Every directory row (any xomtracks-users item that has been `record_seen`n --
    i.e. carries a lastSeen), newest-last-seen first. Each entry:
      {email, firstSeen, lastSeen, ownIngest, spotifyConnected}

    Filtered Scan -- the right tool at friend-group scale (same rationale as the
    other users-table scans). Rows without lastSeen (the Spotify service-account
    row, unseen link rows) are excluded so the directory is exactly "people who
    signed in". ownIngest is computed per row at read time (not stored).
    """
    try:
        from boto3.dynamodb.conditions import Attr

        table = _table()
        rows: list[dict] = []
        kwargs = {"FilterExpression": Attr(LAST_SEEN_ATTR).exists()}
        while True:
            res = table.scan(**kwargs)
            rows.extend(res.get("Items", []))
            last_key = res.get("LastEvaluatedKey")
            if not last_key:
                break
            kwargs["ExclusiveStartKey"] = last_key
    except Exception as err:
        from lambdas.common.errors import DynamoDBError

        log.error("list_directory failed: %s", err)
        raise DynamoDBError(message=str(err), function="list_directory", table=USERS_TABLE_NAME)

    directory = [
        {
            "email": r.get("email"),
            "firstSeen": r.get(FIRST_SEEN_ATTR),
            "lastSeen": r.get(LAST_SEEN_ATTR),
            "ownIngest": _own_ingest(r.get("email")),
            "spotifyConnected": bool(r.get(SPOTIFY_REFRESH_TOKEN_ATTR)),
        }
        for r in rows
        if r.get("email")
    ]
    directory.sort(key=lambda d: d.get("lastSeen") or 0, reverse=True)
    return directory


def is_known_user(email: str) -> bool:
    """
    True if `email` is in the directory (has a users row with a lastSeen). Backs
    the impersonation 404 -- admin can only view-as a user we've actually seen.
    Fail-CLOSED to False on any lookup error.
    """
    if not email or not USERS_TABLE_NAME:
        return False
    try:
        res = _table().get_item(Key={"email": email})
        item = res.get("Item")
        return bool(item and item.get(LAST_SEEN_ATTR) is not None)
    except Exception as err:  # noqa: BLE001 -- fail closed
        log.warning("is_known_user lookup failed for %s: %s", email, err)
        return False
