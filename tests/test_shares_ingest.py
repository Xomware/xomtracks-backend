"""
RED-before-GREEN: POST /shares/ingest -- the extractor push endpoint.

Auth via a scoped SSM bearer key (NOT the user JWT). Idempotent: conditional
put keyed off (messageGuid, sourceUrl) -- reingesting returns "already
exists", not a dupe. Sets matchStatus=pending on first ingest.
"""

import json

import boto3
import pytest
from moto import mock_aws

from lambdas.common.constants import (
    SHARES_TABLE_NAME,
    SHARES_DIRECTION_INDEX,
    SHARES_SHARER_INDEX,
    SHARES_OWNER_DIRECTION_INDEX,
)


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


@pytest.fixture
def ddb_table():
    with mock_aws():
        _create_table()
        yield


VALID_BODY = {
    "messageGuid": "guid-1",
    "direction": "in",
    "sharerHandle": "+13364042196",
    "chatId": "chat-1",
    "platform": "spotify",
    "sourceUrl": "https://open.spotify.com/track/4uLU6hMCjMI75M1A2tKUQC",
    "messageDate": 1753000000,
}


class TestSharesIngestAuth:
    def test_missing_bearer_key_is_401(self, ddb_table, public_event, mock_context):
        from lambdas.shares_ingest.handler import handler

        event = public_event(httpMethod="POST", body=json.dumps(VALID_BODY))
        response = handler(event, mock_context)
        assert response["statusCode"] == 401

    def test_wrong_bearer_key_is_401(self, ddb_table, ingest_event, mock_context):
        from lambdas.shares_ingest.handler import handler

        event = ingest_event(bearer_key="wrong-key", body=json.dumps(VALID_BODY))
        response = handler(event, mock_context)
        assert response["statusCode"] == 401

    def test_correct_bearer_key_is_accepted(self, ddb_table, ingest_event, mock_context):
        from lambdas.shares_ingest.handler import handler

        event = ingest_event(bearer_key="test-ingest-key", body=json.dumps(VALID_BODY))
        response = handler(event, mock_context)
        assert response["statusCode"] == 200


class TestSharesIngestBehavior:
    def test_creates_share_with_pending_status(self, ddb_table, ingest_event, mock_context):
        from lambdas.shares_ingest.handler import handler

        event = ingest_event(bearer_key="test-ingest-key", body=json.dumps(VALID_BODY))
        response = handler(event, mock_context)
        body = json.loads(response["body"])

        assert body["data"]["matchStatus"] == "pending"
        assert body["data"]["created"] is True

    def test_duplicate_ingest_is_idempotent(self, ddb_table, ingest_event, mock_context):
        from lambdas.shares_ingest.handler import handler

        event = ingest_event(bearer_key="test-ingest-key", body=json.dumps(VALID_BODY))
        r1 = handler(event, mock_context)
        r2 = handler(event, mock_context)

        b1 = json.loads(r1["body"])
        b2 = json.loads(r2["body"])
        assert b1["data"]["created"] is True
        assert b2["data"]["created"] is False
        assert b1["data"]["shareId"] == b2["data"]["shareId"]

        import boto3 as _boto3
        table = _boto3.resource("dynamodb", region_name="us-east-1").Table(SHARES_TABLE_NAME)
        assert len(table.scan()["Items"]) == 1

    def test_stamps_default_owner_and_owner_direction(self, ddb_table, ingest_event, mock_context):
        # Phase 1 expand: every new ingest is stamped with DEFAULT_OWNER_ID
        # (Dom) and the derived ownerDirection, so GSI-3 fills going forward.
        from lambdas.shares_ingest.handler import handler
        from lambdas.common.constants import DEFAULT_OWNER_ID

        event = ingest_event(bearer_key="test-ingest-key", body=json.dumps(VALID_BODY))
        response = handler(event, mock_context)
        body = json.loads(response["body"])

        assert body["data"]["ownerId"] == DEFAULT_OWNER_ID
        assert body["data"]["ownerDirection"] == f"{DEFAULT_OWNER_ID}#{VALID_BODY['direction']}"

    def test_invalid_payload_is_400(self, ddb_table, ingest_event, mock_context):
        from lambdas.shares_ingest.handler import handler

        bad_body = dict(VALID_BODY)
        bad_body["platform"] = "tidal"
        event = ingest_event(bearer_key="test-ingest-key", body=json.dumps(bad_body))
        response = handler(event, mock_context)
        assert response["statusCode"] == 400
