"""
RED-before-GREEN: GET /shares/recent?limit=5 (authed) -- compact most-recent
shares for the xomware.com hub widget: a small set shared-with-me (direction=in)
and shared-by-me (direction=out), each with title/artist/albumArtUrl/platform/
sharer/direction/date.
"""

import json
import time

import boto3
import pytest
from moto import mock_aws

from lambdas.common.constants import (
    SHARES_DIRECTION_INDEX,
    SHARES_SHARER_INDEX,
    SHARES_TABLE_NAME,
)


def _create_shares_table():
    ddb = boto3.resource("dynamodb", region_name="us-east-1")
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
    return ddb


@pytest.fixture
def seeded():
    with mock_aws():
        ddb = _create_shares_table()
        shares = ddb.Table(SHARES_TABLE_NAME)
        now = int(time.time())
        # 3 inbound (shared with me), newest = in3
        for i, offset in enumerate([300, 200, 100]):
            shares.put_item(Item={
                "shareId": f"in{i+1}", "messageGuid": f"gi{i+1}", "direction": "in",
                "sharerHandle": "+13364042196", "sharerName": "Sam",
                "platform": "spotify", "sourceUrl": f"https://open.spotify.com/track/in{i}",
                "trackTitle": f"In Song {i+1}", "trackArtist": "In Artist",
                "albumArtUrl": "https://img/in.jpg",
                "messageDate": now - offset, "matchStatus": "matched", "createdAt": "x",
            })
        # 2 outbound (shared by me -- Dom is sender, no sharerHandle)
        for i, offset in enumerate([250, 150]):
            shares.put_item(Item={
                "shareId": f"out{i+1}", "messageGuid": f"go{i+1}", "direction": "out",
                "platform": "spotify", "sourceUrl": f"https://open.spotify.com/track/out{i}",
                "trackTitle": f"Out Song {i+1}", "trackArtist": "Out Artist",
                "albumArtUrl": "https://img/out.jpg",
                "messageDate": now - offset, "matchStatus": "matched", "createdAt": "x",
            })
        yield ddb


class TestSharesRecent:
    def test_requires_auth(self, seeded, public_event, mock_context):
        from lambdas.shares_recent.handler import handler

        assert handler(public_event(), mock_context)["statusCode"] == 401

    def test_returns_compact_both_directions(self, seeded, authorized_event, mock_context):
        from lambdas.shares_recent.handler import handler

        event = authorized_event(email="dom@example.com", queryStringParameters={"limit": "2"})
        resp = handler(event, mock_context)
        assert resp["statusCode"] == 200
        data = json.loads(resp["body"])["data"]

        assert len(data["sharedWithMe"]) == 2
        assert len(data["sharedByMe"]) == 2

        # Newest-first within each direction.
        first_in = data["sharedWithMe"][0]
        assert first_in["title"] == "In Song 3"
        assert first_in["artist"] == "In Artist"
        assert first_in["albumArtUrl"] == "https://img/in.jpg"
        assert first_in["platform"] == "spotify"
        assert first_in["direction"] == "in"
        assert first_in["sharer"] == "Sam"
        assert isinstance(first_in["date"], int)

        # out2 is newer (now-150) than out1 (now-250) -> newest-first.
        first_out = data["sharedByMe"][0]
        assert first_out["title"] == "Out Song 2"
        assert first_out["direction"] == "out"
        # Outbound shares have no sharer handle (Dom is the sender).
        assert first_out["sharer"] is None

    def test_default_limit_and_cap(self, seeded, authorized_event, mock_context):
        from lambdas.shares_recent.handler import handler

        event = authorized_event(email="dom@example.com", queryStringParameters={})
        data = json.loads(handler(event, mock_context)["body"])["data"]
        # default limit 5 -> all 3 in / 2 out returned
        assert len(data["sharedWithMe"]) == 3
        assert len(data["sharedByMe"]) == 2
        assert data["limit"] == 5

    def test_invalid_limit_is_400(self, seeded, authorized_event, mock_context):
        from lambdas.shares_recent.handler import handler

        event = authorized_event(email="dom@example.com", queryStringParameters={"limit": "abc"})
        assert handler(event, mock_context)["statusCode"] == 400
