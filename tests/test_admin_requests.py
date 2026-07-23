"""
RED-before-GREEN: GET /admin/requests (admin-gated).

Lists PENDING link requests for the admin portal. Cognito-authed AND gated to
the admin (Dom): the caller's Cognito email must equal ADMIN_EMAIL, else 403.
"""

import json

import boto3
import pytest
from moto import mock_aws

from lambdas.common.constants import LINK_REQUESTS_TABLE_NAME


def _create_table():
    ddb = boto3.resource("dynamodb", region_name="us-east-1")
    ddb.create_table(
        TableName=LINK_REQUESTS_TABLE_NAME,
        KeySchema=[{"AttributeName": "requestId", "KeyType": "HASH"}],
        AttributeDefinitions=[{"AttributeName": "requestId", "AttributeType": "S"}],
        ProvisionedThroughput={"ReadCapacityUnits": 5, "WriteCapacityUnits": 5},
    )
    return ddb


@pytest.fixture
def table():
    with mock_aws():
        yield _create_table()


class TestAuthAndGate:
    def test_requires_auth(self, table, public_event, mock_context):
        from lambdas.admin_requests.handler import handler

        assert handler(public_event(), mock_context)["statusCode"] == 401

    def test_non_admin_is_forbidden(self, table, authorized_event, mock_context):
        from lambdas.admin_requests.handler import handler

        resp = handler(authorized_event(email="member@example.com"), mock_context)
        assert resp["statusCode"] == 403


class TestList:
    def test_admin_lists_pending_requests(self, table, authorized_event, mock_context):
        from lambdas.admin_requests.handler import handler
        from lambdas.common import link_requests

        link_requests.create_request("a@example.com", "1111111111", "Al")
        b = link_requests.create_request("b@example.com", "2222222222", None)
        link_requests.set_status(b["requestId"], link_requests.STATUS_DENIED)

        resp = handler(authorized_event(email="dominickj.giordano@gmail.com"), mock_context)
        assert resp["statusCode"] == 200

        data = json.loads(resp["body"])["data"]
        assert len(data["requests"]) == 1
        row = data["requests"][0]
        assert row["requesterEmail"] == "a@example.com"
        assert row["phone"] == "1111111111"
        assert row["savedName"] == "Al"
        assert "requestId" in row
        assert "createdAt" in row
