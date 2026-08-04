"""
RED-before-GREEN: GET /me/get (authed) -- the caller's link STATE.

Reports linkStatus in {"none","pending","linked"} so the frontend can show the
right UI: no link yet, an approval pending with the admin, or a live link (with
linkedHandles + shareCount). Under the admin-approval model a member is only
"linked" after Dom approves their request.
"""

import json

import boto3
import pytest
from moto import mock_aws

from lambdas.common.constants import (
    ADMIN_EMAIL,
    INGEST_TOKENS_TABLE_NAME,
    LINK_REQUESTS_TABLE_NAME,
    SHARES_DIRECTION_INDEX,
    SHARES_OWNER_DIRECTION_INDEX,
    SHARES_SHARER_INDEX,
    SHARES_TABLE_NAME,
    USERS_TABLE_NAME,
)


def _create_tables():
    ddb = boto3.resource("dynamodb", region_name="us-east-1")
    ddb.create_table(
        TableName=USERS_TABLE_NAME,
        KeySchema=[{"AttributeName": "email", "KeyType": "HASH"}],
        AttributeDefinitions=[{"AttributeName": "email", "AttributeType": "S"}],
        ProvisionedThroughput={"ReadCapacityUnits": 5, "WriteCapacityUnits": 5},
    )
    ddb.create_table(
        TableName=LINK_REQUESTS_TABLE_NAME,
        KeySchema=[{"AttributeName": "requestId", "KeyType": "HASH"}],
        AttributeDefinitions=[{"AttributeName": "requestId", "AttributeType": "S"}],
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
    ddb.create_table(
        TableName=INGEST_TOKENS_TABLE_NAME,
        KeySchema=[{"AttributeName": "tokenHash", "KeyType": "HASH"}],
        AttributeDefinitions=[{"AttributeName": "tokenHash", "AttributeType": "S"}],
        ProvisionedThroughput={"ReadCapacityUnits": 5, "WriteCapacityUnits": 5},
    )
    return boto3.resource("dynamodb", region_name="us-east-1")


@pytest.fixture
def tables():
    with mock_aws():
        ddb = _create_tables()
        shares = ddb.Table(SHARES_TABLE_NAME)
        shares.put_item(Item={
            "shareId": "s1", "messageGuid": "g1", "direction": "in",
            "sharerHandle": "+13364042196", "platform": "spotify", "sourceUrl": "u1",
            "messageDate": 1753000000, "matchStatus": "matched", "createdAt": "x",
        })
        yield ddb


class TestMeGet:
    def test_requires_auth(self, tables, public_event, mock_context):
        from lambdas.me_get.handler import handler

        assert handler(public_event(), mock_context)["statusCode"] == 401

    def test_unlinked_user_reports_status_none(self, tables, authorized_event, mock_context):
        from lambdas.me_get.handler import handler

        resp = handler(authorized_event(email="nobody@example.com"), mock_context)
        data = json.loads(resp["body"])["data"]
        assert data["linkStatus"] == "none"
        assert data["linked"] is False
        assert data["linkedHandles"] == []
        assert data["shareCount"] == 0
        assert data["email"] == "nobody@example.com"

    def test_pending_request_reports_status_pending(self, tables, authorized_event, mock_context):
        from lambdas.common import link_requests
        from lambdas.me_get.handler import handler

        link_requests.create_request("member@example.com", "3364042196", "Big Al")

        resp = handler(authorized_event(email="member@example.com"), mock_context)
        data = json.loads(resp["body"])["data"]
        assert data["linkStatus"] == "pending"
        assert data["linked"] is False
        assert data["linkedHandles"] == []

    def test_linked_user_reports_status_linked(self, tables, authorized_event, mock_context):
        from lambdas.common.user_links import link_phone
        from lambdas.me_get.handler import handler

        link_phone("member@example.com", "3364042196")

        resp = handler(authorized_event(email="member@example.com"), mock_context)
        data = json.loads(resp["body"])["data"]
        assert data["linkStatus"] == "linked"
        assert data["linked"] is True
        assert data["linkedHandles"] == ["3364042196"]
        assert data["shareCount"] == 1

    def test_spotify_connected_false_when_no_connection(self, tables, authorized_event, mock_context):
        from lambdas.me_get.handler import handler

        data = json.loads(handler(authorized_event(email="nobody@example.com"), mock_context)["body"])["data"]
        assert data["spotifyConnected"] is False
        assert data["spotifyUserId"] is None

    def test_spotify_connected_true_when_connection_row_present(self, tables, authorized_event, mock_context):
        from lambdas.common.dynamo_helpers import store_spotify_connection
        from lambdas.me_get.handler import handler

        store_spotify_connection("member@example.com", "owner-sub-1", "refresh-xyz", "spotify-user-9")

        data = json.loads(handler(authorized_event(email="member@example.com"), mock_context)["body"])["data"]
        assert data["spotifyConnected"] is True
        assert data["spotifyUserId"] == "spotify-user-9"
        # The refresh token is NEVER surfaced in the response.
        assert "refresh-xyz" not in resp_str(data)


class TestMeGetAdminAndOwnIngest:
    """WS6: /me/get also reports isAdmin (email == ADMIN_EMAIL) and ownIngest
    (caller holds a live ingest token OR owns any share)."""

    def test_is_admin_true_for_admin_email(self, tables, authorized_event, mock_context):
        from lambdas.me_get.handler import handler

        data = json.loads(handler(authorized_event(email=ADMIN_EMAIL), mock_context)["body"])["data"]
        assert data["isAdmin"] is True

    def test_is_admin_false_for_other_user(self, tables, authorized_event, mock_context):
        from lambdas.me_get.handler import handler

        data = json.loads(handler(authorized_event(email="friend@example.com"), mock_context)["body"])["data"]
        assert data["isAdmin"] is False

    def test_is_admin_case_insensitive(self, tables, authorized_event, mock_context):
        from lambdas.me_get.handler import handler

        data = json.loads(handler(authorized_event(email=ADMIN_EMAIL.upper()), mock_context)["body"])["data"]
        assert data["isAdmin"] is True

    def test_own_ingest_false_when_no_token_or_shares(self, tables, authorized_event, mock_context):
        from lambdas.me_get.handler import handler

        data = json.loads(handler(authorized_event(email="nobody@example.com"), mock_context)["body"])["data"]
        assert data["ownIngest"] is False

    def test_own_ingest_true_when_active_token(self, tables, authorized_event, mock_context):
        from lambdas.common import ingest_tokens
        from lambdas.me_get.handler import handler

        ingest_tokens.mint_token("friend@example.com", label="laptop")

        data = json.loads(handler(authorized_event(email="friend@example.com"), mock_context)["body"])["data"]
        assert data["ownIngest"] is True

    def test_own_ingest_false_when_only_revoked_token(self, tables, authorized_event, mock_context):
        from lambdas.common import ingest_tokens
        from lambdas.me_get.handler import handler

        minted = ingest_tokens.mint_token("revoked@example.com", label="old")
        ingest_tokens.revoke_token("revoked@example.com", minted["tokenHash"])

        data = json.loads(handler(authorized_event(email="revoked@example.com"), mock_context)["body"])["data"]
        assert data["ownIngest"] is False

    def test_own_ingest_true_when_owns_shares(self, tables, authorized_event, mock_context):
        from lambdas.me_get.handler import handler

        owner = "sharer@example.com"
        tables.Table(SHARES_TABLE_NAME).put_item(Item={
            "shareId": "os1", "messageGuid": "og1", "direction": "in", "sharerHandle": "+9",
            "ownerId": owner, "ownerDirection": f"{owner}#in", "platform": "spotify",
            "sourceUrl": "ou1", "messageDate": 1753000000, "matchStatus": "matched", "createdAt": "x",
        })

        data = json.loads(handler(authorized_event(email=owner), mock_context)["body"])["data"]
        assert data["ownIngest"] is True


class TestMeGetLastScanAt:
    """The onboarding "Connected" panel's "last scan" readout: max lastUsedAt
    across the caller's active tokens, ISO 8601, null until the first push."""

    def test_last_scan_at_null_when_no_token(self, tables, authorized_event, mock_context):
        from lambdas.me_get.handler import handler

        data = json.loads(handler(authorized_event(email="nobody@example.com"), mock_context)["body"])["data"]
        assert data["lastScanAt"] is None

    def test_last_scan_at_null_when_token_never_used(self, tables, authorized_event, mock_context):
        from lambdas.common import ingest_tokens
        from lambdas.me_get.handler import handler

        ingest_tokens.mint_token("fresh@example.com", label="new mac")

        data = json.loads(handler(authorized_event(email="fresh@example.com"), mock_context)["body"])["data"]
        assert data["ownIngest"] is True
        assert data["lastScanAt"] is None

    def test_last_scan_at_iso_after_a_push(self, tables, authorized_event, mock_context):
        from lambdas.common import ingest_tokens
        from lambdas.me_get.handler import handler

        minted = ingest_tokens.mint_token("scanner@example.com", label="mbp")
        # A push resolves the token, stamping lastUsedAt.
        ingest_tokens.resolve_owner(minted["token"])

        data = json.loads(handler(authorized_event(email="scanner@example.com"), mock_context)["body"])["data"]
        assert data["lastScanAt"] is not None
        assert data["lastScanAt"].endswith("Z")


def resp_str(data) -> str:
    return json.dumps(data)
