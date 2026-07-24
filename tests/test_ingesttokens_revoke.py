"""
RED-before-GREEN: POST /ingest-tokens/revoke -- revoke a per-user ingest token.

Cognito-authed. Revokes by tokenHash (the non-secret id returned at mint) OR by
presenting the plaintext token. Scoped to the caller's ownerId: a user can only
revoke a token they own -- revoking someone else's (or a nonexistent) token is a
404, and leaves the token live.
"""

import json

import boto3
import pytest
from moto import mock_aws

from lambdas.common.constants import INGEST_TOKENS_TABLE_NAME

SUB_A = "f4e80448-2061-7059-0c26-d0fd91863568"
SUB_B = "aaaabbbb-1111-2222-3333-444455556666"


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


def _authed_event(sub, body):
    return {
        "httpMethod": "POST",
        "headers": {"Content-Type": "application/json"},
        "body": json.dumps(body),
        "requestContext": {"authorizer": {"claims": {"email": "u@example.com", "sub": sub}}},
    }


class TestRevokeIngestToken:
    def test_owner_revokes_by_hash(self, tokens_table, mock_context):
        from lambdas.ingesttokens_revoke.handler import handler
        from lambdas.common import ingest_tokens

        minted = ingest_tokens.mint_token(SUB_A)
        resp = handler(_authed_event(SUB_A, {"tokenHash": minted["tokenHash"]}), mock_context)

        assert resp["statusCode"] == 200
        assert json.loads(resp["body"])["data"]["revoked"] is True
        assert ingest_tokens.resolve_owner(minted["token"]) is None

    def test_owner_revokes_by_presenting_plaintext(self, tokens_table, mock_context):
        from lambdas.ingesttokens_revoke.handler import handler
        from lambdas.common import ingest_tokens

        minted = ingest_tokens.mint_token(SUB_A)
        resp = handler(_authed_event(SUB_A, {"token": minted["token"]}), mock_context)

        assert resp["statusCode"] == 200
        assert ingest_tokens.resolve_owner(minted["token"]) is None

    def test_cannot_revoke_another_owners_token(self, tokens_table, mock_context):
        from lambdas.ingesttokens_revoke.handler import handler
        from lambdas.common import ingest_tokens

        minted = ingest_tokens.mint_token(SUB_A)
        resp = handler(_authed_event(SUB_B, {"tokenHash": minted["tokenHash"]}), mock_context)

        assert resp["statusCode"] == 404
        # Still live for its real owner.
        assert ingest_tokens.resolve_owner(minted["token"]) == SUB_A

    def test_missing_identifier_is_400(self, tokens_table, mock_context):
        from lambdas.ingesttokens_revoke.handler import handler

        resp = handler(_authed_event(SUB_A, {}), mock_context)
        assert resp["statusCode"] == 400

    def test_no_authorizer_context_is_401(self, tokens_table, mock_context):
        from lambdas.ingesttokens_revoke.handler import handler

        event = {"httpMethod": "POST", "headers": {}, "body": json.dumps({"tokenHash": "x"}), "requestContext": {}}
        resp = handler(event, mock_context)
        assert resp["statusCode"] == 401
