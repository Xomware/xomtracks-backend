"""
RED-before-GREEN: POST /me/link-phone (authed).

Links the phone number the CALLER enters to their Cognito identity, normalized
to last-10 digits. Verification is TRUST-BASED: link unconditionally, then
report how many existing shares carry that handle as their sharerHandle
("linked -- found N of your shares"). 0 matches still links, but is flagged.
"""

import json

import boto3
import pytest
from moto import mock_aws

from lambdas.common.constants import SHARES_TABLE_NAME, USERS_TABLE_NAME, SHARES_DIRECTION_INDEX, SHARES_SHARER_INDEX


def _create_tables():
    ddb = boto3.resource("dynamodb", region_name="us-east-1")
    ddb.create_table(
        TableName=USERS_TABLE_NAME,
        KeySchema=[{"AttributeName": "email", "KeyType": "HASH"}],
        AttributeDefinitions=[{"AttributeName": "email", "AttributeType": "S"}],
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
        # Two shares from a member whose raw E.164 handle normalizes to the
        # number they will link, plus one from someone else.
        shares.put_item(Item={
            "shareId": "s1", "messageGuid": "g1", "direction": "in",
            "sharerHandle": "+13364042196", "platform": "spotify", "sourceUrl": "u1",
            "messageDate": 1753000000, "matchStatus": "matched", "createdAt": "x",
        })
        shares.put_item(Item={
            "shareId": "s2", "messageGuid": "g2", "direction": "in",
            "sharerHandle": "+1 (336) 404-2196", "platform": "spotify", "sourceUrl": "u2",
            "messageDate": 1753000100, "matchStatus": "pending", "createdAt": "x",
        })
        shares.put_item(Item={
            "shareId": "s3", "messageGuid": "g3", "direction": "in",
            "sharerHandle": "+19998887777", "platform": "spotify", "sourceUrl": "u3",
            "messageDate": 1753000200, "matchStatus": "matched", "createdAt": "x",
        })
        yield ddb


class TestAuth:
    def test_requires_auth(self, tables, public_event, mock_context):
        from lambdas.me_link_phone.handler import handler

        resp = handler(public_event(body=json.dumps({"phoneNumber": "+13364042196"})), mock_context)
        assert resp["statusCode"] == 401


class TestLink:
    def test_links_and_counts_matched_shares(self, tables, authorized_event, mock_context):
        from lambdas.me_link_phone.handler import handler

        event = authorized_event(email="member@example.com", body=json.dumps({"phoneNumber": "(336) 404-2196"}))
        resp = handler(event, mock_context)
        assert resp["statusCode"] == 200

        data = json.loads(resp["body"])["data"]
        assert data["handle"] == "3364042196"
        assert data["matchedShareCount"] == 2  # s1 + s2, not s3
        assert data["flagged"] is False
        assert data["linkedHandles"] == ["3364042196"]

        users = tables.Table(USERS_TABLE_NAME)
        item = users.get_item(Key={"email": "member@example.com"})["Item"]
        assert set(item["linkedHandles"]) == {"3364042196"}

    def test_links_with_zero_matches_is_flagged(self, tables, authorized_event, mock_context):
        from lambdas.me_link_phone.handler import handler

        event = authorized_event(email="new@example.com", body=json.dumps({"phoneNumber": "+12025550000"}))
        resp = handler(event, mock_context)
        assert resp["statusCode"] == 200

        data = json.loads(resp["body"])["data"]
        assert data["matchedShareCount"] == 0
        assert data["flagged"] is True
        # still linked
        assert data["linkedHandles"] == ["2025550000"]

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
