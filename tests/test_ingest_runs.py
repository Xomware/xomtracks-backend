"""
Extractor run telemetry: POST /ingest/run records a compact per-scan summary
(ingest-token authed), and GET /admin/runs feeds the admin Extractor-status
"recent runs" view (admin-gated), grouped by owner, recent-first.
"""

import json

import boto3
import pytest
from moto import mock_aws

from lambdas.common.constants import (
    ADMIN_EMAIL,
    INGEST_RUNS_TABLE_NAME,
    INGEST_TOKENS_TABLE_NAME,
)

OWNER = "runner@example.com"


def _mk_runs_table():
    boto3.resource("dynamodb", region_name="us-east-1").create_table(
        TableName=INGEST_RUNS_TABLE_NAME,
        KeySchema=[
            {"AttributeName": "ownerId", "KeyType": "HASH"},
            {"AttributeName": "runAt", "KeyType": "RANGE"},
        ],
        AttributeDefinitions=[
            {"AttributeName": "ownerId", "AttributeType": "S"},
            {"AttributeName": "runAt", "AttributeType": "N"},
        ],
        BillingMode="PAY_PER_REQUEST",
    )


def _mk_tokens_table():
    boto3.resource("dynamodb", region_name="us-east-1").create_table(
        TableName=INGEST_TOKENS_TABLE_NAME,
        KeySchema=[{"AttributeName": "tokenHash", "KeyType": "HASH"}],
        AttributeDefinitions=[{"AttributeName": "tokenHash", "AttributeType": "S"}],
        BillingMode="PAY_PER_REQUEST",
    )


@pytest.fixture
def runs_table():
    with mock_aws():
        _mk_runs_table()
        yield boto3.resource("dynamodb", region_name="us-east-1")


@pytest.fixture
def runs_and_tokens():
    with mock_aws():
        _mk_runs_table()
        _mk_tokens_table()
        yield boto3.resource("dynamodb", region_name="us-east-1")


def _put_run(ddb, owner, run_at, scanned=1, ingested=0):
    ddb.Table(INGEST_RUNS_TABLE_NAME).put_item(
        Item={"ownerId": owner, "runAt": run_at, "scanned": scanned, "ingested": ingested}
    )


# ── Common module ──────────────────────────────────────────────────────────
class TestRecordAndList:
    def test_record_run_writes_row_with_ttl(self, runs_table):
        from lambdas.common import ingest_runs

        assert ingest_runs.record_run(OWNER, scanned=1200, ingested=3, new_watermark=9001, duration_ms=800) is True

        items = runs_table.Table(INGEST_RUNS_TABLE_NAME).scan()["Items"]
        assert len(items) == 1
        row = items[0]
        assert row["ownerId"] == OWNER
        assert int(row["scanned"]) == 1200
        assert int(row["ingested"]) == 3
        assert int(row["newWatermark"]) == 9001
        assert int(row["durationMs"]) == 800
        assert int(row["expiresAt"]) > int(row["runAt"])  # TTL in the future

    def test_list_runs_recent_first_and_limited(self, runs_table):
        from lambdas.common import ingest_runs

        _put_run(runs_table, OWNER, 100)
        _put_run(runs_table, OWNER, 300)
        _put_run(runs_table, OWNER, 200)

        runs = ingest_runs.list_runs_for_owner(OWNER, limit=2)
        assert [r["runAt"] for r in runs] == [300, 200]  # recent-first, capped

    def test_list_runs_empty_for_unknown_owner(self, runs_table):
        from lambdas.common import ingest_runs

        assert ingest_runs.list_runs_for_owner("nobody@example.com") == []


# ── POST /ingest/run ───────────────────────────────────────────────────────
class TestIngestRunHandler:
    def test_per_user_token_records_run_for_that_owner(self, runs_and_tokens, ingest_event, mock_context):
        from lambdas.ingest_run.handler import handler
        from lambdas.common import ingest_tokens

        token = ingest_tokens.mint_token(OWNER)["token"]
        event = ingest_event(bearer_key=token, body=json.dumps({"scanned": 42, "ingested": 2}))

        resp = handler(event, mock_context)
        assert resp["statusCode"] == 200
        assert json.loads(resp["body"])["data"]["recorded"] is True

        rows = runs_and_tokens.Table(INGEST_RUNS_TABLE_NAME).scan()["Items"]
        assert len(rows) == 1 and rows[0]["ownerId"] == OWNER and int(rows[0]["scanned"]) == 42

    def test_legacy_ssm_key_records_for_default_owner(self, runs_and_tokens, ingest_event, mock_context):
        from lambdas.ingest_run.handler import handler
        from lambdas.common.constants import DEFAULT_OWNER_ID

        event = ingest_event(bearer_key="test-ingest-key", body=json.dumps({"scanned": 5, "ingested": 0}))
        resp = handler(event, mock_context)
        assert resp["statusCode"] == 200

        rows = runs_and_tokens.Table(INGEST_RUNS_TABLE_NAME).scan()["Items"]
        assert rows[0]["ownerId"] == DEFAULT_OWNER_ID

    def test_no_bearer_is_401(self, runs_and_tokens, public_event, mock_context):
        from lambdas.ingest_run.handler import handler

        event = public_event(httpMethod="POST", body=json.dumps({"scanned": 1}))
        assert handler(event, mock_context)["statusCode"] == 401

    def test_negative_count_is_400(self, runs_and_tokens, ingest_event, mock_context):
        from lambdas.ingest_run.handler import handler

        event = ingest_event(bearer_key="test-ingest-key", body=json.dumps({"scanned": -1}))
        assert handler(event, mock_context)["statusCode"] == 400


# ── GET /admin/runs ────────────────────────────────────────────────────────
class TestAdminRunsHandler:
    def test_requires_admin(self, runs_and_tokens, public_event, authorized_event, mock_context):
        from lambdas.admin_runs.handler import handler

        assert handler(public_event(), mock_context)["statusCode"] == 401
        assert handler(authorized_event(email="member@example.com"), mock_context)["statusCode"] == 403

    def test_returns_recent_runs_grouped_by_token_owner(self, runs_and_tokens, authorized_event, mock_context):
        from lambdas.admin_runs.handler import handler
        from lambdas.common import ingest_tokens

        ingest_tokens.mint_token(OWNER, label="mbp")  # owner has an extractor
        _put_run(runs_and_tokens, OWNER, 100, scanned=10, ingested=1)
        _put_run(runs_and_tokens, OWNER, 200, scanned=11, ingested=0)

        data = json.loads(handler(authorized_event(email=ADMIN_EMAIL), mock_context)["body"])["data"]
        assert data["ownerCount"] == 1
        runs = data["byOwner"][OWNER]
        assert [r["runAt"] for r in runs] == [200, 100]  # recent-first
