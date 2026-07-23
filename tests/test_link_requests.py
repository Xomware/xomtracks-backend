"""
RED-before-GREEN: link-request store (lambdas/common/link_requests.py).

Admin-approval phone linking replaces the old trust-based auto-link. A member's
POST /me/link-phone creates a PENDING REQUEST here; the admin (Dom) approves or
denies it. This module is the DynamoDB store for those requests
(xomtracks-link-requests, PK requestId).
"""

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


class TestCreate:
    def test_create_returns_pending_request(self, table):
        from lambdas.common import link_requests

        req = link_requests.create_request(
            requester_email="member@example.com",
            phone="3364042196",
            saved_name="Big Al",
            sub="cognito-sub-1",
        )
        assert req["status"] == link_requests.STATUS_PENDING
        assert req["requesterEmail"] == "member@example.com"
        assert req["phone"] == "3364042196"
        assert req["savedName"] == "Big Al"
        assert req["sub"] == "cognito-sub-1"
        assert req["requestId"]
        assert isinstance(req["createdAt"], int)

        stored = link_requests.get_request(req["requestId"])
        assert stored["requesterEmail"] == "member@example.com"

    def test_create_allows_null_saved_name(self, table):
        from lambdas.common import link_requests

        req = link_requests.create_request(
            requester_email="new@example.com", phone="2025550000", saved_name=None
        )
        assert req["savedName"] is None


class TestList:
    def test_list_pending_only_returns_pending(self, table):
        from lambdas.common import link_requests

        a = link_requests.create_request("a@example.com", "1111111111", None)
        b = link_requests.create_request("b@example.com", "2222222222", None)
        link_requests.create_request("c@example.com", "3333333333", None)

        link_requests.set_status(a["requestId"], link_requests.STATUS_APPROVED)
        link_requests.set_status(b["requestId"], link_requests.STATUS_DENIED)

        pending = link_requests.list_pending()
        emails = {r["requesterEmail"] for r in pending}
        assert emails == {"c@example.com"}

    def test_has_pending_for_email(self, table):
        from lambdas.common import link_requests

        link_requests.create_request("member@example.com", "1111111111", None)
        assert link_requests.has_pending_for_email("member@example.com") is True
        assert link_requests.has_pending_for_email("stranger@example.com") is False

    def test_approved_request_no_longer_pending_for_email(self, table):
        from lambdas.common import link_requests

        req = link_requests.create_request("member@example.com", "1111111111", None)
        link_requests.set_status(req["requestId"], link_requests.STATUS_APPROVED)
        assert link_requests.has_pending_for_email("member@example.com") is False


class TestSetStatus:
    def test_set_status_updates_and_returns(self, table):
        from lambdas.common import link_requests

        req = link_requests.create_request("member@example.com", "1111111111", None)
        updated = link_requests.set_status(req["requestId"], link_requests.STATUS_APPROVED)
        assert updated["status"] == link_requests.STATUS_APPROVED
        assert updated["requestId"] == req["requestId"]

    def test_get_missing_request_returns_none(self, table):
        from lambdas.common import link_requests

        assert link_requests.get_request("does-not-exist") is None
