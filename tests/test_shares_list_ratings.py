"""
RED-before-GREEN: GET /shares/list is enriched so each share carries
`trackKey` + `rating` {avg, count, myRating} for its SONG -- the feed shows
whole-group ratings without N extra calls.
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
        # Two shares of the SAME spotify track + one unrated soundcloud share.
        shares.put_item(Item={
            "shareId": "s1", "messageGuid": "g1", "direction": "in", "sharerHandle": "+1",
            "platform": "spotify", "sourceUrl": "https://open.spotify.com/track/abc",
            "resolvedSpotifyId": "abc", "messageDate": now - 100, "matchStatus": "matched", "createdAt": "x",
        })
        shares.put_item(Item={
            "shareId": "s2", "messageGuid": "g2", "direction": "in", "sharerHandle": "+1",
            "platform": "apple", "sourceUrl": "https://music.apple.com/song",
            "resolvedSpotifyId": "abc", "messageDate": now - 200, "matchStatus": "matched", "createdAt": "x",
        })
        shares.put_item(Item={
            "shareId": "s3", "messageGuid": "g3", "direction": "in", "sharerHandle": "+1",
            "platform": "soundcloud", "sourceUrl": "https://soundcloud.com/x/y",
            "messageDate": now - 300, "matchStatus": "pending", "createdAt": "x",
        })

        from lambdas.common.ratings_dynamo import set_rating

        set_rating("spotify:abc", "dom@example.com", 5)
        set_rating("spotify:abc", "sam@example.com", 3)  # avg 4, count 2
        yield ddb


class TestSharesListRatings:
    def test_each_share_carries_rating(self, seeded, authorized_event, mock_context):
        from lambdas.shares_list.handler import handler

        event = authorized_event(email="dom@example.com", queryStringParameters={"direction": "in"})
        resp = handler(event, mock_context)
        shares = {s["shareId"]: s for s in json.loads(resp["body"])["data"]["shares"]}

        # Both spotify-resolved shares share the aggregate for spotify:abc.
        assert shares["s1"]["trackKey"] == "spotify:abc"
        assert shares["s1"]["rating"] == {"avg": 4, "count": 2, "myRating": 5}
        assert shares["s2"]["rating"] == {"avg": 4, "count": 2, "myRating": 5}

        # Unrated soundcloud share -> url key + empty aggregate.
        assert shares["s3"]["trackKey"] == "url:soundcloud.com/x/y"
        assert shares["s3"]["rating"] == {"avg": 0, "count": 0, "myRating": None}

    def test_my_rating_is_per_caller(self, seeded, authorized_event, mock_context):
        from lambdas.shares_list.handler import handler

        event = authorized_event(email="stranger@example.com", queryStringParameters={"direction": "in"})
        resp = handler(event, mock_context)
        shares = {s["shareId"]: s for s in json.loads(resp["body"])["data"]["shares"]}
        assert shares["s1"]["rating"]["myRating"] is None
        assert shares["s1"]["rating"]["count"] == 2
