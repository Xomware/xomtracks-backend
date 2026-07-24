"""
RED-before-GREEN: GET /admin/user-feed -- impersonation (view-as, read-only),
admin-gated.

Returns the TARGET's feed as they'd see it (union of Dom's global baseline +
the target's own shares via GSI-3). 404 if the target isn't in the directory.
"""

import json
import time

import boto3
import pytest
from moto import mock_aws

from lambdas.common.constants import (
    DEFAULT_OWNER_ID,
    SHARES_TABLE_NAME,
    SHARES_DIRECTION_INDEX,
    SHARES_SHARER_INDEX,
    SHARES_OWNER_DIRECTION_INDEX,
    USERS_TABLE_NAME,
)

ADMIN = "dominickj.giordano@gmail.com"
TARGET = "friend@example.com"


def _create_tables():
    ddb = boto3.resource("dynamodb", region_name="us-east-1")
    ddb.create_table(
        TableName=SHARES_TABLE_NAME,
        KeySchema=[{"AttributeName": "shareId", "KeyType": "HASH"}],
        AttributeDefinitions=[
            {"AttributeName": "shareId", "AttributeType": "S"},
            {"AttributeName": "direction", "AttributeType": "S"},
            {"AttributeName": "messageDate", "AttributeType": "N"},
            {"AttributeName": "sharerHandle", "AttributeType": "S"},
            {"AttributeName": "ownerDirection", "AttributeType": "S"},
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
            {
                "IndexName": SHARES_OWNER_DIRECTION_INDEX,
                "KeySchema": [
                    {"AttributeName": "ownerDirection", "KeyType": "HASH"},
                    {"AttributeName": "messageDate", "KeyType": "RANGE"},
                ],
                "Projection": {"ProjectionType": "ALL"},
                "ProvisionedThroughput": {"ReadCapacityUnits": 5, "WriteCapacityUnits": 5},
            },
        ],
        ProvisionedThroughput={"ReadCapacityUnits": 5, "WriteCapacityUnits": 5},
    )
    ddb.create_table(
        TableName=USERS_TABLE_NAME,
        KeySchema=[{"AttributeName": "email", "KeyType": "HASH"}],
        AttributeDefinitions=[{"AttributeName": "email", "AttributeType": "S"}],
        BillingMode="PAY_PER_REQUEST",
    )
    return ddb


@pytest.fixture
def seeded():
    with mock_aws():
        from lambdas.common import user_directory

        user_directory._last_written.clear()
        ddb = _create_tables()
        shares = ddb.Table(SHARES_TABLE_NAME)
        now = int(time.time())
        # Dom's global-baseline share (every user sees it).
        shares.put_item(Item={
            "shareId": "base1", "messageGuid": "g-base", "direction": "in",
            "ownerId": DEFAULT_OWNER_ID, "ownerDirection": f"{DEFAULT_OWNER_ID}#in",
            "platform": "spotify", "sourceUrl": "url-base", "messageDate": now - 100,
            "matchStatus": "matched", "createdAt": "x",
        })
        # The TARGET's own share.
        shares.put_item(Item={
            "shareId": "friend1", "messageGuid": "g-friend", "direction": "in",
            "ownerId": TARGET, "ownerDirection": f"{TARGET}#in",
            "platform": "spotify", "sourceUrl": "url-friend", "messageDate": now - 50,
            "matchStatus": "matched", "createdAt": "x",
        })
        # Mark the target as a known directory user.
        ddb.Table(USERS_TABLE_NAME).put_item(Item={
            "email": TARGET, "firstSeen": now - 1000, "lastSeen": now - 10,
        })
        yield


def _event(authorized_event, email=ADMIN, **params):
    return authorized_event(email=email, queryStringParameters=params)


class TestAuthGate:
    def test_requires_auth(self, seeded, public_event, mock_context):
        from lambdas.admin_userfeed.handler import handler

        ev = public_event(queryStringParameters={"email": TARGET, "direction": "in"})
        assert handler(ev, mock_context)["statusCode"] == 401

    def test_non_admin_forbidden(self, seeded, authorized_event, mock_context):
        from lambdas.admin_userfeed.handler import handler

        ev = _event(authorized_event, email="member@example.com")
        ev["queryStringParameters"] = {"email": TARGET, "direction": "in"}
        assert handler(ev, mock_context)["statusCode"] == 403


class TestViewAs:
    def test_returns_union_feed_for_target(self, seeded, authorized_event, mock_context):
        from lambdas.admin_userfeed.handler import handler

        ev = _event(authorized_event, email=ADMIN)
        ev["queryStringParameters"] = {"email": TARGET, "direction": "in"}
        resp = handler(ev, mock_context)
        assert resp["statusCode"] == 200
        data = json.loads(resp["body"])["data"]
        assert data["email"] == TARGET
        ids = {s["shareId"] for s in data["shares"]}
        assert ids == {"base1", "friend1"}

    def test_unknown_target_is_404(self, seeded, authorized_event, mock_context):
        from lambdas.admin_userfeed.handler import handler

        ev = _event(authorized_event, email=ADMIN)
        ev["queryStringParameters"] = {"email": "ghost@example.com", "direction": "in"}
        assert handler(ev, mock_context)["statusCode"] == 404

    def test_missing_direction_is_400(self, seeded, authorized_event, mock_context):
        from lambdas.admin_userfeed.handler import handler

        ev = _event(authorized_event, email=ADMIN)
        ev["queryStringParameters"] = {"email": TARGET}
        assert handler(ev, mock_context)["statusCode"] == 400
