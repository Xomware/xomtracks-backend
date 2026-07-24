"""
RED-before-GREEN: GET /admin/tokens -- ingest tokens per owner (admin-gated).

Metadata only -- never the plaintext token (the table holds only hashes).
"""

import json

import boto3
import pytest
from moto import mock_aws

from lambdas.common.constants import INGEST_TOKENS_TABLE_NAME, USERS_TABLE_NAME

ADMIN = "dominickj.giordano@gmail.com"


def _create_tables():
    ddb = boto3.resource("dynamodb", region_name="us-east-1")
    ddb.create_table(
        TableName=INGEST_TOKENS_TABLE_NAME,
        KeySchema=[{"AttributeName": "tokenHash", "KeyType": "HASH"}],
        AttributeDefinitions=[{"AttributeName": "tokenHash", "AttributeType": "S"}],
        BillingMode="PAY_PER_REQUEST",
    )
    ddb.create_table(
        TableName=USERS_TABLE_NAME,
        KeySchema=[{"AttributeName": "email", "KeyType": "HASH"}],
        AttributeDefinitions=[{"AttributeName": "email", "AttributeType": "S"}],
        BillingMode="PAY_PER_REQUEST",
    )


@pytest.fixture
def tables():
    with mock_aws():
        from lambdas.common import user_directory

        user_directory._last_written.clear()
        _create_tables()
        yield


class TestAuthGate:
    def test_requires_auth(self, tables, public_event, mock_context):
        from lambdas.admin_tokens.handler import handler

        assert handler(public_event(), mock_context)["statusCode"] == 401

    def test_non_admin_forbidden(self, tables, authorized_event, mock_context):
        from lambdas.admin_tokens.handler import handler

        assert handler(authorized_event(email="member@example.com"), mock_context)["statusCode"] == 403


class TestList:
    def test_lists_tokens_grouped_by_owner_without_plaintext(self, tables, authorized_event, mock_context):
        from lambdas.admin_tokens.handler import handler
        from lambdas.common import ingest_tokens

        a = ingest_tokens.mint_token("a@example.com", label="laptop")
        ingest_tokens.mint_token("a@example.com", label="desktop")
        ingest_tokens.mint_token("b@example.com")

        resp = handler(authorized_event(email=ADMIN), mock_context)
        assert resp["statusCode"] == 200
        data = json.loads(resp["body"])["data"]

        assert data["count"] == 3
        assert len(data["byOwner"]["a@example.com"]) == 2
        assert len(data["byOwner"]["b@example.com"]) == 1

        # Plaintext token must NEVER appear anywhere in the response.
        assert a["token"] not in resp["body"]
        row = data["byOwner"]["a@example.com"][0]
        assert set(row) == {"ownerEmail", "tokenHash", "label", "createdAt", "lastUsedAt", "revoked"}
