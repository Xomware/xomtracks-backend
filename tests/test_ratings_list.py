"""
RED-before-GREEN: GET /ratings/list (authed) -- every track the CALLER has
rated, across BOTH directions, with track info + their rating value. Powers a
true cross-direction "My Rated" (unlike /shares/list, which is scoped to one
direction).
"""

import json
import time

import boto3
import pytest
from moto import mock_aws

from lambdas.common.constants import (
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
    return ddb


@pytest.fixture
def seeded():
    with mock_aws():
        ddb = _create_tables()
        shares = ddb.Table(SHARES_TABLE_NAME)
        now = int(time.time())
        # inbound track (shared with me)
        shares.put_item(Item={
            "shareId": "in1", "messageGuid": "gi1", "direction": "in", "sharerHandle": "+1",
            "platform": "spotify", "sourceUrl": "https://open.spotify.com/track/abc",
            "resolvedSpotifyId": "abc", "trackTitle": "In Song", "trackArtist": "In Artist",
            "albumArtUrl": "https://img/in.jpg", "albumName": "In Album",
            "messageDate": now - 100, "matchStatus": "matched", "createdAt": "x",
        })
        # outbound track (shared by me)
        shares.put_item(Item={
            "shareId": "out1", "messageGuid": "go1", "direction": "out",
            "platform": "spotify", "sourceUrl": "https://open.spotify.com/track/def",
            "resolvedSpotifyId": "def", "trackTitle": "Out Song", "trackArtist": "Out Artist",
            "albumArtUrl": "https://img/out.jpg", "albumName": "Out Album",
            "messageDate": now - 200, "matchStatus": "matched", "createdAt": "x",
        })

        from lambdas.common.ratings_dynamo import set_rating

        # Dom rated one inbound + one outbound track; Sam rated only inbound.
        set_rating("spotify:abc", "dom@example.com", 5)
        set_rating("spotify:def", "dom@example.com", 4)
        set_rating("spotify:abc", "sam@example.com", 2)
        yield ddb


class TestRatingsList:
    def test_requires_auth(self, seeded, public_event, mock_context):
        from lambdas.ratings_list.handler import handler

        assert handler(public_event(), mock_context)["statusCode"] == 401

    def test_returns_callers_rated_across_directions(self, seeded, authorized_event, mock_context):
        from lambdas.ratings_list.handler import handler

        event = authorized_event(email="dom@example.com")
        resp = handler(event, mock_context)
        assert resp["statusCode"] == 200
        data = json.loads(resp["body"])["data"]

        assert data["count"] == 2
        by_key = {r["trackKey"]: r for r in data["rated"]}

        assert by_key["spotify:abc"]["rating"] == 5
        assert by_key["spotify:abc"]["trackTitle"] == "In Song"
        assert by_key["spotify:abc"]["direction"] == "in"
        assert by_key["spotify:abc"]["albumArtUrl"] == "https://img/in.jpg"

        # Cross-direction: the outbound track shows up too.
        assert by_key["spotify:def"]["rating"] == 4
        assert by_key["spotify:def"]["trackTitle"] == "Out Song"
        assert by_key["spotify:def"]["direction"] == "out"

    def test_only_callers_ratings(self, seeded, authorized_event, mock_context):
        from lambdas.ratings_list.handler import handler

        event = authorized_event(email="sam@example.com")
        data = json.loads(handler(event, mock_context)["body"])["data"]
        assert data["count"] == 1
        assert data["rated"][0]["trackKey"] == "spotify:abc"
        assert data["rated"][0]["rating"] == 2
