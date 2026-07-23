"""
RED-before-GREEN: GET /ratings/get?trackKeys=a,b,c (authed) -- batch aggregate
ratings + the caller's own rating per key; unrated keys return the empty
aggregate so the client can render every requested key.
"""

import json

import boto3
import pytest
from moto import mock_aws

from lambdas.common.constants import RATINGS_TABLE_NAME


def _create_ratings_table():
    ddb = boto3.resource("dynamodb", region_name="us-east-1")
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
def ratings_table():
    with mock_aws():
        ddb = _create_ratings_table()
        from lambdas.common.ratings_dynamo import set_rating

        set_rating("spotify:abc", "dom@example.com", 5)
        set_rating("spotify:abc", "sam@example.com", 3)  # avg 4, count 2
        yield ddb


class TestRatingsGet:
    def test_requires_auth(self, ratings_table, public_event, mock_context):
        from lambdas.ratings_get.handler import handler

        event = public_event(queryStringParameters={"trackKeys": "spotify:abc"})
        assert handler(event, mock_context)["statusCode"] == 401

    def test_batch_returns_map(self, ratings_table, authorized_event, mock_context):
        from lambdas.ratings_get.handler import handler

        event = authorized_event(
            email="dom@example.com",
            queryStringParameters={"trackKeys": "spotify:abc,url:none"},
        )
        resp = handler(event, mock_context)
        assert resp["statusCode"] == 200
        ratings = json.loads(resp["body"])["data"]["ratings"]

        assert ratings["spotify:abc"] == {"avg": 4, "count": 2, "myRating": 5}
        # unrated key still present, empty aggregate
        assert ratings["url:none"] == {"avg": 0, "count": 0, "myRating": None}

    def test_my_rating_is_per_caller(self, ratings_table, authorized_event, mock_context):
        from lambdas.ratings_get.handler import handler

        event = authorized_event(
            email="sam@example.com",
            queryStringParameters={"trackKeys": "spotify:abc"},
        )
        ratings = json.loads(handler(event, mock_context)["body"])["data"]["ratings"]
        assert ratings["spotify:abc"]["myRating"] == 3

    def test_missing_track_keys_is_400(self, ratings_table, authorized_event, mock_context):
        from lambdas.ratings_get.handler import handler

        event = authorized_event(email="dom@example.com", queryStringParameters={})
        assert handler(event, mock_context)["statusCode"] == 400
