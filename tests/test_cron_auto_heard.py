"""
RED-before-GREEN: the auto-heard cron reads Dom's Spotify recently-played and
marks the matching tracks heard for Dom (Dom-only for now -- per-user Spotify
OAuth is a documented fast-follow). Pure logic + the persist core are tested
here; the live Spotify fetch is an injectable edge (same pattern as the
matching sweep).
"""

import boto3
import pytest
from moto import mock_aws

from lambdas.common.constants import HEARD_TABLE_NAME


def _create_heard_table():
    ddb = boto3.resource("dynamodb", region_name="us-east-1")
    ddb.create_table(
        TableName=HEARD_TABLE_NAME,
        KeySchema=[
            {"AttributeName": "trackKey", "KeyType": "HASH"},
            {"AttributeName": "raterEmail", "KeyType": "RANGE"},
        ],
        AttributeDefinitions=[
            {"AttributeName": "trackKey", "AttributeType": "S"},
            {"AttributeName": "raterEmail", "AttributeType": "S"},
        ],
        ProvisionedThroughput={"ReadCapacityUnits": 5, "WriteCapacityUnits": 5},
    )
    return ddb


def _item(track_id, played_at="2026-07-20T12:00:00.000Z", name="Song"):
    return {
        "track": {"id": track_id, "name": name, "artists": [{"name": "Artist"}]},
        "played_at": played_at,
    }


class TestAutoHeardPure:
    def test_track_keys_from_recently_played_dedups(self):
        from lambdas.cron_auto_heard.handler import track_keys_from_recently_played

        items = [_item("abc"), _item("def"), _item("abc", played_at="2026-07-20T09:00:00Z")]
        keys = [tk for tk, _epoch in track_keys_from_recently_played(items)]
        assert keys == ["spotify:abc", "spotify:def"]

    def test_skips_items_without_track_id(self):
        from lambdas.cron_auto_heard.handler import track_keys_from_recently_played

        items = [{"track": {}, "played_at": "2026-07-20T12:00:00Z"}, _item("abc")]
        keys = [tk for tk, _epoch in track_keys_from_recently_played(items)]
        assert keys == ["spotify:abc"]


class TestAutoHeardCore:
    def test_marks_recently_played_heard_for_dom(self):
        with mock_aws():
            _create_heard_table()
            from lambdas.cron_auto_heard.handler import run_auto_heard
            from lambdas.common.heard_dynamo import caller_heard_map, set_heard

            def persist(track_key, rater_email, heard_at):
                set_heard(track_key, rater_email, True, heard_at=heard_at)

            items = [_item("abc"), _item("def")]
            summary = run_auto_heard(items, "dom@example.com", persist)

            assert summary["marked"] == 2
            assert summary["rater"] == "dom@example.com"
            heard = caller_heard_map({"spotify:abc", "spotify:def"}, "dom@example.com")
            assert heard == {"spotify:abc": True, "spotify:def": True}
