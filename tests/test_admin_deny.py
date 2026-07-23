"""
RED-before-GREEN: POST /admin/deny (admin-gated).

Denying a pending request marks it denied and creates NO link. Admin-gated:
non-admin callers get 403.
"""

import json

import boto3
import pytest
from moto import mock_aws

from lambdas.common.constants import LINK_REQUESTS_TABLE_NAME, USERS_TABLE_NAME


def _create_tables():
    ddb = boto3.resource("dynamodb", region_name="us-east-1")
    ddb.create_table(
        TableName=LINK_REQUESTS_TABLE_NAME,
        KeySchema=[{"AttributeName": "requestId", "KeyType": "HASH"}],
        AttributeDefinitions=[{"AttributeName": "requestId", "AttributeType": "S"}],
        ProvisionedThroughput={"ReadCapacityUnits": 5, "WriteCapacityUnits": 5},
    )
    ddb.create_table(
        TableName=USERS_TABLE_NAME,
        KeySchema=[{"AttributeName": "email", "KeyType": "HASH"}],
        AttributeDefinitions=[{"AttributeName": "email", "AttributeType": "S"}],
        ProvisionedThroughput={"ReadCapacityUnits": 5, "WriteCapacityUnits": 5},
    )
    return ddb


@pytest.fixture
def tables():
    with mock_aws():
        yield _create_tables()


def _admin(authorized_event, body):
    return authorized_event(email="dominickj.giordano@gmail.com", body=json.dumps(body))


class TestAuthAndGate:
    def test_requires_auth(self, tables, public_event, mock_context):
        from lambdas.admin_deny.handler import handler

        resp = handler(public_event(body=json.dumps({"requestId": "x"})), mock_context)
        assert resp["statusCode"] == 401

    def test_non_admin_is_forbidden(self, tables, authorized_event, mock_context):
        from lambdas.admin_deny.handler import handler

        resp = handler(
            authorized_event(email="member@example.com", body=json.dumps({"requestId": "x"})),
            mock_context,
        )
        assert resp["statusCode"] == 403


class TestDeny:
    def test_deny_marks_denied_and_creates_no_link(self, tables, authorized_event, mock_context):
        from lambdas.admin_deny.handler import handler
        from lambdas.common import link_requests

        req = link_requests.create_request("member@example.com", "3364042196", None)

        resp = handler(_admin(authorized_event, {"requestId": req["requestId"]}), mock_context)
        assert resp["statusCode"] == 200
        data = json.loads(resp["body"])["data"]
        assert data["status"] == "denied"

        users = tables.Table(USERS_TABLE_NAME)
        assert "Item" not in users.get_item(Key={"email": "member@example.com"})

        assert link_requests.get_request(req["requestId"])["status"] == "denied"

    def test_missing_request_is_404(self, tables, authorized_event, mock_context):
        from lambdas.admin_deny.handler import handler

        resp = handler(_admin(authorized_event, {"requestId": "nope"}), mock_context)
        assert resp["statusCode"] == 404

    def test_missing_request_id_is_400(self, tables, authorized_event, mock_context):
        from lambdas.admin_deny.handler import handler

        resp = handler(_admin(authorized_event, {}), mock_context)
        assert resp["statusCode"] == 400
