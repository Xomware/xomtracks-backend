"""
RED-before-GREEN: GET /admin/calls -- calls & errors dashboard (admin-gated).
"""

import json

import boto3
import pytest
from moto import mock_aws

from lambdas.common.constants import USERS_TABLE_NAME

ADMIN = "dominickj.giordano@gmail.com"
REQUEST_LOG_TABLE = "xomtracks-request-log-test"


def _create_tables():
    ddb = boto3.resource("dynamodb", region_name="us-east-1")
    ddb.create_table(
        TableName=REQUEST_LOG_TABLE,
        KeySchema=[{"AttributeName": "id", "KeyType": "HASH"}],
        AttributeDefinitions=[{"AttributeName": "id", "AttributeType": "S"}],
        BillingMode="PAY_PER_REQUEST",
    )
    ddb.create_table(
        TableName=USERS_TABLE_NAME,
        KeySchema=[{"AttributeName": "email", "KeyType": "HASH"}],
        AttributeDefinitions=[{"AttributeName": "email", "AttributeType": "S"}],
        BillingMode="PAY_PER_REQUEST",
    )


@pytest.fixture
def tables(monkeypatch):
    with mock_aws():
        from lambdas.common import user_directory

        user_directory._last_written.clear()
        _create_tables()
        monkeypatch.setattr("lambdas.common.constants.REQUEST_LOG_TABLE_NAME", REQUEST_LOG_TABLE)
        yield


class TestAuthGate:
    def test_requires_auth(self, tables, public_event, mock_context):
        from lambdas.admin_calls.handler import handler

        assert handler(public_event(), mock_context)["statusCode"] == 401

    def test_non_admin_forbidden(self, tables, authorized_event, mock_context):
        from lambdas.admin_calls.handler import handler

        assert handler(authorized_event(email="member@example.com"), mock_context)["statusCode"] == 403


class TestDashboard:
    def test_aggregates_calls_and_errors(self, tables, authorized_event, mock_context):
        from lambdas.admin_calls.handler import handler
        from lambdas.common import request_log

        request_log.record(path="/shares/list", method="GET", status=200, email="a@example.com")
        request_log.record(path="/me/get", method="GET", status=500, email="a@example.com", error="boom")

        resp = handler(authorized_event(email=ADMIN), mock_context)
        assert resp["statusCode"] == 200
        data = json.loads(resp["body"])["data"]
        # >= because require_admin's own call is also logged by the wrapper hook.
        assert data["totalCalls"] >= 2
        assert data["errorCount"] >= 1
        assert any(e["error"] == "boom" for e in data["recentErrors"])
        assert "byPath" in data and "byStatus" in data
