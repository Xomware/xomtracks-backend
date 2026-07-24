"""
XOMTRACKS Ingest Tokens
=======================
Per-user extractor ingest tokens (self-serve foundation Phase 3).

Each user runs their own extractor authenticating as themselves, so ingested
shares can be stamped with the right ownerId. A token is an OPAQUE random string;
only its SHA-256 HASH is ever persisted (PK of the xomtracks-ingest-tokens
table). The plaintext is returned to the owner exactly ONCE at mint and never
recoverable -- authentication hashes the presented bearer and looks the hash up.

Why hashed-opaque (not a JWT):
  - Revocable with no signing-key blast radius: a friend leaves -> flip `revoked`
    (or delete the row); every other token is unaffected. A stolen signing key
    for a JWT scheme would forge every user's identity.
  - Nothing sensitive at rest: a table leak exposes only irreversible hashes.

Table: xomtracks-ingest-tokens (see constants.INGEST_TOKENS_TABLE_NAME)
  PK = tokenHash  (SHA-256 hex of the opaque token)
  attrs: ownerId (Cognito sub), createdAt (epoch), revoked (bool),
         revokedAt (epoch, set on revoke), lastUsedAt (epoch, best-effort),
         label (optional human tag, e.g. the device name).
"""

import hashlib
import secrets
import time

import boto3

from lambdas.common.constants import AWS_DEFAULT_REGION, INGEST_TOKENS_TABLE_NAME
from lambdas.common.errors import DynamoDBError, NotFoundError
from lambdas.common.logger import get_logger

log = get_logger(__file__)

# Opaque tokens carry a short, non-secret prefix purely so a leaked/pasted token
# is recognizable as a Xomtracks ingest token (like GitHub's `ghp_`). The prefix
# is part of the hashed value -- it grants no capability on its own.
TOKEN_PREFIX = "xti_"

_dynamodb = None


def _get_dynamodb():
    """
    Lazily create (and cache) the DynamoDB resource on FIRST USE rather than at
    import time -- keeps import order from resolving/leaking AWS credentials, so
    tests import this module freely and only bind to (mocked) AWS on first call.
    """
    global _dynamodb
    if _dynamodb is None:
        _dynamodb = boto3.resource("dynamodb", region_name=AWS_DEFAULT_REGION)
    return _dynamodb


def _table():
    return _get_dynamodb().Table(INGEST_TOKENS_TABLE_NAME)


def generate_token() -> str:
    """A new opaque, URL-safe ingest token (256 bits of entropy + prefix)."""
    return f"{TOKEN_PREFIX}{secrets.token_urlsafe(32)}"


def hash_token(token: str) -> str:
    """SHA-256 hex digest of a token -- the value persisted as the row key."""
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def mint_token(owner_id: str, label: str | None = None) -> dict:
    """
    Mint a new ingest token for `owner_id` (a Cognito sub). Stores ONLY the hash;
    returns {token (plaintext -- shown once), tokenHash, ownerId, createdAt,
    label}. The plaintext is never persisted and never logged.
    """
    if not owner_id:
        raise DynamoDBError(
            message="owner_id is required to mint an ingest token",
            function="mint_token",
            table=INGEST_TOKENS_TABLE_NAME,
        )

    token = generate_token()
    token_hash = hash_token(token)
    now = int(time.time())
    row = {
        "tokenHash": token_hash,
        "ownerId": owner_id,
        "createdAt": now,
        "revoked": False,
    }
    if label:
        row["label"] = label

    try:
        # attribute_not_exists guards the (astronomically unlikely) hash
        # collision rather than silently overwriting another live token.
        _table().put_item(Item=row, ConditionExpression="attribute_not_exists(tokenHash)")
    except Exception as err:
        log.error(f"Mint ingest token failed: {err}")
        raise DynamoDBError(message=str(err), function="mint_token", table=INGEST_TOKENS_TABLE_NAME)

    log.info(f"Minted ingest token for owner={owner_id} tokenHash={token_hash}")
    return {"token": token, "tokenHash": token_hash, "ownerId": owner_id, "createdAt": now, "label": label}


def _get_row(token_hash: str) -> dict | None:
    try:
        res = _table().get_item(Key={"tokenHash": token_hash})
        return res.get("Item")
    except Exception as err:
        log.error(f"Get ingest token row failed: {err}")
        raise DynamoDBError(message=str(err), function="_get_row", table=INGEST_TOKENS_TABLE_NAME)


def resolve_owner(token: str) -> str | None:
    """
    Resolve the ownerId for a presented plaintext token, or None if the token is
    unknown or revoked. FAILS CLOSED: any lookup error resolves to None (deny)
    rather than raising -- a table hiccup must never authenticate an ingest, and
    the legacy SSM key path (see utility_helpers.resolve_ingest_owner) is checked
    FIRST so Dom's extractor is unaffected by a tokens-table outage.
    """
    if not token:
        return None
    token_hash = hash_token(token)
    try:
        row = _get_row(token_hash)
    except Exception as err:  # noqa: BLE001 -- fail closed on any lookup error
        log.error(f"resolve_owner lookup failed (denying): {err}")
        return None

    if not row or row.get("revoked"):
        return None

    owner_id = row.get("ownerId")
    if owner_id:
        _touch_last_used(token_hash)
    return owner_id


def _touch_last_used(token_hash: str) -> None:
    """Best-effort lastUsedAt stamp -- never fails the caller's request."""
    try:
        _table().update_item(
            Key={"tokenHash": token_hash},
            UpdateExpression="SET lastUsedAt = :n",
            ExpressionAttributeValues={":n": int(time.time())},
        )
    except Exception as err:  # noqa: BLE001 -- non-fatal telemetry
        log.warning(f"Failed to stamp lastUsedAt for tokenHash={token_hash}: {err}")


def revoke_token(owner_id: str, token_hash: str) -> dict:
    """
    Revoke the token identified by `token_hash`, SCOPED to `owner_id`: the row
    must exist AND be owned by this caller. Otherwise raise NotFoundError (we do
    not distinguish "missing" from "not yours", so a caller can't probe for other
    users' token hashes). Idempotent -- revoking an already-revoked token is fine.
    """
    row = _get_row(token_hash)
    if not row or row.get("ownerId") != owner_id:
        raise NotFoundError(
            message="Ingest token not found",
            handler="ingest_tokens",
            function="revoke_token",
            resource="ingest-token",
        )

    try:
        _table().update_item(
            Key={"tokenHash": token_hash},
            UpdateExpression="SET revoked = :t, revokedAt = :n",
            ExpressionAttributeValues={":t": True, ":n": int(time.time())},
        )
    except Exception as err:
        log.error(f"Revoke ingest token failed: {err}")
        raise DynamoDBError(message=str(err), function="revoke_token", table=INGEST_TOKENS_TABLE_NAME)

    log.info(f"Revoked ingest token owner={owner_id} tokenHash={token_hash}")
    return {"tokenHash": token_hash, "revoked": True}
