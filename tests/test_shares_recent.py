"""
RED-before-GREEN: GET /shares/recent?limit=5 (PUBLIC -- WS-AUTH) -- compact
most-recent shares for the xomware.com hub showcase strip: a small set
shared-with-me (direction=in) and shared-by-me (direction=out), each with
title/artist/albumArtUrl/platform/sharer/direction/date.

The route is now UNAUTHENTICATED and server-side-scoped to the showcase owner
(Dom's normalized email = DEFAULT_OWNER_ID) via GSI-3, or an explicit `ownerId`
querystring override. A second user's rows never leak into the public strip.
"""

import json
import time

import boto3
import pytest
from moto import mock_aws

from lambdas.common.constants import (
    DEFAULT_OWNER_ID,
    SHARES_DIRECTION_INDEX,
    SHARES_OWNER_DIRECTION_INDEX,
    SHARES_SHARER_INDEX,
    SHARES_TABLE_NAME,
)

OTHER_OWNER = "someone-else@example.com"


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
    return ddb


def _put(shares, owner, **item):
    item["ownerId"] = owner
    item["ownerDirection"] = f"{owner}#{item['direction']}"
    shares.put_item(Item=item)


@pytest.fixture
def seeded():
    with mock_aws():
        ddb = _create_shares_table()
        shares = ddb.Table(SHARES_TABLE_NAME)
        now = int(time.time())
        # 3 inbound (shared with Dom), newest = in3
        for i, offset in enumerate([300, 200, 100]):
            _put(shares, DEFAULT_OWNER_ID,
                 shareId=f"in{i+1}", messageGuid=f"gi{i+1}", direction="in",
                 sharerHandle="+13364042196", sharerName="Sam",
                 platform="spotify", sourceUrl=f"https://open.spotify.com/track/in{i}",
                 trackTitle=f"In Song {i+1}", trackArtist="In Artist",
                 albumArtUrl="https://img/in.jpg",
                 messageDate=now - offset, matchStatus="matched", createdAt="x")
        # 2 outbound (shared by Dom -- no sharerHandle)
        for i, offset in enumerate([250, 150]):
            _put(shares, DEFAULT_OWNER_ID,
                 shareId=f"out{i+1}", messageGuid=f"go{i+1}", direction="out",
                 platform="spotify", sourceUrl=f"https://open.spotify.com/track/out{i}",
                 trackTitle=f"Out Song {i+1}", trackArtist="Out Artist",
                 albumArtUrl="https://img/out.jpg",
                 messageDate=now - offset, matchStatus="matched", createdAt="x")
        # A row owned by a DIFFERENT user -- must NEVER surface in the default
        # (Dom-scoped) public strip.
        _put(shares, OTHER_OWNER,
             shareId="other1", messageGuid="gx1", direction="in",
             sharerHandle="+19995550000", sharerName="Nope",
             platform="spotify", sourceUrl="https://open.spotify.com/track/other",
             trackTitle="Other Song", trackArtist="Other Artist",
             messageDate=now - 10, matchStatus="matched", createdAt="x")
        yield ddb


class TestSharesRecent:
    def test_public_no_auth_required(self, seeded, public_event, mock_context):
        from lambdas.shares_recent.handler import handler

        # No Authorization header at all -> still 200 (public showcase).
        resp = handler(public_event(), mock_context)
        assert resp["statusCode"] == 200

    def test_scoped_to_default_owner_excludes_others(self, seeded, public_event, mock_context):
        from lambdas.shares_recent.handler import handler

        data = json.loads(handler(public_event(), mock_context)["body"])["data"]
        assert data["ownerId"] == DEFAULT_OWNER_ID
        titles = {s["title"] for s in data["sharedWithMe"]}
        assert "Other Song" not in titles  # another owner's row never leaks

    def test_returns_compact_both_directions(self, seeded, public_event, mock_context):
        from lambdas.shares_recent.handler import handler

        event = public_event(queryStringParameters={"limit": "2"})
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

    def test_default_limit_and_cap(self, seeded, public_event, mock_context):
        from lambdas.shares_recent.handler import handler

        data = json.loads(handler(public_event(), mock_context)["body"])["data"]
        # default limit 5 -> all 3 in / 2 out returned
        assert len(data["sharedWithMe"]) == 3
        assert len(data["sharedByMe"]) == 2
        assert data["limit"] == 5

    def test_explicit_owner_override(self, seeded, public_event, mock_context):
        from lambdas.shares_recent.handler import handler

        event = public_event(queryStringParameters={"ownerId": OTHER_OWNER})
        data = json.loads(handler(event, mock_context)["body"])["data"]
        assert data["ownerId"] == OTHER_OWNER
        assert {s["title"] for s in data["sharedWithMe"]} == {"Other Song"}

    def test_invalid_limit_is_400(self, seeded, public_event, mock_context):
        from lambdas.shares_recent.handler import handler

        event = public_event(queryStringParameters={"limit": "abc"})
        assert handler(event, mock_context)["statusCode"] == 400
