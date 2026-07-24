"""
XOMTRACKS DynamoDB Helpers
==========================
Generic table operations, plus access to xomtracks' single app-service-
account user row -- the Spotify-connected account the app plays/searches/
builds playlists through (self-contained per PLAN.md Option 3; NOT
xomify's users table).
"""

import time

import boto3
from boto3.dynamodb.conditions import Attr

from lambdas.common.constants import AWS_DEFAULT_REGION, APP_SERVICE_USER_EMAIL
from lambdas.common.errors import DynamoDBError, NotFoundError
from lambdas.common.logger import get_logger

log = get_logger(__file__)

_dynamodb = None

# Attribute names for the per-user Spotify connection (Phase 2). Stored on the
# caller's OWN xomtracks-users row (keyed by Cognito email -- the same row
# user_links writes linkedHandles to). `userId` mirrors the service-account
# row's attribute so the vendored Spotify/Playlist clients read the connected
# account's id unchanged; `spotifyUserId` is the explicit Phase-2 name.
SPOTIFY_REFRESH_TOKEN_ATTR = "refreshToken"
SPOTIFY_USER_ID_ATTR = "spotifyUserId"


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


def update_table_item_field(table_name: str, key_name: str, key_value: str, field_name: str, field_value) -> None:
    """Update a single field on a single item, by primary key."""
    try:
        table = _get_dynamodb().Table(table_name)
        table.update_item(
            Key={key_name: key_value},
            UpdateExpression="SET #f = :v",
            ExpressionAttributeNames={"#f": field_name},
            ExpressionAttributeValues={":v": field_value},
        )
    except Exception as err:
        log.error(f"Update table item field failed: {err}")
        raise DynamoDBError(message=str(err), function="update_table_item_field", table=table_name)


def get_app_service_user() -> dict:
    """
    Fetch xomtracks' single Spotify-connected service-account user row,
    keyed by APP_SERVICE_USER_EMAIL (set via SSM/Terraform at deploy time).

    Raises:
        NotFoundError: the configured email has no row yet (app hasn't
            completed its own Spotify OAuth connect flow).
    """
    from lambdas.common.constants import USERS_TABLE_NAME

    try:
        table = _get_dynamodb().Table(USERS_TABLE_NAME)
        res = table.get_item(Key={"email": APP_SERVICE_USER_EMAIL})
    except Exception as err:
        log.error(f"Get app service user failed: {err}")
        raise DynamoDBError(message=str(err), function="get_app_service_user", table=USERS_TABLE_NAME)

    item = res.get("Item")
    if not item:
        raise NotFoundError(
            message=f"App service user not found: {APP_SERVICE_USER_EMAIL!r}",
            handler="dynamo_helpers",
            function="get_app_service_user",
            resource=f"users/{APP_SERVICE_USER_EMAIL}",
        )
    return item


# ============================================
# Per-user Spotify connection (self-serve foundation Phase 2)
# ============================================
# A "connected" row is any xomtracks-users item carrying a refreshToken that the
# user minted via the OAuth flow (auth_spotify_callback). It is keyed by the
# caller's Cognito email and stamped with their ownerId (Cognito sub) so the
# owner-scoped consumers (crons, /playlists/create) can act as that user on
# Spotify. The service-account row (APP_SERVICE_USER_EMAIL) is the FALLBACK for
# any owner who hasn't connected yet -- keeping Dom's experience intact.


def _get_users_table():
    from lambdas.common.constants import USERS_TABLE_NAME
    return _get_dynamodb().Table(USERS_TABLE_NAME)


def store_spotify_auth_state(email: str, state: str, expires_at: int) -> None:
    """
    Stamp a one-time CSRF `state` (+ its expiry) on the caller's row at
    /auth/spotify-login. auth_spotify_callback verifies the presented state
    against this before exchanging the code, then clears it.
    """
    try:
        _get_users_table().update_item(
            Key={"email": email},
            UpdateExpression="SET spotifyAuthState = :s, spotifyAuthStateExp = :e, updatedAt = :u",
            ExpressionAttributeValues={":s": state, ":e": expires_at, ":u": int(time.time())},
        )
    except Exception as err:
        log.error(f"Store spotify auth state failed: {err}")
        raise DynamoDBError(message=str(err), function="store_spotify_auth_state")


def store_spotify_connection(email: str, owner_id: str, refresh_token: str, spotify_user_id: str) -> None:
    """
    Persist a completed OAuth connection on the caller's OWN row (keyed by
    Cognito email): the long-lived refreshToken, the Spotify account id, and the
    ownerId (Cognito sub) that owner-scoped consumers key by. Clears the one-time
    auth state so a code can't be replayed. The refreshToken is never logged
    (mask_sensitive_data covers it) and never returned to the client.
    """
    from lambdas.common.constants import USERS_TABLE_NAME

    now = int(time.time())
    try:
        _get_dynamodb().Table(USERS_TABLE_NAME).update_item(
            Key={"email": email},
            UpdateExpression=(
                "SET #rt = :rt, #suid = :suid, userId = :suid, ownerId = :owner, "
                "recordType = if_not_exists(recordType, :rtype), spotifyConnectedAt = :now, updatedAt = :now "
                "REMOVE spotifyAuthState, spotifyAuthStateExp"
            ),
            ExpressionAttributeNames={
                "#rt": SPOTIFY_REFRESH_TOKEN_ATTR,
                "#suid": SPOTIFY_USER_ID_ATTR,
            },
            ExpressionAttributeValues={
                ":rt": refresh_token,
                ":suid": spotify_user_id,
                ":owner": owner_id,
                ":rtype": "userLink",
                ":now": now,
            },
        )
    except Exception as err:
        log.error(f"Store spotify connection failed: {err}")
        raise DynamoDBError(message=str(err), function="store_spotify_connection", table=USERS_TABLE_NAME)


def list_spotify_connected_users() -> list[dict]:
    """
    Every xomtracks-users row that has completed the Spotify OAuth connect flow
    (a refreshToken is present). Paginated Scan -- the right tool at
    friend-group scale (same rationale as scan_shares_by_normalized_handles); a
    GSI on ownerId is the documented fast-follow if the user set grows.
    """
    try:
        table = _get_users_table()
        items: list[dict] = []
        kwargs = {"FilterExpression": Attr(SPOTIFY_REFRESH_TOKEN_ATTR).exists()}
        while True:
            res = table.scan(**kwargs)
            items.extend(res.get("Items", []))
            last_key = res.get("LastEvaluatedKey")
            if not last_key:
                break
            kwargs["ExclusiveStartKey"] = last_key
        return items
    except Exception as err:
        log.error(f"List spotify connected users failed: {err}")
        raise DynamoDBError(message=str(err), function="list_spotify_connected_users")


def get_spotify_user_by_owner(owner_id: str) -> dict | None:
    """
    The connected row for a given ownerId (Cognito sub), or None if that owner
    hasn't connected their Spotify yet. Filtered Scan on ownerId + refreshToken
    presence (see list_spotify_connected_users on why Scan is right here).
    """
    if not owner_id:
        return None
    try:
        table = _get_users_table()
        kwargs = {
            "FilterExpression": Attr("ownerId").eq(owner_id) & Attr(SPOTIFY_REFRESH_TOKEN_ATTR).exists(),
        }
        while True:
            res = table.scan(**kwargs)
            items = res.get("Items", [])
            if items:
                return items[0]
            last_key = res.get("LastEvaluatedKey")
            if not last_key:
                return None
            kwargs["ExclusiveStartKey"] = last_key
    except Exception as err:
        log.error(f"Get spotify user by owner failed: {err}")
        raise DynamoDBError(message=str(err), function="get_spotify_user_by_owner")
