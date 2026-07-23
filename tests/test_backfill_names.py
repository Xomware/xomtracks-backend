"""
RED-before-GREEN: extractor/backfill_names.py -- one-shot backfill of
sharerName onto EXISTING xomtracks-shares rows, resolved from the local
macOS Contacts DB. Runs on Dom's Mac; here we drive its pure `backfill()`
core against a moto-mocked DynamoDB table with an injected resolver (no real
AWS, no real Contacts DB).
"""

import boto3
import pytest
from moto import mock_aws


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
        table.put_item(Item={"shareId": "s1", "direction": "in", "sharerHandle": "+13364042196"})
        table.put_item(Item={"shareId": "s2", "direction": "in", "sharerHandle": "+15550009999"})
        table.put_item(Item={"shareId": "s3", "direction": "out"})  # no handle -> skipped
        table.put_item(Item={"shareId": "s4", "direction": "in", "sharerHandle": "+13364042196", "sharerName": "Old Name"})
        yield table


_NAMES = {"+13364042196": "Jordan Reyes", "+15550009999": None}


class TestBackfill:
    def test_fills_missing_names_and_reports_counts(self, shares_table):
        from extractor.backfill_names import backfill

        summary = backfill(shares_table, resolve_name=lambda h: _NAMES.get(h))

        assert shares_table.get_item(Key={"shareId": "s1"})["Item"]["sharerName"] == "Jordan Reyes"
        assert summary["updated"] == 1
        assert summary["unresolved"] == 1          # s2 handle not in contacts
        assert summary["skipped_no_handle"] == 1   # s3 outgoing
        assert summary["skipped_existing"] == 1    # s4 already had a name

    def test_does_not_overwrite_existing_name_without_force(self, shares_table):
        from extractor.backfill_names import backfill

        backfill(shares_table, resolve_name=lambda h: "Jordan Reyes")
        assert shares_table.get_item(Key={"shareId": "s4"})["Item"]["sharerName"] == "Old Name"

    def test_force_overwrites_existing_name(self, shares_table):
        from extractor.backfill_names import backfill

        backfill(shares_table, resolve_name=lambda h: "Jordan Reyes", force=True)
        assert shares_table.get_item(Key={"shareId": "s4"})["Item"]["sharerName"] == "Jordan Reyes"

    def test_dry_run_writes_nothing(self, shares_table):
        from extractor.backfill_names import backfill

        summary = backfill(shares_table, resolve_name=lambda h: _NAMES.get(h), dry_run=True)

        assert summary["would_update"] == 1
        assert "sharerName" not in shares_table.get_item(Key={"shareId": "s1"})["Item"]
