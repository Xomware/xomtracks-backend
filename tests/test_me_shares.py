"""
RED-before-GREEN: GET /me/shares (authed) -- the caller's OWN shares (where
sharerHandle normalizes to one of their linked handles), newest-first, within
a time window. Unlinked callers get an empty list flagged linked=false so the
UI can prompt them to link.
"""

import json
import time

import boto3
import pytest
from moto import mock_aws

from lambdas.common.constants import SHARES_TABLE_NAME, USERS_TABLE_NAME, SHARES_DIRECTION_INDEX, SHARES_SHARER_INDEX


def _create_tables():
    ddb = boto3.resource("dynamodb", region_name="us-east-1")
    ddb.create_table(
        TableName=USERS_TABLE_NAME,
        KeySchema=[{"AttributeName": "email", "KeyType": "HASH"}],
        AttributeDefinitions=[{"AttributeName": "email", "AttributeType": "S"}],
        ProvisionedThroughput={"ReadCapacityUnits": 5, "WriteCapacityUnits": 5},
    )
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
    return boto3.resource("dynamodb", region_name="us-east-1")


@pytest.fixture
def tables():
    with mock_aws():
        ddb = _create_tables()
        shares = ddb.Table(SHARES_TABLE_NAME)
        now = int(time.time())
        # Two of the member's own shares (recent + old) and one from someone else.
        shares.put_item(Item={
            "shareId": "s1", "messageGuid": "g1", "direction": "in",
            "sharerHandle": "+13364042196", "platform": "spotify", "sourceUrl": "u1",
            "messageDate": now - 3600, "matchStatus": "matched", "createdAt": "x",
        })
        shares.put_item(Item={
            "shareId": "s2", "messageGuid": "g2", "direction": "in",
            "sharerHandle": "336.404.2196", "platform": "spotify", "sourceUrl": "u2",
            "messageDate": now - (60 * 24 * 3600), "matchStatus": "matched", "createdAt": "x",
        })
        shares.put_item(Item={
            "shareId": "s3", "messageGuid": "g3", "direction": "in",
            "sharerHandle": "+19998887777", "platform": "spotify", "sourceUrl": "u3",
            "messageDate": now - 3600, "matchStatus": "matched", "createdAt": "x",
        })
        yield ddb


class TestMeShares:
    def test_requires_auth(self, tables, public_event, mock_context):
        from lambdas.me_shares.handler import handler

        assert handler(public_event(), mock_context)["statusCode"] == 401

    def test_unlinked_returns_empty_flagged(self, tables, authorized_event, mock_context):
        from lambdas.me_shares.handler import handler

        resp = handler(authorized_event(email="nobody@example.com"), mock_context)
        data = json.loads(resp["body"])["data"]
        assert data["linked"] is False
        assert data["shares"] == []
        assert data["count"] == 0

    def test_linked_returns_own_shares_newest_first(self, tables, authorized_event, mock_context):
        from lambdas.common.user_links import link_phone
        from lambdas.me_shares.handler import handler

        link_phone("member@example.com", "3364042196")

        event = authorized_event(email="member@example.com", queryStringParameters={"window": "all"})
        resp = handler(event, mock_context)
        data = json.loads(resp["body"])["data"]

        assert data["linked"] is True
        ids = [s["shareId"] for s in data["shares"]]
        assert ids == ["s1", "s2"]  # newest first, excludes s3
        assert data["count"] == 2

    def test_window_narrows_results(self, tables, authorized_event, mock_context):
        from lambdas.common.user_links import link_phone
        from lambdas.me_shares.handler import handler

        link_phone("member@example.com", "3364042196")

        event = authorized_event(email="member@example.com", queryStringParameters={"window": "week"})
        resp = handler(event, mock_context)
        data = json.loads(resp["body"])["data"]
        ids = [s["shareId"] for s in data["shares"]]
        assert ids == ["s1"]  # 60-day-old s2 excluded

    def test_invalid_window_is_400(self, tables, authorized_event, mock_context):
        from lambdas.me_shares.handler import handler

        event = authorized_event(email="member@example.com", queryStringParameters={"window": "decade"})
        assert handler(event, mock_context)["statusCode"] == 400
