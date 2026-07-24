"""
RED-before-GREEN: POST /auth/spotify-callback (authed) -- verifies the CSRF
state, exchanges the code, and stores the owner's refresh token on their row
(Phase 2). Spotify token/`/me` edges are patched; DynamoDB is moto.
"""

import json
import time

import boto3
import pytest
from moto import mock_aws

from lambdas.common.constants import USERS_TABLE_NAME

EMAIL = "dom@example.com"
SUB = "f4e80448-2061-7059-0c26-d0fd91863568"


def _create_users_table():
    ddb = boto3.resource("dynamodb", region_name="us-east-1")
    ddb.create_table(
        TableName=USERS_TABLE_NAME,
        KeySchema=[{"AttributeName": "email", "KeyType": "HASH"}],
        AttributeDefinitions=[{"AttributeName": "email", "AttributeType": "S"}],
        ProvisionedThroughput={"ReadCapacityUnits": 5, "WriteCapacityUnits": 5},
    )
    return ddb.Table(USERS_TABLE_NAME)


def _event(body: dict, email: str = EMAIL, sub: str | None = SUB) -> dict:
    claims = {"email": email}
    if sub is not None:
        claims["sub"] = sub
    return {
        "httpMethod": "POST",
        "headers": {"Content-Type": "application/json"},
        "requestContext": {"authorizer": {"claims": claims}},
        "body": json.dumps(body),
        "isBase64Encoded": False,
    }


def _patch_exchange(monkeypatch):
    import lambdas.auth_spotify_callback.handler as H
    monkeypatch.setattr(H, "exchange_code", lambda code, redirect: {"access_token": "AT", "refresh_token": "RT"})
    monkeypatch.setattr(H, "fetch_spotify_user_id", lambda access: "spotify-uid")
    return H


class TestSpotifyCallbackHandler:
    def test_requires_auth(self, public_event, mock_context):
        from lambdas.auth_spotify_callback.handler import handler
        resp = handler(public_event(httpMethod="POST", body=json.dumps({"code": "c", "state": "s"})), mock_context)
        assert resp["statusCode"] == 401

    def test_happy_path_stores_connection(self, monkeypatch, mock_context):
        with mock_aws():
            table = _create_users_table()
            table.put_item(Item={
                "email": EMAIL,
                "spotifyAuthState": "STATE1",
                "spotifyAuthStateExp": int(time.time()) + 300,
            })
            H = _patch_exchange(monkeypatch)

            resp = H.handler(_event({"code": "code123", "state": "STATE1"}), mock_context)
            assert resp["statusCode"] == 200
            data = json.loads(resp["body"])["data"]
            assert data["connected"] is True
            assert data["spotifyUserId"] == "spotify-uid"
            assert data["ownerId"] == SUB
            # refresh token stored on the row, keyed by owner sub, state cleared
            row = table.get_item(Key={"email": EMAIL})["Item"]
            assert row["refreshToken"] == "RT"
            assert row["ownerId"] == SUB
            assert row["spotifyUserId"] == "spotify-uid"
            assert row["userId"] == "spotify-uid"
            assert "spotifyAuthState" not in row
            # the token is never echoed back to the client
            assert "refreshToken" not in data

    def test_state_mismatch_is_401(self, monkeypatch, mock_context):
        with mock_aws():
            table = _create_users_table()
            table.put_item(Item={
                "email": EMAIL,
                "spotifyAuthState": "STATE1",
                "spotifyAuthStateExp": int(time.time()) + 300,
            })
            H = _patch_exchange(monkeypatch)
            resp = H.handler(_event({"code": "c", "state": "WRONG"}), mock_context)
            assert resp["statusCode"] == 401
            # no connection written on a bad state
            row = table.get_item(Key={"email": EMAIL})["Item"]
            assert "refreshToken" not in row

    def test_expired_state_is_401(self, monkeypatch, mock_context):
        with mock_aws():
            table = _create_users_table()
            table.put_item(Item={
                "email": EMAIL,
                "spotifyAuthState": "STATE1",
                "spotifyAuthStateExp": int(time.time()) - 5,  # expired
            })
            H = _patch_exchange(monkeypatch)
            resp = H.handler(_event({"code": "c", "state": "STATE1"}), mock_context)
            assert resp["statusCode"] == 401

    def test_missing_sub_is_401(self, monkeypatch, mock_context):
        with mock_aws():
            _create_users_table()
            H = _patch_exchange(monkeypatch)
            resp = H.handler(_event({"code": "c", "state": "s"}, sub=None), mock_context)
            assert resp["statusCode"] == 401

    def test_redirect_uri_mismatch_is_400(self, monkeypatch, mock_context):
        with mock_aws():
            table = _create_users_table()
            table.put_item(Item={
                "email": EMAIL,
                "spotifyAuthState": "STATE1",
                "spotifyAuthStateExp": int(time.time()) + 300,
            })
            H = _patch_exchange(monkeypatch)
            resp = H.handler(
                _event({"code": "c", "state": "STATE1", "redirectUri": "https://evil.example/cb"}),
                mock_context,
            )
            assert resp["statusCode"] == 400
