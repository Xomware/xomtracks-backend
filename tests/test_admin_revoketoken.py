"""
RED-before-GREEN: POST /admin/revoke-token -- admin override revoke (admin-gated).

Revokes ANY user's token by hash (not owner-scoped, unlike /ingest-tokens/revoke).
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


def _body_event(authorized_event, email, body):
    return authorized_event(email=email, httpMethod="POST", body=json.dumps(body))


class TestAuthGate:
    def test_requires_auth(self, tables, public_event, mock_context):
        from lambdas.admin_revoketoken.handler import handler

        ev = public_event(httpMethod="POST", body=json.dumps({"tokenHash": "x"}))
        assert handler(ev, mock_context)["statusCode"] == 401

    def test_non_admin_forbidden(self, tables, authorized_event, mock_context):
        from lambdas.admin_revoketoken.handler import handler

        ev = _body_event(authorized_event, "member@example.com", {"tokenHash": "x"})
        assert handler(ev, mock_context)["statusCode"] == 403


class TestRevoke:
    def test_admin_revokes_any_owners_token(self, tables, authorized_event, mock_context):
        from lambdas.admin_revoketoken.handler import handler
        from lambdas.common import ingest_tokens

        minted = ingest_tokens.mint_token("someone@example.com")
        ev = _body_event(authorized_event, ADMIN, {"tokenHash": minted["tokenHash"]})
        resp = handler(ev, mock_context)

        assert resp["statusCode"] == 200
        data = json.loads(resp["body"])["data"]
        assert data["revoked"] is True
        assert data["ownerEmail"] == "someone@example.com"
        # Token no longer authenticates ingest.
        assert ingest_tokens.resolve_owner(minted["token"]) is None

    def test_missing_hash_is_404(self, tables, authorized_event, mock_context):
        from lambdas.admin_revoketoken.handler import handler

        ev = _body_event(authorized_event, ADMIN, {"tokenHash": "does-not-exist"})
        assert handler(ev, mock_context)["statusCode"] == 404

    def test_missing_field_is_400(self, tables, authorized_event, mock_context):
        from lambdas.admin_revoketoken.handler import handler

        ev = _body_event(authorized_event, ADMIN, {})
        assert handler(ev, mock_context)["statusCode"] == 400
