"""
Cross-app Spotify-token sync.

A self-serve user already granted Spotify at xomify sign-in (the app is
unreachable otherwise), and xomtracks reuses xomify's Spotify APP -- so that
refreshToken is valid here as-is. When they opt in (mint an ingest token), we
copy it from the xomify users table onto their xomtracks-users row (same email
key) so the rolling-playlist cron -- which scans xomtracks-users for a
refreshToken -- can build their OWN playlists. No second OAuth.
"""

import json

import boto3
import pytest
from moto import mock_aws

from conftest import make_xomify_token
from lambdas.common.constants import INGEST_TOKENS_TABLE_NAME, USERS_TABLE_NAME
from lambdas.common.dynamo_helpers import SPOTIFY_REFRESH_TOKEN_ATTR, XOMIFY_USERS_TABLE_NAME

EMAIL = "friend@example.com"


def _mk_users_table(name):
    boto3.resource("dynamodb", region_name="us-east-1").create_table(
        TableName=name,
        KeySchema=[{"AttributeName": "email", "KeyType": "HASH"}],
        AttributeDefinitions=[{"AttributeName": "email", "AttributeType": "S"}],
        BillingMode="PAY_PER_REQUEST",
    )


def _mk_tokens_table():
    boto3.resource("dynamodb", region_name="us-east-1").create_table(
        TableName=INGEST_TOKENS_TABLE_NAME,
        KeySchema=[{"AttributeName": "tokenHash", "KeyType": "HASH"}],
        AttributeDefinitions=[{"AttributeName": "tokenHash", "AttributeType": "S"}],
        BillingMode="PAY_PER_REQUEST",
    )


@pytest.fixture
def tables():
    with mock_aws():
        _mk_users_table(USERS_TABLE_NAME)
        _mk_users_table(XOMIFY_USERS_TABLE_NAME)
        _mk_tokens_table()
        yield boto3.resource("dynamodb", region_name="us-east-1")


def _put_xomify_user(ddb, email=EMAIL, refresh_token="rt-xyz", user_id="spuser1"):
    ddb.Table(XOMIFY_USERS_TABLE_NAME).put_item(
        Item={"email": email, "userId": user_id, "refreshToken": refresh_token, "active": True}
    )


class TestReadXomifyConnection:
    def test_returns_token_and_user_id(self, tables):
        from lambdas.common import dynamo_helpers

        _put_xomify_user(tables, refresh_token="rt-abc", user_id="sp-9")
        assert dynamo_helpers.read_xomify_spotify_connection(EMAIL) == {
            "refreshToken": "rt-abc",
            "spotifyUserId": "sp-9",
        }

    def test_none_when_no_row(self, tables):
        from lambdas.common import dynamo_helpers

        assert dynamo_helpers.read_xomify_spotify_connection(EMAIL) is None

    def test_none_when_row_has_no_token(self, tables):
        from lambdas.common import dynamo_helpers

        tables.Table(XOMIFY_USERS_TABLE_NAME).put_item(Item={"email": EMAIL, "userId": "x"})
        assert dynamo_helpers.read_xomify_spotify_connection(EMAIL) is None


class TestEnsureConnectionFromXomify:
    def test_syncs_token_and_enrolls_for_cron(self, tables):
        from lambdas.common import dynamo_helpers

        _put_xomify_user(tables, refresh_token="rt-sync", user_id="sp-7")

        assert dynamo_helpers.ensure_spotify_connection_from_xomify(EMAIL) is True

        row = tables.Table(USERS_TABLE_NAME).get_item(Key={"email": EMAIL})["Item"]
        assert row[SPOTIFY_REFRESH_TOKEN_ATTR] == "rt-sync"
        assert row["ownerId"] == EMAIL
        # Now visible to the rolling-playlist cron.
        connected = [u["email"] for u in dynamo_helpers.list_spotify_connected_users()]
        assert EMAIL in connected

    def test_noop_when_already_connected(self, tables):
        from lambdas.common import dynamo_helpers

        tables.Table(USERS_TABLE_NAME).put_item(Item={"email": EMAIL, "refreshToken": "existing"})
        _put_xomify_user(tables, refresh_token="rt-new")

        assert dynamo_helpers.ensure_spotify_connection_from_xomify(EMAIL) is False
        row = tables.Table(USERS_TABLE_NAME).get_item(Key={"email": EMAIL})["Item"]
        assert row["refreshToken"] == "existing"  # untouched

    def test_noop_when_xomify_has_no_connection(self, tables):
        from lambdas.common import dynamo_helpers

        assert dynamo_helpers.ensure_spotify_connection_from_xomify(EMAIL) is False
        assert "Item" not in tables.Table(USERS_TABLE_NAME).get_item(Key={"email": EMAIL})


class TestIngestCreateTriggersSync:
    def _event(self, email=EMAIL):
        return {
            "httpMethod": "POST",
            "headers": {"Authorization": f"Bearer {make_xomify_token(email)}"},
            "body": None,
            "requestContext": {},
        }

    def test_minting_a_token_syncs_the_spotify_connection(self, tables, mock_context):
        from lambdas.ingesttokens_create.handler import handler

        _put_xomify_user(tables, refresh_token="rt-from-mint", user_id="sp-3")

        resp = handler(self._event(), mock_context)
        assert resp["statusCode"] == 200
        assert json.loads(resp["body"])["data"]["token"]  # minted

        # Side effect: the caller is now Spotify-connected in xomtracks.
        row = tables.Table(USERS_TABLE_NAME).get_item(Key={"email": EMAIL})["Item"]
        assert row[SPOTIFY_REFRESH_TOKEN_ATTR] == "rt-from-mint"

    def test_mint_still_succeeds_when_no_xomify_connection(self, tables, mock_context):
        from lambdas.ingesttokens_create.handler import handler

        # No xomify row for this user -> sync no-ops, mint must still work.
        resp = handler(self._event(), mock_context)
        assert resp["statusCode"] == 200
        assert "Item" not in tables.Table(USERS_TABLE_NAME).get_item(Key={"email": EMAIL})
