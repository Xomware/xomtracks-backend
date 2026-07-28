"""
GET /me/playlists (authed) -- the caller's rolling playlists.

Returns {own, baseline}: `baseline` is Dom's (DEFAULT_OWNER_ID) always-visible
pair; `own` is the caller's own pair (null until the cron builds theirs). For
Dom, own == baseline. Ids resolve from the owner's xomtracks-users row
(rollingInPlaylistId / rollingOutPlaylistId), with the service-account SSM
params as Dom's fallback.
"""

import json

import boto3
import pytest
from moto import mock_aws

from lambdas.common.constants import (
    DEFAULT_OWNER_ID,
    ROLLING_IN_PLAYLIST_PARAM,
    ROLLING_OUT_PLAYLIST_PARAM,
    ROLLING_PLAYLIST_NAMES,
    USERS_TABLE_NAME,
)


def _create_users_table():
    ddb = boto3.resource("dynamodb", region_name="us-east-1")
    ddb.create_table(
        TableName=USERS_TABLE_NAME,
        KeySchema=[{"AttributeName": "email", "KeyType": "HASH"}],
        AttributeDefinitions=[{"AttributeName": "email", "AttributeType": "S"}],
        ProvisionedThroughput={"ReadCapacityUnits": 5, "WriteCapacityUnits": 5},
    )
    return ddb


@pytest.fixture
def tables():
    with mock_aws():
        yield _create_users_table()


def _put_row(ddb, email, **attrs):
    ddb.Table(USERS_TABLE_NAME).put_item(Item={"email": email, **attrs})


def _data(resp):
    return json.loads(resp["body"])["data"]


class TestMePlaylists:
    def test_requires_auth(self, tables, public_event, mock_context):
        from lambdas.me_playlists.handler import handler

        assert handler(public_event(), mock_context)["statusCode"] == 401

    def test_dom_own_equals_baseline_from_row(self, tables, authorized_event, mock_context):
        from lambdas.me_playlists.handler import handler

        _put_row(
            tables,
            DEFAULT_OWNER_ID,
            rollingInPlaylistId="pl_in",
            rollingOutPlaylistId="pl_out",
        )

        data = _data(handler(authorized_event(email=DEFAULT_OWNER_ID), mock_context))

        assert data["own"] == data["baseline"]
        assert data["baseline"]["in"]["playlistId"] == "pl_in"
        assert data["baseline"]["in"]["name"] == ROLLING_PLAYLIST_NAMES["in"]
        assert data["baseline"]["in"]["url"] == "https://open.spotify.com/playlist/pl_in"
        assert data["baseline"]["out"]["playlistId"] == "pl_out"
        assert data["baseline"]["out"]["name"] == ROLLING_PLAYLIST_NAMES["out"]

    def test_baseline_falls_back_to_ssm_when_no_dom_row(self, tables, authorized_event, mock_context):
        from lambdas.common import ssm_helpers
        from lambdas.me_playlists.handler import handler

        ssm = boto3.client("ssm", region_name="us-east-1")
        ssm.put_parameter(Name=ROLLING_IN_PLAYLIST_PARAM, Value="ssm_in", Type="SecureString", Overwrite=True)
        ssm.put_parameter(Name=ROLLING_OUT_PLAYLIST_PARAM, Value="ssm_out", Type="SecureString", Overwrite=True)
        # Bypass the pre-seeded cache so the moto-backed values are read.
        ssm_helpers._ssm_cache.pop(ROLLING_IN_PLAYLIST_PARAM, None)
        ssm_helpers._ssm_cache.pop(ROLLING_OUT_PLAYLIST_PARAM, None)

        data = _data(handler(authorized_event(email="friend@example.com"), mock_context))

        assert data["baseline"]["in"]["playlistId"] == "ssm_in"
        assert data["baseline"]["out"]["playlistId"] == "ssm_out"

    def test_friend_own_is_null_until_generated(self, tables, authorized_event, mock_context):
        from lambdas.me_playlists.handler import handler

        _put_row(tables, DEFAULT_OWNER_ID, rollingInPlaylistId="pl_in", rollingOutPlaylistId="pl_out")

        data = _data(handler(authorized_event(email="friend@example.com"), mock_context))

        assert data["own"] == {"in": None, "out": None}
        # Baseline is still Dom's, always visible.
        assert data["baseline"]["in"]["playlistId"] == "pl_in"

    def test_friend_own_populated_from_their_row(self, tables, authorized_event, mock_context):
        from lambdas.me_playlists.handler import handler

        _put_row(tables, DEFAULT_OWNER_ID, rollingInPlaylistId="dom_in", rollingOutPlaylistId="dom_out")
        _put_row(
            tables,
            "friend@example.com",
            rollingInPlaylistId="friend_in",
            rollingOutPlaylistId="friend_out",
        )

        data = _data(handler(authorized_event(email="friend@example.com"), mock_context))

        assert data["own"]["in"]["playlistId"] == "friend_in"
        assert data["own"]["out"]["playlistId"] == "friend_out"
        assert data["baseline"]["in"]["playlistId"] == "dom_in"

    def test_unset_placeholder_is_null(self, tables, authorized_event, mock_context):
        from lambdas.me_playlists.handler import handler

        _put_row(tables, DEFAULT_OWNER_ID, rollingInPlaylistId="unset", rollingOutPlaylistId="unset")

        data = _data(handler(authorized_event(email=DEFAULT_OWNER_ID), mock_context))

        assert data["baseline"] == {"in": None, "out": None}
