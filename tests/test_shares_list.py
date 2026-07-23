"""
RED-before-GREEN: GET /shares -- query by direction + time window
(week/month/6mo/all) via GSI-1. Authed route (per-user JWT, gated by the
custom authorizer -- see conftest.authorized_event).
"""

import json
import time

import boto3
import pytest
from moto import mock_aws

from lambdas.common.constants import SHARES_TABLE_NAME, SHARES_DIRECTION_INDEX, SHARES_SHARER_INDEX


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
