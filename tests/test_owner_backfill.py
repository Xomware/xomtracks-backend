"""
RED-before-GREEN: lambdas/owner_backfill/handler.py -- the Phase 1B live-data
migration that stamps ownerId + ownerDirection onto historical shares.

Driven against a moto xomtracks-shares table with MIXED rows (some already
owned) to prove: only unowned rows get stamped, owned rows are untouched, a
re-run is a no-op, and ownerDirection == `<ownerId>#<direction>`.
"""

import boto3
import pytest
from moto import mock_aws

OWNER = "sub-dom-owner"


@pytest.fixture
def shares_table():
    with mock_aws():
        ddb = boto3.resource("dynamodb", region_name="us-east-1")
        table = ddb.create_table(
            TableName="xomtracks-shares-test",
            KeySchema=[{"AttributeName": "shareId", "KeyType": "HASH"}],
            AttributeDefinitions=[{"AttributeName": "shareId", "AttributeType": "S"}],
            BillingMode="PAY_PER_REQUEST",
        )
        # Two unowned legacy rows (in/out), one ALREADY owned (by someone else),
        # one with no direction (unstampable edge).
        table.put_item(Item={"shareId": "s1", "direction": "in", "messageDate": 1000})
        table.put_item(Item={"shareId": "s2", "direction": "out", "messageDate": 2000})
        table.put_item(Item={
            "shareId": "s3", "direction": "in", "messageDate": 3000,
            "ownerId": "sub-someone-else", "ownerDirection": "sub-someone-else#in",
        })
        table.put_item(Item={"shareId": "s4", "messageDate": 4000})  # no direction
        yield table


class TestOwnerBackfill:
    def test_stamps_only_unowned_rows(self, shares_table):
        from lambdas.owner_backfill.handler import backfill

        summary = backfill(shares_table, OWNER)

        assert summary["scanned"] == 4
        assert summary["stamped"] == 2  # s1, s2
        assert summary["skipped"] == 1  # s3 already owned
        assert summary["missing_direction"] == 1  # s4

        s1 = shares_table.get_item(Key={"shareId": "s1"})["Item"]
        assert s1["ownerId"] == OWNER
        assert s1["ownerDirection"] == f"{OWNER}#in"

        s2 = shares_table.get_item(Key={"shareId": "s2"})["Item"]
        assert s2["ownerDirection"] == f"{OWNER}#out"

    def test_does_not_touch_already_owned_rows(self, shares_table):
        from lambdas.owner_backfill.handler import backfill

        backfill(shares_table, OWNER)

        s3 = shares_table.get_item(Key={"shareId": "s3"})["Item"]
        assert s3["ownerId"] == "sub-someone-else"
        assert s3["ownerDirection"] == "sub-someone-else#in"

    def test_rerun_is_a_noop(self, shares_table):
        from lambdas.owner_backfill.handler import backfill

        backfill(shares_table, OWNER)
        second = backfill(shares_table, OWNER)

        # Everything is now owned (except the directionless row) -> nothing new.
        assert second["stamped"] == 0
        assert second["skipped"] == 3  # s1, s2, s3 all owned now
        assert second["missing_direction"] == 1

    def test_dry_run_writes_nothing(self, shares_table):
        from lambdas.owner_backfill.handler import backfill

        summary = backfill(shares_table, OWNER, dry_run=True)
        assert summary["stamped"] == 2

        # No actual write happened.
        assert "ownerId" not in shares_table.get_item(Key={"shareId": "s1"})["Item"]

    def test_requires_owner_id(self, shares_table):
        from lambdas.owner_backfill.handler import backfill

        with pytest.raises(ValueError):
            backfill(shares_table, "")

    def test_parity_stamped_count_equals_unowned_count(self, shares_table):
        """Post-backfill, count(ownerId present) must equal total stampable rows
        -- the item-count parity the migration verifies against GSI-3 in prod."""
        from lambdas.owner_backfill.handler import backfill

        backfill(shares_table, OWNER)

        owned = [i for i in shares_table.scan()["Items"] if i.get("ownerId")]
        # s1, s2 (newly stamped) + s3 (pre-owned) = 3; s4 has no direction.
        assert len(owned) == 3
