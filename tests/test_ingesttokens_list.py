"""
GET /ingest-tokens/list -- list the caller's OWN active ingest tokens.

xomify-authed (WS-AUTH). Scoped to the caller's ownerId (normalized email): a
user only ever sees their own devices. Revoked tokens are excluded. Timestamps
surface as Z-suffixed ISO 8601. Never returns a plaintext token.
"""

import json

import boto3
import pytest
from moto import mock_aws

from conftest import make_xomify_token
from lambdas.common.constants import INGEST_TOKENS_TABLE_NAME

OWNER_A = "a@example.com"
OWNER_B = "b@example.com"


def _create_table():
    ddb = boto3.resource("dynamodb", region_name="us-east-1")
    ddb.create_table(
        TableName=INGEST_TOKENS_TABLE_NAME,
        KeySchema=[{"AttributeName": "tokenHash", "KeyType": "HASH"}],
        AttributeDefinitions=[{"AttributeName": "tokenHash", "AttributeType": "S"}],
        BillingMode="PAY_PER_REQUEST",
    )


@pytest.fixture
def tokens_table():
    with mock_aws():
        _create_table()
        yield


def _authed_event(email):
    return {
        "httpMethod": "GET",
        "headers": {"Authorization": f"Bearer {make_xomify_token(email)}"},
        "requestContext": {},
    }


def _data(resp):
    return json.loads(resp["body"])["data"]


class TestListIngestTokens:
    def test_lists_only_the_callers_active_tokens(self, tokens_table, mock_context):
        from lambdas.ingesttokens_list.handler import handler
        from lambdas.common import ingest_tokens

        ingest_tokens.mint_token(OWNER_A, label="MacBook Pro")
        ingest_tokens.mint_token(OWNER_A, label="Mac mini")
        ingest_tokens.mint_token(OWNER_B, label="Someone else")

        resp = handler(_authed_event(OWNER_A), mock_context)
        assert resp["statusCode"] == 200

        devices = _data(resp)["devices"]
        assert len(devices) == 2
        labels = {d["label"] for d in devices}
        assert labels == {"MacBook Pro", "Mac mini"}

    def test_excludes_revoked_tokens(self, tokens_table, mock_context):
        from lambdas.ingesttokens_list.handler import handler
        from lambdas.common import ingest_tokens

        keep = ingest_tokens.mint_token(OWNER_A, label="Keep")
        gone = ingest_tokens.mint_token(OWNER_A, label="Gone")
        ingest_tokens.revoke_token(OWNER_A, gone["tokenHash"])

        devices = _data(handler(_authed_event(OWNER_A), mock_context))["devices"]
        assert [d["label"] for d in devices] == ["Keep"]
        assert devices[0]["tokenHash"] == keep["tokenHash"]

    def test_never_returns_plaintext_token(self, tokens_table, mock_context):
        from lambdas.ingesttokens_list.handler import handler
        from lambdas.common import ingest_tokens

        minted = ingest_tokens.mint_token(OWNER_A)
        devices = _data(handler(_authed_event(OWNER_A), mock_context))["devices"]
        assert minted["token"] not in json.dumps(devices)
        assert "token" not in devices[0]  # only tokenHash, never the secret

    def test_last_scan_at_is_iso_after_a_push_and_null_before(self, tokens_table, mock_context):
        from lambdas.ingesttokens_list.handler import handler
        from lambdas.common import ingest_tokens

        minted = ingest_tokens.mint_token(OWNER_A)

        # Never used yet -> lastScanAt is null.
        before = _data(handler(_authed_event(OWNER_A), mock_context))["devices"][0]
        assert before["lastScanAt"] is None
        assert before["createdAt"] is not None  # ISO string

        # A push resolves the token, stamping lastUsedAt -> surfaces as ISO.
        ingest_tokens.resolve_owner(minted["token"])
        after = _data(handler(_authed_event(OWNER_A), mock_context))["devices"][0]
        assert after["lastScanAt"] is not None
        assert after["lastScanAt"].endswith("Z")

    def test_empty_when_no_tokens(self, tokens_table, mock_context):
        from lambdas.ingesttokens_list.handler import handler

        assert _data(handler(_authed_event(OWNER_A), mock_context))["devices"] == []

    def test_no_auth_header_is_401(self, tokens_table, mock_context):
        from lambdas.ingesttokens_list.handler import handler

        resp = handler({"httpMethod": "GET", "headers": {}, "requestContext": {}}, mock_context)
        assert resp["statusCode"] == 401
