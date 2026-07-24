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

from lambdas.common.constants import (
    SHARES_TABLE_NAME,
    SHARES_DIRECTION_INDEX,
    SHARES_SHARER_INDEX,
    SHARES_OWNER_DIRECTION_INDEX,
)


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


class TestScanByNormalizedHandles:
    """Powers "my shares" for a linked member + the matched-count on link.
    sharerHandle is stored raw (E.164); linked handles are last-10-digit
    normalized -- so we scan and normalize each row's handle in Python."""

    def _seed(self, put_share_idempotent, sample_share):
        # Two shares from the same member in two different raw formats, one
        # from someone else.
        a = dict(sample_share)
        a.update({"shareId": "sa", "messageGuid": "ga", "sourceUrl": "ua",
                  "sharerHandle": "+13364042196", "messageDate": 2000})
        b = dict(sample_share)
        b.update({"shareId": "sb", "messageGuid": "gb", "sourceUrl": "ub",
                  "sharerHandle": "(336) 404-2196", "messageDate": 3000})
        c = dict(sample_share)
        c.update({"shareId": "sc", "messageGuid": "gc", "sourceUrl": "uc",
                  "sharerHandle": "+19998887777", "messageDate": 4000})
        for s in (a, b, c):
            put_share_idempotent(s)

    def test_matches_across_formats(self, ddb_table, sample_share):
        from lambdas.common.shares_dynamo import put_share_idempotent, scan_shares_by_normalized_handles

        self._seed(put_share_idempotent, sample_share)
        results = scan_shares_by_normalized_handles({"3364042196"})
        assert {r["shareId"] for r in results} == {"sa", "sb"}

    def test_since_epoch_filters(self, ddb_table, sample_share):
        from lambdas.common.shares_dynamo import put_share_idempotent, scan_shares_by_normalized_handles

        self._seed(put_share_idempotent, sample_share)
        results = scan_shares_by_normalized_handles({"3364042196"}, since_epoch=2500)
        assert {r["shareId"] for r in results} == {"sb"}

    def test_empty_handles_returns_empty(self, ddb_table, sample_share):
        from lambdas.common.shares_dynamo import put_share_idempotent, scan_shares_by_normalized_handles

        self._seed(put_share_idempotent, sample_share)
        assert scan_shares_by_normalized_handles(set()) == []


class TestOwnerDirection:
    """Phase 1 multi-tenant re-key: ownerDirection (`<ownerId>#<direction>`) is
    derived on write and drives the owner-scoped GSI-3 query."""

    def test_compute_owner_direction(self):
        from lambdas.common.shares_dynamo import compute_owner_direction

        assert compute_owner_direction("sub-abc", "in") == "sub-abc#in"
        assert compute_owner_direction("sub-abc", "out") == "sub-abc#out"

    def test_compute_owner_direction_none_when_unowned(self):
        from lambdas.common.shares_dynamo import compute_owner_direction

        assert compute_owner_direction(None, "in") is None
        assert compute_owner_direction("sub-abc", None) is None

    def test_write_derives_owner_direction(self, ddb_table, sample_share):
        from lambdas.common.shares_dynamo import put_share_idempotent, derive_share_id
        import boto3 as _boto3

        share = dict(sample_share)
        share["ownerId"] = "sub-abc"
        share["direction"] = "in"
        share["shareId"] = derive_share_id("go", "urlo")
        share["messageGuid"] = "go"
        share["sourceUrl"] = "urlo"
        put_share_idempotent(share)

        table = _boto3.resource("dynamodb", region_name="us-east-1").Table(SHARES_TABLE_NAME)
        stored = table.get_item(Key={"shareId": share["shareId"]})["Item"]
        assert stored["ownerDirection"] == "sub-abc#in"

    def test_write_without_owner_omits_owner_direction(self, ddb_table, sample_share):
        # Legacy/unowned write -- ownerDirection must be absent (sparse GSI-3).
        from lambdas.common.shares_dynamo import put_share_idempotent, derive_share_id
        import boto3 as _boto3

        share = dict(sample_share)
        share.pop("ownerId", None)
        share["shareId"] = derive_share_id("gl", "urll")
        share["messageGuid"] = "gl"
        share["sourceUrl"] = "urll"
        put_share_idempotent(share)

        table = _boto3.resource("dynamodb", region_name="us-east-1").Table(SHARES_TABLE_NAME)
        stored = table.get_item(Key={"shareId": share["shareId"]})["Item"]
        assert "ownerDirection" not in stored

    def test_query_by_owner_direction_scopes_to_owner(self, ddb_table, sample_share):
        from lambdas.common.shares_dynamo import (
            put_share_idempotent,
            query_shares_by_owner_direction,
            derive_share_id,
        )

        dom_in = dict(sample_share)
        dom_in.update({"ownerId": "sub-dom", "direction": "in", "messageDate": 5000,
                       "shareId": derive_share_id("d1", "u1"), "messageGuid": "d1", "sourceUrl": "u1"})
        dom_out = dict(sample_share)
        dom_out.update({"ownerId": "sub-dom", "direction": "out", "sharerHandle": None,
                        "messageDate": 5000, "shareId": derive_share_id("d2", "u2"),
                        "messageGuid": "d2", "sourceUrl": "u2"})
        other_in = dict(sample_share)
        other_in.update({"ownerId": "sub-other", "direction": "in", "messageDate": 5000,
                         "shareId": derive_share_id("o1", "u3"), "messageGuid": "o1", "sourceUrl": "u3"})
        for s in (dom_in, dom_out, other_in):
            put_share_idempotent(s)

        dom_shares = query_shares_by_owner_direction("sub-dom", "in", since_epoch=0)
        assert {s["messageGuid"] for s in dom_shares} == {"d1"}

        other_shares = query_shares_by_owner_direction("sub-other", "in", since_epoch=0)
        assert {s["messageGuid"] for s in other_shares} == {"o1"}

    def test_query_by_owner_direction_respects_since_epoch(self, ddb_table, sample_share):
        from lambdas.common.shares_dynamo import (
            put_share_idempotent,
            query_shares_by_owner_direction,
            derive_share_id,
        )

        old = dict(sample_share)
        old.update({"ownerId": "sub-dom", "direction": "in", "messageDate": 1000,
                    "shareId": derive_share_id("a", "ua"), "messageGuid": "a", "sourceUrl": "ua"})
        new = dict(sample_share)
        new.update({"ownerId": "sub-dom", "direction": "in", "messageDate": 9000,
                    "shareId": derive_share_id("b", "ub"), "messageGuid": "b", "sourceUrl": "ub"})
        for s in (old, new):
            put_share_idempotent(s)

        results = query_shares_by_owner_direction("sub-dom", "in", since_epoch=5000)
        assert {s["messageGuid"] for s in results} == {"b"}

    def test_parity_owner_scoped_matches_legacy_for_single_owner(self, ddb_table, sample_share):
        """The load-bearing Phase 1C guarantee: for the sole owner (Dom), the
        owner-scoped GSI-3 query returns the EXACT same set as the legacy GSI-1
        direction query once every row is owned."""
        from lambdas.common.shares_dynamo import (
            put_share_idempotent,
            query_shares_by_direction,
            query_shares_by_owner_direction,
            derive_share_id,
        )

        for i in range(5):
            s = dict(sample_share)
            s.update({"ownerId": "sub-dom", "direction": "in", "messageDate": 1000 + i,
                      "shareId": derive_share_id(f"g{i}", f"u{i}"),
                      "messageGuid": f"g{i}", "sourceUrl": f"u{i}"})
            put_share_idempotent(s)

        legacy = query_shares_by_direction("in", since_epoch=0)
        owned = query_shares_by_owner_direction("sub-dom", "in", since_epoch=0)
        assert {s["shareId"] for s in owned} == {s["shareId"] for s in legacy}
        assert len(owned) == len(legacy) == 5


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
