"""
RED-before-GREEN: shares_dynamo.py -- the xomtracks-shares store module.

Uses moto.mock_aws against real boto3 table schemas: PK shareId, GSI-1
direction/messageDate (MVP time-window-per-direction query), GSI-2
sharerHandle/messageDate (reserved for the by-sharer fast-follow).

Dedup key: shareId is a deterministic hash of (messageGuid, sourceUrl) --
NOT messageGuid alone, since a single message can contain more than one
music link (each is a distinct share). Re-ingesting the same
(messageGuid, sourceUrl) pair must NOT create a second row.
"""

import boto3
import pytest
from moto import mock_aws

from lambdas.common.constants import SHARES_TABLE_NAME, SHARES_DIRECTION_INDEX, SHARES_SHARER_INDEX


def _create_table():
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
def ddb_table():
    with mock_aws():
        _create_table()
        yield


class TestDeriveShareId:
    def test_deterministic(self):
        from lambdas.common.shares_dynamo import derive_share_id

        a = derive_share_id("guid-1", "https://open.spotify.com/track/abc")
        b = derive_share_id("guid-1", "https://open.spotify.com/track/abc")
        assert a == b

    def test_distinct_per_url(self):
        from lambdas.common.shares_dynamo import derive_share_id

        a = derive_share_id("guid-1", "https://open.spotify.com/track/abc")
        b = derive_share_id("guid-1", "https://soundcloud.com/artist/track")
        assert a != b

    def test_distinct_per_guid(self):
        from lambdas.common.shares_dynamo import derive_share_id

        a = derive_share_id("guid-1", "https://open.spotify.com/track/abc")
        b = derive_share_id("guid-2", "https://open.spotify.com/track/abc")
        assert a != b


class TestPutShareIdempotent:
    def test_first_write_creates(self, ddb_table, sample_share):
        from lambdas.common.shares_dynamo import put_share_idempotent

        item, created = put_share_idempotent(sample_share)
        assert created is True
        assert item["shareId"] == sample_share["shareId"]

    def test_duplicate_messageguid_and_url_does_not_create_second_row(self, ddb_table, sample_share):
        from lambdas.common.shares_dynamo import put_share_idempotent, derive_share_id
        import boto3 as _boto3

        sample_share["shareId"] = derive_share_id(sample_share["messageGuid"], sample_share["sourceUrl"])
        item1, created1 = put_share_idempotent(dict(sample_share))
        item2, created2 = put_share_idempotent(dict(sample_share))

        assert created1 is True
        assert created2 is False  # already-exists, not a dupe

        table = _boto3.resource("dynamodb", region_name="us-east-1").Table(SHARES_TABLE_NAME)
        scanned = table.scan()["Items"]
        assert len(scanned) == 1

    def test_distinct_url_same_message_creates_second_row(self, ddb_table, sample_share):
        from lambdas.common.shares_dynamo import put_share_idempotent, derive_share_id
        import boto3 as _boto3

        share_a = dict(sample_share)
        share_a["shareId"] = derive_share_id(share_a["messageGuid"], share_a["sourceUrl"])
        put_share_idempotent(share_a)

        share_b = dict(sample_share)
        share_b["sourceUrl"] = "https://soundcloud.com/artist/other-track"
        share_b["shareId"] = derive_share_id(share_b["messageGuid"], share_b["sourceUrl"])
        item_b, created_b = put_share_idempotent(share_b)

        assert created_b is True
        table = _boto3.resource("dynamodb", region_name="us-east-1").Table(SHARES_TABLE_NAME)
        scanned = table.scan()["Items"]
        assert len(scanned) == 2


class TestQueryByDirectionWindow:
    def test_filters_by_direction_and_since(self, ddb_table, sample_share):
        from lambdas.common.shares_dynamo import put_share_idempotent, query_shares_by_direction, derive_share_id

        in_old = dict(sample_share)
        in_old["direction"] = "in"
        in_old["messageDate"] = 1000
        in_old["shareId"] = derive_share_id("g1", "url1")
        in_old["messageGuid"] = "g1"
        in_old["sourceUrl"] = "url1"

        in_new = dict(sample_share)
        in_new["direction"] = "in"
        in_new["messageDate"] = 5000
        in_new["shareId"] = derive_share_id("g2", "url2")
        in_new["messageGuid"] = "g2"
        in_new["sourceUrl"] = "url2"

        out_new = dict(sample_share)
        out_new["direction"] = "out"
        out_new["messageDate"] = 5000
        out_new["shareId"] = derive_share_id("g3", "url3")
        out_new["messageGuid"] = "g3"
        out_new["sourceUrl"] = "url3"

        for s in (in_old, in_new, out_new):
            put_share_idempotent(s)

        results = query_shares_by_direction("in", since_epoch=2000)
        guids = {r["messageGuid"] for r in results}
        assert guids == {"g2"}

    def test_empty_when_no_matches(self, ddb_table):
        from lambdas.common.shares_dynamo import query_shares_by_direction

        results = query_shares_by_direction("in", since_epoch=0)
        assert results == []


class TestScanByMatchStatus:
    """Whole-table filtered scan powering the matching sweep -- no GSI on
    matchStatus (infrequent backfill/cron read)."""

    def test_returns_only_matching_status(self, ddb_table, sample_share):
        from lambdas.common.shares_dynamo import put_share_idempotent, scan_shares_by_match_status, derive_share_id

        pending = dict(sample_share)
        pending["matchStatus"] = "pending"
        pending["shareId"] = derive_share_id("gp", "urlp")
        pending["messageGuid"] = "gp"
        pending["sourceUrl"] = "urlp"

        matched = dict(sample_share)
        matched["matchStatus"] = "matched"
        matched["shareId"] = derive_share_id("gm", "urlm")
        matched["messageGuid"] = "gm"
        matched["sourceUrl"] = "urlm"

        for s in (pending, matched):
            put_share_idempotent(s)

        results = scan_shares_by_match_status("pending")
        assert [r["messageGuid"] for r in results] == ["gp"]

    def test_empty_when_no_matches(self, ddb_table):
        from lambdas.common.shares_dynamo import scan_shares_by_match_status

        assert scan_shares_by_match_status("pending") == []


class TestQueryBySharer:
    """GSI-2 -- reserved for the by-sharer fast-follow, but the table +
    query function exist now (cheap, per PLAN.md)."""

    def test_filters_by_sharer_handle(self, ddb_table, sample_share):
        from lambdas.common.shares_dynamo import put_share_idempotent, query_shares_by_sharer, derive_share_id

        share = dict(sample_share)
        share["sharerHandle"] = "+15551234567"
        share["shareId"] = derive_share_id("g9", "url9")
        share["messageGuid"] = "g9"
        share["sourceUrl"] = "url9"
        put_share_idempotent(share)

        results = query_shares_by_sharer("+15551234567", since_epoch=0)
        assert len(results) == 1
        assert results[0]["messageGuid"] == "g9"

        empty = query_shares_by_sharer("+10000000000", since_epoch=0)
        assert empty == []
