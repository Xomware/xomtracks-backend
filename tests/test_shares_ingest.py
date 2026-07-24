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


# ---------------------------------------------------------------------------
# Phase 3: per-user ingest tokens + dual-accept of the legacy SSM bearer key.
# resolve_ingest_owner hashes the presented bearer -> owner. The legacy SSM key
# is dual-accepted and maps to DEFAULT_OWNER_ID (Dom), so his running extractor
# keeps working unchanged. A per-user token stamps ITS owner. Neither -> 401.
# ---------------------------------------------------------------------------

from lambdas.common.constants import INGEST_TOKENS_TABLE_NAME  # noqa: E402


def _create_tokens_table():
    ddb = boto3.resource("dynamodb", region_name="us-east-1")
    ddb.create_table(
        TableName=INGEST_TOKENS_TABLE_NAME,
        KeySchema=[{"AttributeName": "tokenHash", "KeyType": "HASH"}],
        AttributeDefinitions=[{"AttributeName": "tokenHash", "AttributeType": "S"}],
        BillingMode="PAY_PER_REQUEST",
    )


@pytest.fixture
def ddb_shares_and_tokens():
    with mock_aws():
        _create_table()
        _create_tokens_table()
        yield


class TestSharesIngestPerUserTokens:
    def test_legacy_ssm_key_still_maps_to_default_owner(self, ddb_shares_and_tokens, ingest_event, mock_context):
        # Dual-accept: the legacy SSM bearer key resolves to DEFAULT_OWNER_ID.
        from lambdas.shares_ingest.handler import handler
        from lambdas.common.constants import DEFAULT_OWNER_ID

        event = ingest_event(bearer_key="test-ingest-key", body=json.dumps(VALID_BODY))
        response = handler(event, mock_context)
        body = json.loads(response["body"])
        assert response["statusCode"] == 200
        assert body["data"]["ownerId"] == DEFAULT_OWNER_ID

    def test_per_user_token_stamps_that_owner(self, ddb_shares_and_tokens, ingest_event, mock_context):
        from lambdas.shares_ingest.handler import handler
        from lambdas.common import ingest_tokens

        owner = "aaaabbbb-1111-2222-3333-444455556666"
        token = ingest_tokens.mint_token(owner)["token"]

        event = ingest_event(bearer_key=token, body=json.dumps(VALID_BODY))
        response = handler(event, mock_context)
        body = json.loads(response["body"])

        assert response["statusCode"] == 200
        assert body["data"]["ownerId"] == owner
        assert body["data"]["ownerDirection"] == f"{owner}#{VALID_BODY['direction']}"

    def test_unknown_token_is_401(self, ddb_shares_and_tokens, ingest_event, mock_context):
        from lambdas.shares_ingest.handler import handler

        event = ingest_event(bearer_key="not-a-real-token", body=json.dumps(VALID_BODY))
        response = handler(event, mock_context)
        assert response["statusCode"] == 401

    def test_revoked_token_is_401(self, ddb_shares_and_tokens, ingest_event, mock_context):
        from lambdas.shares_ingest.handler import handler
        from lambdas.common import ingest_tokens

        owner = "aaaabbbb-1111-2222-3333-444455556666"
        minted = ingest_tokens.mint_token(owner)
        ingest_tokens.revoke_token(owner, minted["tokenHash"])

        event = ingest_event(bearer_key=minted["token"], body=json.dumps(VALID_BODY))
        response = handler(event, mock_context)
        assert response["statusCode"] == 401
