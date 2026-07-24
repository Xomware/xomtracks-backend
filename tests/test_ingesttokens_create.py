"""
RED-before-GREEN: POST /ingest-tokens/create -- mint a per-user ingest token.

xomify-authed (WS-AUTH). Mints an opaque token bound to the caller's ownerId
(their normalized email from the verified xomify token), stores only its SHA-256
hash, and returns the PLAINTEXT exactly once. A caller with no valid token is
refused (we can't attribute the token to an owner).
"""

import json

import boto3
import pytest
from moto import mock_aws

from conftest import make_xomify_token
from lambdas.common.constants import INGEST_TOKENS_TABLE_NAME

OWNER = "dom@example.com"


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


def _authed_event(email=OWNER, body=None):
    return {
        "httpMethod": "POST",
        "headers": {
            "Content-Type": "application/json",
            "Authorization": f"Bearer {make_xomify_token(email)}",
        },
        "body": body,
        "requestContext": {},
    }


class TestCreateIngestToken:
    def test_mints_and_returns_plaintext_once(self, tokens_table, mock_context):
        from lambdas.ingesttokens_create.handler import handler
        from lambdas.common import ingest_tokens

        resp = handler(_authed_event(), mock_context)
        assert resp["statusCode"] == 200
        data = json.loads(resp["body"])["data"]

        assert data["token"]  # plaintext returned exactly once
        assert data["ownerId"] == OWNER
        assert data["tokenHash"] == ingest_tokens.hash_token(data["token"])

        # Stored row keys on the hash, owns the email, and never holds the plaintext.
        table = boto3.resource("dynamodb", region_name="us-east-1").Table(INGEST_TOKENS_TABLE_NAME)
        row = table.get_item(Key={"tokenHash": data["tokenHash"]})["Item"]
        assert row["ownerId"] == OWNER
        assert data["token"] not in str(row)

    def test_owner_is_lowercased_email(self, tokens_table, mock_context):
        from lambdas.ingesttokens_create.handler import handler

        resp = handler(_authed_event(email="Dom@Example.COM"), mock_context)
        assert resp["statusCode"] == 200
        assert json.loads(resp["body"])["data"]["ownerId"] == "dom@example.com"

    def test_invalid_token_is_401(self, tokens_table, mock_context):
        from lambdas.ingesttokens_create.handler import handler

        event = {
            "httpMethod": "POST",
            "headers": {"Content-Type": "application/json", "Authorization": "Bearer not-a-jwt"},
            "body": None,
            "requestContext": {},
        }
        resp = handler(event, mock_context)
        assert resp["statusCode"] == 401

    def test_no_auth_header_is_401(self, tokens_table, mock_context):
        from lambdas.ingesttokens_create.handler import handler

        event = {"httpMethod": "POST", "headers": {}, "body": None, "requestContext": {}}
        resp = handler(event, mock_context)
        assert resp["statusCode"] == 401
