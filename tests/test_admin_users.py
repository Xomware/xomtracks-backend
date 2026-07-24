"""
RED-before-GREEN: GET /admin/users -- the user directory (admin-gated).
"""

import json

import boto3
import pytest
from moto import mock_aws

from lambdas.common.constants import USERS_TABLE_NAME

ADMIN = "dominickj.giordano@gmail.com"


def _create_users_table():
    ddb = boto3.resource("dynamodb", region_name="us-east-1")
    ddb.create_table(
        TableName=USERS_TABLE_NAME,
        KeySchema=[{"AttributeName": "email", "KeyType": "HASH"}],
        AttributeDefinitions=[{"AttributeName": "email", "AttributeType": "S"}],
        BillingMode="PAY_PER_REQUEST",
    )
    return ddb.Table(USERS_TABLE_NAME)


@pytest.fixture
def users_table():
    with mock_aws():
        from lambdas.common import user_directory

        user_directory._last_written.clear()
        yield _create_users_table()


class TestAuthGate:
    def test_requires_auth(self, users_table, public_event, mock_context):
        from lambdas.admin_users.handler import handler

        assert handler(public_event(), mock_context)["statusCode"] == 401

    def test_non_admin_forbidden(self, users_table, authorized_event, mock_context):
        from lambdas.admin_users.handler import handler

        resp = handler(authorized_event(email="member@example.com"), mock_context)
        assert resp["statusCode"] == 403


class TestList:
    def test_admin_lists_directory(self, users_table, authorized_event, mock_context):
        from lambdas.admin_users.handler import handler
        from lambdas.common import user_directory

        user_directory.record_seen("member@example.com")

        resp = handler(authorized_event(email=ADMIN), mock_context)
        assert resp["statusCode"] == 200
        data = json.loads(resp["body"])["data"]
        emails = {u["email"] for u in data["users"]}
        # The member we seeded + the admin (auto-upserted on this very call).
        assert "member@example.com" in emails
        assert ADMIN in emails
        row = next(u for u in data["users"] if u["email"] == "member@example.com")
        assert set(row) == {"email", "firstSeen", "lastSeen", "ownIngest", "spotifyConnected"}
