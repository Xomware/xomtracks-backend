"""
RED-before-GREEN: user directory store (admin portal WS6).

record_seen auto-upserts firstSeen/lastSeen (throttled, fail-open) onto the
xomtracks-users row; list_directory materializes the directory; is_known_user
backs the impersonation 404. Extends the EXISTING users table (email PK).
"""

import time

import boto3
import pytest
from moto import mock_aws

from lambdas.common.constants import USERS_TABLE_NAME


def _create_users_table():
    ddb = boto3.resource("dynamodb", region_name="us-east-1")
    ddb.create_table(
        TableName=USERS_TABLE_NAME,
        KeySchema=[{"AttributeName": "email", "KeyType": "HASH"}],
        AttributeDefinitions=[{"AttributeName": "email", "AttributeType": "S"}],
        BillingMode="PAY_PER_REQUEST",
    )
    return ddb


@pytest.fixture
def users_table():
    with mock_aws():
        ddb = _create_users_table()
        # Reset the in-process throttle so each test starts clean.
        from lambdas.common import user_directory

        user_directory._last_written.clear()
        yield ddb.Table(USERS_TABLE_NAME)


class TestRecordSeen:
    def test_sets_first_and_last_seen_on_first_sight(self, users_table):
        from lambdas.common import user_directory

        user_directory.record_seen("a@example.com")

        row = users_table.get_item(Key={"email": "a@example.com"})["Item"]
        assert row["firstSeen"] == row["lastSeen"]
        assert row["recordType"] == "directory"

    def test_bumps_last_seen_but_keeps_first_seen(self, users_table):
        from lambdas.common import user_directory

        user_directory.record_seen("a@example.com")
        first = users_table.get_item(Key={"email": "a@example.com"})["Item"]["firstSeen"]

        # Backdate lastSeen + clear the in-process throttle so a second write lands.
        users_table.update_item(
            Key={"email": "a@example.com"},
            UpdateExpression="SET lastSeen = :old",
            ExpressionAttributeValues={":old": int(time.time()) - 10_000},
        )
        user_directory._last_written.clear()

        user_directory.record_seen("a@example.com")
        row = users_table.get_item(Key={"email": "a@example.com"})["Item"]
        assert row["firstSeen"] == first
        assert row["lastSeen"] > first - 1

    def test_in_process_throttle_skips_second_write(self, users_table):
        from lambdas.common import user_directory

        user_directory.record_seen("a@example.com")
        # Corrupt the row; a throttled second call must NOT rewrite it.
        users_table.update_item(
            Key={"email": "a@example.com"},
            UpdateExpression="SET lastSeen = :v",
            ExpressionAttributeValues={":v": 123},
        )
        user_directory.record_seen("a@example.com")  # throttled -> no write
        row = users_table.get_item(Key={"email": "a@example.com"})["Item"]
        assert row["lastSeen"] == 123

    def test_fail_open_on_missing_table(self):
        # No moto / no table -> must swallow and not raise.
        from lambdas.common import user_directory

        user_directory._last_written.clear()
        user_directory.record_seen("nobody@example.com")  # no exception

    def test_empty_email_is_noop(self, users_table):
        from lambdas.common import user_directory

        user_directory.record_seen("")
        assert "Items" not in users_table.scan() or users_table.scan()["Count"] == 0


class TestListDirectory:
    def test_lists_only_seen_rows_with_computed_flags(self, users_table):
        from lambdas.common import user_directory

        # A seen user + a Spotify-connected seen user + an UNSEEN service row.
        user_directory.record_seen("seen@example.com")
        users_table.put_item(Item={
            "email": "spot@example.com",
            "lastSeen": int(time.time()),
            "firstSeen": int(time.time()),
            "refreshToken": "secret-refresh",
        })
        users_table.put_item(Item={"email": "service@example.com"})  # no lastSeen

        directory = user_directory.list_directory()
        emails = {d["email"] for d in directory}
        assert emails == {"seen@example.com", "spot@example.com"}

        spot = next(d for d in directory if d["email"] == "spot@example.com")
        assert spot["spotifyConnected"] is True
        assert "refreshToken" not in spot
        seen = next(d for d in directory if d["email"] == "seen@example.com")
        assert seen["spotifyConnected"] is False
        assert seen["ownIngest"] is False


class TestIsKnownUser:
    def test_true_for_seen_user(self, users_table):
        from lambdas.common import user_directory

        user_directory.record_seen("a@example.com")
        assert user_directory.is_known_user("a@example.com") is True

    def test_false_for_unseen_and_missing(self, users_table):
        from lambdas.common import user_directory

        users_table.put_item(Item={"email": "noseen@example.com"})  # no lastSeen
        assert user_directory.is_known_user("noseen@example.com") is False
        assert user_directory.is_known_user("ghost@example.com") is False
