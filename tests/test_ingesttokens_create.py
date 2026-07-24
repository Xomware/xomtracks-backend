"""
RED-before-GREEN: POST /ingest-tokens/create -- mint a per-user ingest token.

Cognito-authed. Mints an opaque token bound to the caller's ownerId (Cognito
sub), stores only its SHA-256 hash, and returns the PLAINTEXT exactly once. A
caller with no Cognito sub is refused (we can't attribute the token to an owner).
"""

import json

import boto3
import pytest
from moto import mock_aws

from lambdas.common.constants import INGEST_TOKENS_TABLE_NAME

SUB = "f4e80448-2061-7059-0c26-d0fd91863568"


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


def _authed_event(sub=SUB, email="dom@example.com", body=None):
    return {
        "httpMethod": "POST",
        "headers": {"Content-Type": "application/json"},
        "body": body,
        "requestContext": {"authorizer": {"claims": {"email": email, "sub": sub}}},
    }


class TestCreateIngestToken:
    def test_mints_and_returns_plaintext_once(self, tokens_table, mock_context):
        from lambdas.ingesttokens_create.handler import handler
        from lambdas.common import ingest_tokens

        resp = handler(_authed_event(), mock_context)
        assert resp["statusCode"] == 200
        data = json.loads(resp["body"])["data"]

        assert data["token"]  # plaintext returned exactly once
        assert data["ownerId"] == SUB
        assert data["tokenHash"] == ingest_tokens.hash_token(data["token"])

        # Stored row keys on the hash, owns SUB, and never holds the plaintext.
        table = boto3.resource("dynamodb", region_name="us-east-1").Table(INGEST_TOKENS_TABLE_NAME)
        row = table.get_item(Key={"tokenHash": data["tokenHash"]})["Item"]
        assert row["ownerId"] == SUB
        assert data["token"] not in str(row)

    def test_missing_caller_sub_is_401(self, tokens_table, mock_context):
        from lambdas.ingesttokens_create.handler import handler

        # Authorizer context present (email) but NO sub -> cannot attribute owner.
        event = {
            "httpMethod": "POST",
            "headers": {"Content-Type": "application/json"},
            "body": None,
            "requestContext": {"authorizer": {"claims": {"email": "x@example.com"}}},
        }
        resp = handler(event, mock_context)
        assert resp["statusCode"] == 401

    def test_no_authorizer_context_is_401(self, tokens_table, mock_context):
        from lambdas.ingesttokens_create.handler import handler

        event = {"httpMethod": "POST", "headers": {}, "body": None, "requestContext": {}}
        resp = handler(event, mock_context)
        assert resp["statusCode"] == 401
