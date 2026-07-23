"""
RED-before-GREEN: POST /me/link-phone (authed) -- ADMIN-APPROVAL model.

The old trust-based auto-link is GONE. POST /me/link-phone now creates a PENDING
REQUEST (it does NOT write a link) and emails the admin (Dom) so he can approve
or deny it in the admin portal. The saved contact name for the number (if Dom
has one -- resolved from any share's sharerName) is captured on the request and
included in the notification. Returns {status:"pending", requestId}.
"""

import json

import boto3
import pytest
from moto import mock_aws

from lambdas.common.constants import (
    LINK_REQUESTS_TABLE_NAME,
    SHARES_DIRECTION_INDEX,
    SHARES_SHARER_INDEX,
    SHARES_TABLE_NAME,
    USERS_TABLE_NAME,
)


def _create_tables():
    ddb = boto3.resource("dynamodb", region_name="us-east-1")
    ddb.create_table(
        TableName=USERS_TABLE_NAME,
        KeySchema=[{"AttributeName": "email", "KeyType": "HASH"}],
        AttributeDefinitions=[{"AttributeName": "email", "AttributeType": "S"}],
        ProvisionedThroughput={"ReadCapacityUnits": 5, "WriteCapacityUnits": 5},
    )
    ddb.create_table(
        TableName=LINK_REQUESTS_TABLE_NAME,
        KeySchema=[{"AttributeName": "requestId", "KeyType": "HASH"}],
        AttributeDefinitions=[{"AttributeName": "requestId", "AttributeType": "S"}],
        ProvisionedThroughput={"ReadCapacityUnits": 5, "WriteCapacityUnits": 5},
    )
    ddb.create_table(
        TableName=SHARES_TABLE_NAME,
        KeySchema=[{"AttributeName": "shareId", "KeyType": "HASH"}],
        AttributeDefinitions=[
            {"AttributeName": "shareId", "AttributeType": "S"},
            {"AttributeName": "direction", "AttributeType": "S"},
            {"AttributeName": "messageDate", "AttributeType": "N"},
            {"AttributeName": "sharerHandle", "AttributeType": "S"},
        ],
        GlobalSecondaryIndexes=[
            {
                "IndexName": SHARES_DIRECTION_INDEX,
                "KeySchema": [
                    {"AttributeName": "direction", "KeyType": "HASH"},
                    {"AttributeName": "messageDate", "KeyType": "RANGE"},
                ],
                "Projection": {"ProjectionType": "ALL"},
                "ProvisionedThroughput": {"ReadCapacityUnits": 5, "WriteCapacityUnits": 5},
            },
            {
                "IndexName": SHARES_SHARER_INDEX,
                "KeySchema": [
                    {"AttributeName": "sharerHandle", "KeyType": "HASH"},
                    {"AttributeName": "messageDate", "KeyType": "RANGE"},
                ],
                "Projection": {"ProjectionType": "ALL"},
                "ProvisionedThroughput": {"ReadCapacityUnits": 5, "WriteCapacityUnits": 5},
            },
        ],
        ProvisionedThroughput={"ReadCapacityUnits": 5, "WriteCapacityUnits": 5},
    )
    return boto3.resource("dynamodb", region_name="us-east-1")


@pytest.fixture(autouse=True)
def _no_real_ses(monkeypatch):
    """Every link-phone call fires an SES notification -- stub it so tests never
    touch AWS SES. Individual tests can still assert on the recorded calls."""
    from lambdas.common import email_notify

    monkeypatch.setattr(
        email_notify, "send_link_request_notification", lambda **kwargs: True
    )


@pytest.fixture
def tables():
    with mock_aws():
        ddb = _create_tables()
        shares = ddb.Table(SHARES_TABLE_NAME)
        # A share whose raw handle normalizes to the number the member links,
        # carrying the saved contact name Dom has for that number.
        shares.put_item(Item={
            "shareId": "s1", "messageGuid": "g1", "direction": "in",
            "sharerHandle": "+13364042196", "sharerName": "Big Al",
            "platform": "spotify", "sourceUrl": "u1",
            "messageDate": 1753000000, "matchStatus": "matched", "createdAt": "x",
        })
        yield ddb


class TestAuth:
    def test_requires_auth(self, tables, public_event, mock_context):
        from lambdas.me_link_phone.handler import handler

        resp = handler(public_event(body=json.dumps({"phoneNumber": "+13364042196"})), mock_context)
        assert resp["statusCode"] == 401


class TestRequest:
    def test_creates_pending_request_not_a_link(self, tables, authorized_event, mock_context):
        from lambdas.me_link_phone.handler import handler

        event = authorized_event(
            email="member@example.com", body=json.dumps({"phoneNumber": "(336) 404-2196"})
        )
        resp = handler(event, mock_context)
        assert resp["statusCode"] == 200

        data = json.loads(resp["body"])["data"]
        assert data["status"] == "pending"
        assert data["requestId"]

        # NO link was written on the users table -- approval does that.
        users = tables.Table(USERS_TABLE_NAME)
        assert "Item" not in users.get_item(Key={"email": "member@example.com"})

        # The request was stored with the normalized handle + resolved saved name.
        reqs = tables.Table(LINK_REQUESTS_TABLE_NAME).scan()["Items"]
        assert len(reqs) == 1
        assert reqs[0]["phone"] == "3364042196"
        assert reqs[0]["savedName"] == "Big Al"
        assert reqs[0]["status"] == "pending"
        assert reqs[0]["requesterEmail"] == "member@example.com"

    def test_saved_name_null_when_no_shares_for_number(self, tables, authorized_event, mock_context):
        from lambdas.me_link_phone.handler import handler

        event = authorized_event(
            email="new@example.com", body=json.dumps({"phoneNumber": "+12025550000"})
        )
        resp = handler(event, mock_context)
        assert resp["statusCode"] == 200

        reqs = tables.Table(LINK_REQUESTS_TABLE_NAME).scan()["Items"]
        assert reqs[0]["phone"] == "2025550000"
        assert reqs[0].get("savedName") is None

    def test_notifies_admin_via_ses(self, tables, authorized_event, mock_context, monkeypatch):
        from lambdas.common import email_notify
        from lambdas.me_link_phone.handler import handler

        calls = []
        monkeypatch.setattr(
            email_notify,
            "send_link_request_notification",
            lambda **kwargs: calls.append(kwargs) or True,
        )

        event = authorized_event(
            email="member@example.com", body=json.dumps({"phoneNumber": "(336) 404-2196"})
        )
        handler(event, mock_context)

        assert len(calls) == 1
        assert calls[0]["requester_email"] == "member@example.com"
        assert calls[0]["phone"] == "3364042196"
        assert calls[0]["saved_name"] == "Big Al"

    def test_missing_phone_is_400(self, tables, authorized_event, mock_context):
        from lambdas.me_link_phone.handler import handler

        resp = handler(authorized_event(email="member@example.com", body=json.dumps({})), mock_context)
        assert resp["statusCode"] == 400

    def test_no_digits_is_400(self, tables, authorized_event, mock_context):
        from lambdas.me_link_phone.handler import handler

        resp = handler(
            authorized_event(email="member@example.com", body=json.dumps({"phoneNumber": "not a number"})),
            mock_context,
        )
        assert resp["statusCode"] == 400
