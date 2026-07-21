"""
XOMTRACKS DynamoDB Helpers
==========================
Generic table operations, plus access to xomtracks' single app-service-
account user row -- the Spotify-connected account the app plays/searches/
builds playlists through (self-contained per PLAN.md Option 3; NOT
xomify's users table).
"""

import boto3

from lambdas.common.constants import AWS_DEFAULT_REGION, APP_SERVICE_USER_EMAIL
from lambdas.common.errors import DynamoDBError, NotFoundError
from lambdas.common.logger import get_logger

log = get_logger(__file__)

dynamodb = boto3.resource("dynamodb", region_name=AWS_DEFAULT_REGION)


def update_table_item_field(table_name: str, key_name: str, key_value: str, field_name: str, field_value) -> None:
    """Update a single field on a single item, by primary key."""
    try:
        table = dynamodb.Table(table_name)
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
        table = dynamodb.Table(USERS_TABLE_NAME)
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
