"""
RED-before-GREEN: GET /me/get (authed) -- the caller's link STATE.

Reports linkStatus in {"none","pending","linked"} so the frontend can show the
right UI: no link yet, an approval pending with the admin, or a live link (with
linkedHandles + shareCount). Under the admin-approval model a member is only
"linked" after Dom approves their request.
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


@pytest.fixture
def tables():
    with mock_aws():
        ddb = _create_tables()
        shares = ddb.Table(SHARES_TABLE_NAME)
        shares.put_item(Item={
            "shareId": "s1", "messageGuid": "g1", "direction": "in",
            "sharerHandle": "+13364042196", "platform": "spotify", "sourceUrl": "u1",
            "messageDate": 1753000000, "matchStatus": "matched", "createdAt": "x",
        })
        yield ddb


class TestMeGet:
    def test_requires_auth(self, tables, public_event, mock_context):
        from lambdas.me_get.handler import handler

        assert handler(public_event(), mock_context)["statusCode"] == 401

    def test_unlinked_user_reports_status_none(self, tables, authorized_event, mock_context):
        from lambdas.me_get.handler import handler

        resp = handler(authorized_event(email="nobody@example.com"), mock_context)
        data = json.loads(resp["body"])["data"]
        assert data["linkStatus"] == "none"
        assert data["linked"] is False
        assert data["linkedHandles"] == []
        assert data["shareCount"] == 0
        assert data["email"] == "nobody@example.com"

    def test_pending_request_reports_status_pending(self, tables, authorized_event, mock_context):
        from lambdas.common import link_requests
        from lambdas.me_get.handler import handler

        link_requests.create_request("member@example.com", "3364042196", "Big Al")

        resp = handler(authorized_event(email="member@example.com"), mock_context)
        data = json.loads(resp["body"])["data"]
        assert data["linkStatus"] == "pending"
        assert data["linked"] is False
        assert data["linkedHandles"] == []

    def test_linked_user_reports_status_linked(self, tables, authorized_event, mock_context):
        from lambdas.common.user_links import link_phone
        from lambdas.me_get.handler import handler

        link_phone("member@example.com", "3364042196")

        resp = handler(authorized_event(email="member@example.com"), mock_context)
        data = json.loads(resp["body"])["data"]
        assert data["linkStatus"] == "linked"
        assert data["linked"] is True
        assert data["linkedHandles"] == ["3364042196"]
        assert data["shareCount"] == 1
