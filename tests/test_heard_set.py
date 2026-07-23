"""
RED-before-GREEN: POST /heard/set (authed) -- upsert the CALLER's per-song
heard flag. Keyed by (trackKey, raterEmail) in the sibling xomtracks-heard
table, so a member's heard state follows the SONG across all of its share
instances (same identity model as ratings).
"""

import json

import boto3
import pytest
from moto import mock_aws

from lambdas.common.constants import HEARD_TABLE_NAME


def _create_heard_table():
    ddb = boto3.resource("dynamodb", region_name="us-east-1")
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
def heard_table():
    with mock_aws():
        yield _create_heard_table()


class TestHeardSet:
    def test_requires_auth(self, heard_table, public_event, mock_context):
        from lambdas.heard_set.handler import handler

        event = public_event(body=json.dumps({"trackKey": "spotify:abc", "heard": True}))
        assert handler(event, mock_context)["statusCode"] == 401

    def test_marks_heard(self, heard_table, authorized_event, mock_context):
        from lambdas.heard_set.handler import handler

        event = authorized_event(
            email="dom@example.com",
            body=json.dumps({"trackKey": "spotify:abc", "heard": True}),
        )
        resp = handler(event, mock_context)
        assert resp["statusCode"] == 200
        data = json.loads(resp["body"])["data"]
        assert data["trackKey"] == "spotify:abc"
        assert data["heard"] is True
        assert isinstance(data["heardAt"], int)

    def test_toggle_unheard(self, heard_table, authorized_event, mock_context):
        from lambdas.heard_set.handler import handler

        heard_on = authorized_event(
            email="dom@example.com",
            body=json.dumps({"trackKey": "spotify:abc", "heard": True}),
        )
        handler(heard_on, mock_context)

        heard_off = authorized_event(
            email="dom@example.com",
            body=json.dumps({"trackKey": "spotify:abc", "heard": False}),
        )
        data = json.loads(handler(heard_off, mock_context)["body"])["data"]
        assert data["heard"] is False

    def test_missing_track_key_is_400(self, heard_table, authorized_event, mock_context):
        from lambdas.heard_set.handler import handler

        event = authorized_event(email="dom@example.com", body=json.dumps({"heard": True}))
        assert handler(event, mock_context)["statusCode"] == 400

    def test_heard_is_per_caller(self, heard_table, authorized_event, mock_context):
        from lambdas.heard_set.handler import handler
        from lambdas.common.heard_dynamo import caller_heard_map

        event = authorized_event(
            email="dom@example.com",
            body=json.dumps({"trackKey": "spotify:abc", "heard": True}),
        )
        handler(event, mock_context)

        assert caller_heard_map({"spotify:abc"}, "dom@example.com") == {"spotify:abc": True}
        # A different caller has no heard state for the same song.
        assert caller_heard_map({"spotify:abc"}, "sam@example.com") == {}
