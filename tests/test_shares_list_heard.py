"""
RED-before-GREEN: GET /shares/list + GET /me/shares carry `share.heard` (the
CALLER's per-song heard state, default False) alongside the rating enrichment,
so the frontend can offer an "unheard" filter.
"""

import json
import time

import boto3
import pytest
from moto import mock_aws

from lambdas.common.constants import (
    HEARD_TABLE_NAME,
    RATINGS_TABLE_NAME,
    SHARES_DIRECTION_INDEX,
    SHARES_SHARER_INDEX,
    SHARES_TABLE_NAME,
)


def _create_tables():
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
    ddb.create_table(
        TableName=RATINGS_TABLE_NAME,
        KeySchema=[
            {"AttributeName": "trackKey", "KeyType": "HASH"},
            {"AttributeName": "raterEmail", "KeyType": "RANGE"},
        ],
        AttributeDefinitions=[
            {"AttributeName": "trackKey", "AttributeType": "S"},
            {"AttributeName": "raterEmail", "AttributeType": "S"},
        ],
        ProvisionedThroughput={"ReadCapacityUnits": 5, "WriteCapacityUnits": 5},
    )
    ddb.create_table(
        TableName=HEARD_TABLE_NAME,
        KeySchema=[
            {"AttributeName": "trackKey", "KeyType": "HASH"},
            {"AttributeName": "raterEmail", "KeyType": "RANGE"},
        ],
        AttributeDefinitions=[
            {"AttributeName": "trackKey", "AttributeType": "S"},
            {"AttributeName": "raterEmail", "AttributeType": "S"},
        ],
        ProvisionedThroughput={"ReadCapacityUnits": 5, "WriteCapacityUnits": 5},
    )
    return ddb


@pytest.fixture
def seeded():
    with mock_aws():
        ddb = _create_tables()
        shares = ddb.Table(SHARES_TABLE_NAME)
        now = int(time.time())
        shares.put_item(Item={
            "shareId": "s1", "messageGuid": "g1", "direction": "in", "sharerHandle": "+1",
            "platform": "spotify", "sourceUrl": "https://open.spotify.com/track/abc",
            "resolvedSpotifyId": "abc", "messageDate": now - 100, "matchStatus": "matched", "createdAt": "x",
        })
        shares.put_item(Item={
            "shareId": "s2", "messageGuid": "g2", "direction": "in", "sharerHandle": "+1",
            "platform": "soundcloud", "sourceUrl": "https://soundcloud.com/x/y",
            "messageDate": now - 300, "matchStatus": "pending", "createdAt": "x",
        })
        yield ddb


class TestSharesListHeard:
    def test_default_unheard(self, seeded, authorized_event, mock_context):
        from lambdas.shares_list.handler import handler

        event = authorized_event(email="dom@example.com", queryStringParameters={"direction": "in"})
        shares = {s["shareId"]: s for s in json.loads(handler(event, mock_context)["body"])["data"]["shares"]}
        assert shares["s1"]["heard"] is False
        assert shares["s2"]["heard"] is False

    def test_heard_reflects_caller_state(self, seeded, authorized_event, mock_context):
        from lambdas.shares_list.handler import handler
        from lambdas.common.heard_dynamo import set_heard

        # Dom heard spotify:abc (the track behind s1); Sam did not.
        set_heard("spotify:abc", "dom@example.com", True)

        dom = authorized_event(email="dom@example.com", queryStringParameters={"direction": "in"})
        dom_shares = {s["shareId"]: s for s in json.loads(handler(dom, mock_context)["body"])["data"]["shares"]}
        assert dom_shares["s1"]["heard"] is True
        assert dom_shares["s2"]["heard"] is False

        sam = authorized_event(email="sam@example.com", queryStringParameters={"direction": "in"})
        sam_shares = {s["shareId"]: s for s in json.loads(handler(sam, mock_context)["body"])["data"]["shares"]}
        assert sam_shares["s1"]["heard"] is False
