"""
RED-before-GREEN: POST /auth/spotify-login (authed) -- mints the Spotify
authorize URL and stamps a CSRF state on the caller's row (Phase 2).
"""

import json
from urllib.parse import parse_qs, urlparse

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
    return ddb.Table(USERS_TABLE_NAME)


class TestSpotifyLoginHandler:
    def test_requires_auth(self, public_event, mock_context):
        from lambdas.auth_spotify_login.handler import handler

        resp = handler(public_event(httpMethod="POST"), mock_context)
        assert resp["statusCode"] == 401

    def test_mints_url_and_persists_state(self, authorized_event, mock_context):
        with mock_aws():
            table = _create_users_table()
            from lambdas.auth_spotify_login.handler import handler

            resp = handler(authorized_event(email="dom@example.com", httpMethod="POST"), mock_context)
            assert resp["statusCode"] == 200
            data = json.loads(resp["body"])["data"]

            # the returned URL carries the same state that got stamped on the row
            q = parse_qs(urlparse(data["authorizeUrl"]).query)
            assert q["state"][0] == data["state"]

            row = table.get_item(Key={"email": "dom@example.com"})["Item"]
            assert row["spotifyAuthState"] == data["state"]
            assert int(row["spotifyAuthStateExp"]) == data["expiresAt"]
            # never leaks a token; only the connect metadata
            assert "refreshToken" not in row
