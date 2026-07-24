"""
RED-before-GREEN: GET /shares -- query by direction + time window
(week/month/6mo/all) via GSI-1. Authed route (xomify HS256 token, validated
in-handler -- see conftest.authorized_event).
"""

import json
import time

import boto3
import pytest
from moto import mock_aws

from conftest import make_xomify_token
from lambdas.common.constants import (
    SHARES_TABLE_NAME,
    SHARES_DIRECTION_INDEX,
    SHARES_SHARER_INDEX,
    SHARES_OWNER_DIRECTION_INDEX,
)

# WS-AUTH: ownerId is the caller's normalized email, not a Cognito sub.
DOM_OWNER = "dom@example.com"


def _create_table():
    ddb = boto3.resource("dynamodb", region_name="us-east-1")
    ddb.create_table(
        TableName=SHARES_TABLE_NAME,
        KeySchema=[{"AttributeName": "shareId", "KeyType": "HASH"}],
        AttributeDefinitions=[
            {"AttributeName": "shareId", "AttributeType": "S"},
            {"AttributeName": "direction", "AttributeType": "S"},
            {"AttributeName": "messageDate", "AttributeType": "N"},
            {"AttributeName": "sharerHandle", "AttributeType": "S"},
            {"AttributeName": "ownerDirection", "AttributeType": "S"},
        ],
        GlobalSecondaryIndexes=[
            {
                "IndexName": SHARES_DIRECTION_INDEX,
                "KeySchema": [
                    {"AttributeName": "direction", "KeyType": "HASH"},
                    {"AttributeName": "messageDate", "KeyType": "RANGE"},
                ],
                "Projection": {"ProjectionType": "ALL"},
                "ProvisionedThroughput": {"ReadCapacityUnits": 5, "WriteCapacityUnits": 5},
            },
            {
                "IndexName": SHARES_SHARER_INDEX,
                "KeySchema": [
                    {"AttributeName": "sharerHandle", "KeyType": "HASH"},
                    {"AttributeName": "messageDate", "KeyType": "RANGE"},
                ],
                "Projection": {"ProjectionType": "ALL"},
                "ProvisionedThroughput": {"ReadCapacityUnits": 5, "WriteCapacityUnits": 5},
            },
            {
                "IndexName": SHARES_OWNER_DIRECTION_INDEX,
                "KeySchema": [
                    {"AttributeName": "ownerDirection", "KeyType": "HASH"},
                    {"AttributeName": "messageDate", "KeyType": "RANGE"},
                ],
                "Projection": {"ProjectionType": "ALL"},
                "ProvisionedThroughput": {"ReadCapacityUnits": 5, "WriteCapacityUnits": 5},
            },
        ],
        ProvisionedThroughput={"ReadCapacityUnits": 5, "WriteCapacityUnits": 5},
    )
    return boto3.resource("dynamodb", region_name="us-east-1").Table(SHARES_TABLE_NAME)


@pytest.fixture
def seeded_table():
    with mock_aws():
        table = _create_table()
        now = int(time.time())
        # in-direction, one within the last week, one from 60 days ago
        table.put_item(Item={
            "shareId": "s1", "messageGuid": "g1", "direction": "in", "sharerHandle": "+1",
            "chatId": "c1", "platform": "spotify", "sourceUrl": "url1",
            "messageDate": now - 3600, "matchStatus": "matched", "createdAt": "x",
        })
        table.put_item(Item={
            "shareId": "s2", "messageGuid": "g2", "direction": "in", "sharerHandle": "+1",
            "chatId": "c1", "platform": "spotify", "sourceUrl": "url2",
            "messageDate": now - (60 * 24 * 3600), "matchStatus": "matched", "createdAt": "x",
        })
        # "out" shares have no sharerHandle attribute at all (Dom is the
        # sender) -- DynamoDB rejects NULL for a GSI key attribute, so
        # production code omits it entirely rather than setting None.
        table.put_item(Item={
            "shareId": "s3", "messageGuid": "g3", "direction": "out",
            "chatId": "c1", "platform": "spotify", "sourceUrl": "url3",
            "messageDate": now - 3600, "matchStatus": "matched", "createdAt": "x",
        })
        yield table


@pytest.fixture
def owned_table():
    """Same shape as seeded_table but every row is owner-stamped for Dom
    (ownerId + ownerDirection) -- the post-backfill state used to exercise the
    Phase 1C owner-scoping flag + parity."""
    with mock_aws():
        table = _create_table()
        now = int(time.time())
        rows = [
            {"shareId": "s1", "messageGuid": "g1", "direction": "in", "sharerHandle": "+1",
             "sourceUrl": "url1", "messageDate": now - 3600},
            {"shareId": "s2", "messageGuid": "g2", "direction": "in", "sharerHandle": "+1",
             "sourceUrl": "url2", "messageDate": now - (60 * 24 * 3600)},
            {"shareId": "s3", "messageGuid": "g3", "direction": "out",
             "sourceUrl": "url3", "messageDate": now - 3600},
        ]
        for r in rows:
            r.update({
                "ownerId": DOM_OWNER,
                "ownerDirection": f"{DOM_OWNER}#{r['direction']}",
                "chatId": "c1", "platform": "spotify",
                "matchStatus": "matched", "createdAt": "x",
            })
            table.put_item(Item=r)
        yield table


def _authed(email: str, **qs) -> dict:
    """Event carrying a valid xomify token for `email` (the caller's ownerId)."""
    return {
        "httpMethod": "GET", "path": "/shares",
        "headers": {"Authorization": f"Bearer {make_xomify_token(email)}"},
        "body": None, "isBase64Encoded": False,
        "queryStringParameters": qs or None,
        "requestContext": {},
    }


class TestSharesListOwnerScoping:
    """Phase 1C read cutover -- flag-gated, GSI-1 legacy path is the rollback."""

    def test_flag_on_scopes_to_caller_owner(self, owned_table, monkeypatch, mock_context):
        import lambdas.shares_list.handler as h

        monkeypatch.setattr(h, "OWNER_SCOPING_ENABLED", True)
        event = _authed(DOM_OWNER, direction="in", window="all")
        response = h.handler(event, mock_context)
        body = json.loads(response["body"])

        assert {s["messageGuid"] for s in body["data"]["shares"]} == {"g1", "g2"}

    def test_flag_on_parity_with_legacy_for_owner(self, owned_table, monkeypatch, mock_context):
        """The load-bearing guarantee: with the flag ON, Dom's feed is IDENTICAL
        to the legacy GSI-1 path with the flag OFF."""
        import lambdas.shares_list.handler as h

        monkeypatch.setattr(h, "OWNER_SCOPING_ENABLED", False)
        legacy = json.loads(
            h.handler(_authed(DOM_OWNER, direction="in", window="all"),
                      mock_context)["body"]
        )["data"]["shares"]

        monkeypatch.setattr(h, "OWNER_SCOPING_ENABLED", True)
        owned = json.loads(
            h.handler(_authed(DOM_OWNER, direction="in", window="all"),
                      mock_context)["body"]
        )["data"]["shares"]

        assert {s["shareId"] for s in owned} == {s["shareId"] for s in legacy}

    def test_flag_on_second_user_gets_empty_feed(self, owned_table, monkeypatch, mock_context):
        import lambdas.shares_list.handler as h

        monkeypatch.setattr(h, "OWNER_SCOPING_ENABLED", True)
        event = _authed("friend@example.com", direction="in", window="all")
        response = h.handler(event, mock_context)
        body = json.loads(response["body"])

        assert body["data"]["shares"] == []

    def test_flag_off_uses_legacy_path_for_everyone(self, owned_table, monkeypatch, mock_context):
        # Flag OFF: even a different caller sub sees the global direction feed
        # (Dom-only pre-multi-tenant behavior) -- the instant-rollback path.
        import lambdas.shares_list.handler as h

        monkeypatch.setattr(h, "OWNER_SCOPING_ENABLED", False)
        event = _authed("friend@example.com", direction="in", window="all")
        response = h.handler(event, mock_context)
        body = json.loads(response["body"])

        assert {s["messageGuid"] for s in body["data"]["shares"]} == {"g1", "g2"}


class TestSharesListAuth:
    def test_no_authorizer_context_is_401(self, seeded_table, public_event, mock_context):
        from lambdas.shares_list.handler import handler

        event = public_event(queryStringParameters={"direction": "in", "window": "week"})
        response = handler(event, mock_context)
        assert response["statusCode"] == 401


class TestSharesListQuery:
    def test_default_window_is_all(self, seeded_table, authorized_event, mock_context):
        from lambdas.shares_list.handler import handler

        event = authorized_event(queryStringParameters={"direction": "in"})
        response = handler(event, mock_context)
        body = json.loads(response["body"])

        guids = {s["messageGuid"] for s in body["data"]["shares"]}
        assert guids == {"g1", "g2"}

    def test_week_window_excludes_old_share(self, seeded_table, authorized_event, mock_context):
        from lambdas.shares_list.handler import handler

        event = authorized_event(queryStringParameters={"direction": "in", "window": "week"})
        response = handler(event, mock_context)
        body = json.loads(response["body"])

        guids = {s["messageGuid"] for s in body["data"]["shares"]}
        assert guids == {"g1"}

    def test_direction_out_only_returns_out(self, seeded_table, authorized_event, mock_context):
        from lambdas.shares_list.handler import handler

        event = authorized_event(queryStringParameters={"direction": "out", "window": "all"})
        response = handler(event, mock_context)
        body = json.loads(response["body"])

        guids = {s["messageGuid"] for s in body["data"]["shares"]}
        assert guids == {"g3"}

    def test_missing_direction_is_400(self, seeded_table, authorized_event, mock_context):
        from lambdas.shares_list.handler import handler

        event = authorized_event(queryStringParameters={})
        response = handler(event, mock_context)
        assert response["statusCode"] == 400

    def test_invalid_window_is_400(self, seeded_table, authorized_event, mock_context):
        from lambdas.shares_list.handler import handler

        event = authorized_event(queryStringParameters={"direction": "in", "window": "decade"})
        response = handler(event, mock_context)
        assert response["statusCode"] == 400


class TestSharesListGenres:
    """Every returned share must carry `genres` as a string[] so the frontend
    genre filter can read it unconditionally -- stored genres pass through,
    historical shares default to []."""

    def test_all_shares_have_genres_list(self, seeded_table, authorized_event, mock_context):
        from lambdas.shares_list.handler import handler

        event = authorized_event(queryStringParameters={"direction": "in", "window": "all"})
        response = handler(event, mock_context)
        body = json.loads(response["body"])

        shares = body["data"]["shares"]
        assert shares
        assert all(isinstance(s["genres"], list) for s in shares)

    def test_stored_genres_surface_in_response(self, seeded_table, authorized_event, mock_context):
        from lambdas.shares_list.handler import handler

        seeded_table.put_item(Item={
            "shareId": "s9", "messageGuid": "g9", "direction": "in", "sharerHandle": "+1",
            "chatId": "c1", "platform": "spotify", "sourceUrl": "url9",
            "messageDate": int(time.time()) - 60, "matchStatus": "matched", "createdAt": "x",
            "genres": ["indie rock", "art pop"],
        })

        event = authorized_event(queryStringParameters={"direction": "in", "window": "all"})
        response = handler(event, mock_context)
        body = json.loads(response["body"])

        by_id = {s["shareId"]: s for s in body["data"]["shares"]}
        assert by_id["s9"]["genres"] == ["indie rock", "art pop"]
        # A share with no stored genres still exposes an empty list.
        assert by_id["s1"]["genres"] == []
