"""
RED-before-GREEN: lambdas/common/dynamo_helpers.py -- generic table helpers
plus xomtracks' single app-service-account user row (self-contained
Spotify OAuth per PLAN.md Option 3 -- distinct from xomify's users table).
"""

import boto3
import pytest
from moto import mock_aws

from lambdas.common.constants import USERS_TABLE_NAME


def _create_users_table():
    ddb = boto3.resource("dynamodb", region_name="us-east-1")
    ddb.create_table(
        TableName=USERS_TABLE_NAME,
        KeySchema=[{"AttributeName": "email", "KeyType": "HASH"}],
        AttributeDefinitions=[{"AttributeName": "email", "AttributeType": "S"}],
        ProvisionedThroughput={"ReadCapacityUnits": 5, "WriteCapacityUnits": 5},
    )
    return boto3.resource("dynamodb", region_name="us-east-1").Table(USERS_TABLE_NAME)


@pytest.fixture
def users_table():
    with mock_aws():
        yield _create_users_table()


class TestUpdateTableItemField:
    def test_updates_existing_field(self, users_table):
        from lambdas.common.dynamo_helpers import update_table_item_field

        users_table.put_item(Item={"email": "app@xomtracks.xomware.com", "refreshToken": "old"})
        update_table_item_field(USERS_TABLE_NAME, "email", "app@xomtracks.xomware.com", "refreshToken", "new")

        item = users_table.get_item(Key={"email": "app@xomtracks.xomware.com"})["Item"]
        assert item["refreshToken"] == "new"


class TestGetAppServiceUser:
    def test_returns_the_configured_service_user(self, users_table, monkeypatch):
        from lambdas.common import constants
        monkeypatch.setattr(constants, "APP_SERVICE_USER_EMAIL", "app@xomtracks.xomware.com")
        # dynamo_helpers reads the constant at call time via the module,
        # not a copied import, so patching constants.APP_SERVICE_USER_EMAIL
        # is sufficient here.
        import lambdas.common.dynamo_helpers as dynamo_helpers
        monkeypatch.setattr(dynamo_helpers, "APP_SERVICE_USER_EMAIL", "app@xomtracks.xomware.com")

        users_table.put_item(Item={"email": "app@xomtracks.xomware.com", "refreshToken": "rt1", "userId": "spotify-uid"})

        from lambdas.common.dynamo_helpers import get_app_service_user
        user = get_app_service_user()

        assert user["email"] == "app@xomtracks.xomware.com"
        assert user["refreshToken"] == "rt1"

    def test_missing_row_raises_not_found(self, users_table, monkeypatch):
        import lambdas.common.dynamo_helpers as dynamo_helpers
        monkeypatch.setattr(dynamo_helpers, "APP_SERVICE_USER_EMAIL", "nobody@example.com")

        from lambdas.common.dynamo_helpers import get_app_service_user
        from lambdas.common.errors import NotFoundError

        with pytest.raises(NotFoundError):
            get_app_service_user()


class TestSpotifyConnection:
    """Per-user Spotify connection storage/lookup (self-serve Phase 2)."""

    def test_store_connection_and_lookup_by_owner(self, users_table):
        from lambdas.common.dynamo_helpers import (
            get_spotify_user_by_owner,
            store_spotify_connection,
        )

        store_spotify_connection("dom@example.com", "sub-dom", "RT-dom", "spotify-dom")

        row = get_spotify_user_by_owner("sub-dom")
        assert row is not None
        assert row["email"] == "dom@example.com"
        assert row["refreshToken"] == "RT-dom"
        assert row["ownerId"] == "sub-dom"
        assert row["spotifyUserId"] == "spotify-dom"
        # userId mirrors the spotify id so the vendored clients read it unchanged
        assert row["userId"] == "spotify-dom"

    def test_lookup_unknown_owner_is_none(self, users_table):
        from lambdas.common.dynamo_helpers import get_spotify_user_by_owner
        assert get_spotify_user_by_owner("nobody") is None

    def test_list_connected_users_only_returns_rows_with_token(self, users_table):
        from lambdas.common.dynamo_helpers import (
            list_spotify_connected_users,
            store_spotify_connection,
        )

        # a plain link row (no refreshToken) must NOT appear
        users_table.put_item(Item={"email": "linkonly@example.com", "linkedHandles": {"+13360001111"}})
        store_spotify_connection("a@example.com", "sub-a", "RT-a", "sp-a")
        store_spotify_connection("b@example.com", "sub-b", "RT-b", "sp-b")

        connected = list_spotify_connected_users()
        emails = {r["email"] for r in connected}
        assert emails == {"a@example.com", "b@example.com"}

    def test_store_auth_state_then_connection_clears_it(self, users_table):
        from lambdas.common.dynamo_helpers import (
            store_spotify_auth_state,
            store_spotify_connection,
        )

        store_spotify_auth_state("dom@example.com", "STATE1", 9999999999)
        row = users_table.get_item(Key={"email": "dom@example.com"})["Item"]
        assert row["spotifyAuthState"] == "STATE1"

        store_spotify_connection("dom@example.com", "sub-dom", "RT", "sp")
        row = users_table.get_item(Key={"email": "dom@example.com"})["Item"]
        assert "spotifyAuthState" not in row
        assert row["refreshToken"] == "RT"
