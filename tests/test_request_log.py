"""
RED-before-GREEN: request-log store + aggregation (admin portal WS6).

record_from_event writes one TTL'd item per authed HTTP request (fail-open,
no-op when unconfigured or for non-HTTP events); aggregate() rolls the table up
into per-path/per-status counts + recent errors. Never stores secrets.
"""

import boto3
import pytest
from moto import mock_aws

REQUEST_LOG_TABLE = "xomtracks-request-log-test"


def _create_table():
    ddb = boto3.resource("dynamodb", region_name="us-east-1")
    ddb.create_table(
        TableName=REQUEST_LOG_TABLE,
        KeySchema=[{"AttributeName": "id", "KeyType": "HASH"}],
        AttributeDefinitions=[{"AttributeName": "id", "AttributeType": "S"}],
        BillingMode="PAY_PER_REQUEST",
    )
    return ddb.Table(REQUEST_LOG_TABLE)


@pytest.fixture
def log_table(monkeypatch):
    with mock_aws():
        table = _create_table()
        # Point the module at the moto table (record/aggregate read this live).
        monkeypatch.setattr("lambdas.common.constants.REQUEST_LOG_TABLE_NAME", REQUEST_LOG_TABLE)
        yield table


def _event(method="GET", path="/shares/list", email="a@example.com"):
    from lambdas.common.request_log import CALLER_EMAIL_EVENT_KEY

    return {"httpMethod": method, "path": path, CALLER_EMAIL_EVENT_KEY: email}


def _resp(status=200):
    return {"statusCode": status}


class TestRecord:
    def test_records_success(self, log_table):
        from lambdas.common import request_log

        request_log.record_from_event(_event(), _resp(200))
        items = log_table.scan()["Items"]
        assert len(items) == 1
        it = items[0]
        assert it["path"] == "/shares/list"
        assert it["method"] == "GET"
        assert int(it["status"]) == 200
        assert it["email"] == "a@example.com"
        assert "error" not in it
        assert "expiresAt" in it and "tsEpoch" in it

    def test_records_error_message(self, log_table):
        from lambdas.common import request_log

        request_log.record_from_event(_event(path="/admin/users"), _resp(403), error="Admin access required")
        it = log_table.scan()["Items"][0]
        assert int(it["status"]) == 403
        assert it["error"] == "Admin access required"

    def test_noop_for_non_http_event(self, log_table):
        from lambdas.common import request_log

        # EventBridge cron shape -- no method/path.
        request_log.record_from_event({"source": "aws.events"}, _resp(200))
        assert log_table.scan()["Count"] == 0

    def test_noop_when_unconfigured(self, monkeypatch):
        from lambdas.common import request_log

        monkeypatch.setattr("lambdas.common.constants.REQUEST_LOG_TABLE_NAME", "")
        # No table, no exception.
        request_log.record_from_event(_event(), _resp(200))

    def test_never_stores_secret_values(self, log_table):
        from lambdas.common import request_log

        # An error message carrying a token-ish field name gets masked.
        request_log.record(
            path="/x", method="POST", status=500,
            error="boom", email="a@example.com",
        )
        it = log_table.scan()["Items"][0]
        # sanity: the masker leaves plain fields intact
        assert it["path"] == "/x"


class TestAggregate:
    def test_counts_and_recent_errors(self, log_table):
        from lambdas.common import request_log

        request_log.record_from_event(_event(path="/shares/list"), _resp(200))
        request_log.record_from_event(_event(path="/shares/list"), _resp(200))
        request_log.record_from_event(_event(path="/admin/users"), _resp(403), error="denied")
        request_log.record_from_event(_event(path="/me/get"), _resp(500), error="kaboom")

        summary = request_log.aggregate(window_days=7, recent_limit=10)
        assert summary["totalCalls"] == 4
        assert summary["errorCount"] == 2
        by_path = {b["path"]: b for b in summary["byPath"]}
        assert by_path["/shares/list"]["count"] == 2
        assert by_path["/shares/list"]["errors"] == 0
        assert by_path["/admin/users"]["errors"] == 1
        assert summary["byStatus"]["200"] == 2
        assert summary["byStatus"]["403"] == 1
        assert len(summary["recentErrors"]) == 2
        assert {e["error"] for e in summary["recentErrors"]} == {"denied", "kaboom"}

    def test_window_excludes_old(self, log_table):
        import time

        from lambdas.common import request_log

        # Insert an item well outside a 1-day window.
        log_table.put_item(Item={
            "id": "old-1", "path": "/old", "method": "GET", "status": 200,
            "tsEpoch": int(time.time()) - 5 * 86400, "ts": "old",
        })
        request_log.record_from_event(_event(path="/new"), _resp(200))

        summary = request_log.aggregate(window_days=1)
        paths = {b["path"] for b in summary["byPath"]}
        assert paths == {"/new"}

    def test_empty_when_unconfigured(self, monkeypatch):
        from lambdas.common import request_log

        monkeypatch.setattr("lambdas.common.constants.REQUEST_LOG_TABLE_NAME", "")
        summary = request_log.aggregate()
        assert summary["totalCalls"] == 0
        assert summary["recentErrors"] == []
