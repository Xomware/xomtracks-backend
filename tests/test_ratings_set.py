"""
RED-before-GREEN: POST /ratings/set (authed) -- upsert the caller's 1-5 rating
for a song; return the fresh aggregate {avg, count, myRating}.
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
        yield _create_ratings_table()


class TestRatingsSet:
    def test_requires_auth(self, ratings_table, public_event, mock_context):
        from lambdas.ratings_set.handler import handler

        event = public_event(httpMethod="POST", body=json.dumps({"trackKey": "spotify:abc", "rating": 4}))
        assert handler(event, mock_context)["statusCode"] == 401

    def test_set_returns_aggregate(self, ratings_table, authorized_event, mock_context):
        from lambdas.ratings_set.handler import handler

        event = authorized_event(
            email="dom@example.com",
            httpMethod="POST",
            body=json.dumps({"trackKey": "spotify:abc", "rating": 5}),
        )
        resp = handler(event, mock_context)
        assert resp["statusCode"] == 200
        data = json.loads(resp["body"])["data"]
        assert data["trackKey"] == "spotify:abc"
        assert data["rating"] == {"avg": 5, "count": 1, "myRating": 5}

    def test_rerate_overwrites(self, ratings_table, authorized_event, mock_context):
        from lambdas.ratings_set.handler import handler

        def _call(rating):
            event = authorized_event(
                email="dom@example.com",
                httpMethod="POST",
                body=json.dumps({"trackKey": "spotify:abc", "rating": rating}),
            )
            return json.loads(handler(event, mock_context)["body"])["data"]

        _call(2)
        data = _call(4)
        assert data["rating"]["count"] == 1
        assert data["rating"]["myRating"] == 4

    def test_invalid_rating_is_400(self, ratings_table, authorized_event, mock_context):
        from lambdas.ratings_set.handler import handler

        event = authorized_event(
            email="dom@example.com",
            httpMethod="POST",
            body=json.dumps({"trackKey": "spotify:abc", "rating": 9}),
        )
        assert handler(event, mock_context)["statusCode"] == 400

    def test_missing_track_key_is_400(self, ratings_table, authorized_event, mock_context):
        from lambdas.ratings_set.handler import handler

        event = authorized_event(
            email="dom@example.com",
            httpMethod="POST",
            body=json.dumps({"rating": 3}),
        )
        assert handler(event, mock_context)["statusCode"] == 400
