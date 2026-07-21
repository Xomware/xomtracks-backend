"""
RED-before-GREEN: POST /shares/{id}/match-override -- manual override
endpoint. Accepts a Spotify track id/URL, sets matchStatus=manual. Backs
the UI "pick the match" affordance for permanently-unmatched shares.
"""

import json
from unittest.mock import AsyncMock, patch

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
    return boto3.resource("dynamodb", region_name="us-east-1").Table(SHARES_TABLE_NAME)


@pytest.fixture
def seeded_share():
    with mock_aws():
        table = _create_table()
        table.put_item(Item={
            "shareId": "share-1", "messageGuid": "g1", "direction": "in", "sharerHandle": "+1",
            "chatId": "c1", "platform": "soundcloud", "sourceUrl": "https://soundcloud.com/a/b",
            "messageDate": 1753000000, "matchStatus": "unmatched", "createdAt": "x",
        })
        yield table


def _fake_track(track_id="abc123"):
    return {"id": track_id, "name": "Song", "artists": [{"name": "Artist"}], "uri": f"spotify:track:{track_id}"}


class TestSharesMatchOverride:
    @patch("lambdas.shares_match_override.handler._build_spotify_client")
    def test_valid_override_sets_manual_status(self, mock_build_spotify, seeded_share, authorized_event, mock_context):
        from lambdas.shares_match_override.handler import handler

        fake_spotify = AsyncMock()
        fake_spotify.aiohttp_get_track = AsyncMock(return_value=_fake_track("abc123"))
        mock_build_spotify.return_value = fake_spotify

        event = authorized_event(
            httpMethod="POST",
            pathParameters={"shareId": "share-1"},
            body=json.dumps({"spotifyTrackId": "abc123"}),
        )
        response = handler(event, mock_context)
        body = json.loads(response["body"])

        assert response["statusCode"] == 200
        assert body["data"]["matchStatus"] == "manual"
        assert body["data"]["resolvedSpotifyId"] == "abc123"

    @patch("lambdas.shares_match_override.handler._build_spotify_client")
    def test_accepts_full_spotify_url(self, mock_build_spotify, seeded_share, authorized_event, mock_context):
        from lambdas.shares_match_override.handler import handler

        fake_spotify = AsyncMock()
        fake_spotify.aiohttp_get_track = AsyncMock(return_value=_fake_track("abc123"))
        mock_build_spotify.return_value = fake_spotify

        event = authorized_event(
            httpMethod="POST",
            pathParameters={"shareId": "share-1"},
            body=json.dumps({"spotifyTrackId": "https://open.spotify.com/track/abc123?si=x"}),
        )
        response = handler(event, mock_context)
        assert response["statusCode"] == 200
        fake_spotify.aiohttp_get_track.assert_awaited_once_with("abc123")

    def test_missing_share_id_path_param_is_400(self, seeded_share, authorized_event, mock_context):
        from lambdas.shares_match_override.handler import handler

        event = authorized_event(httpMethod="POST", body=json.dumps({"spotifyTrackId": "abc123"}))
        response = handler(event, mock_context)
        assert response["statusCode"] == 400

    def test_unknown_share_id_is_404(self, seeded_share, authorized_event, mock_context):
        from lambdas.shares_match_override.handler import handler

        event = authorized_event(
            httpMethod="POST",
            pathParameters={"shareId": "does-not-exist"},
            body=json.dumps({"spotifyTrackId": "abc123"}),
        )
        response = handler(event, mock_context)
        assert response["statusCode"] == 404

    def test_no_auth_context_is_401(self, seeded_share, public_event, mock_context):
        from lambdas.shares_match_override.handler import handler

        event = public_event(
            httpMethod="POST",
            pathParameters={"shareId": "share-1"},
            body=json.dumps({"spotifyTrackId": "abc123"}),
        )
        response = handler(event, mock_context)
        assert response["statusCode"] == 401
