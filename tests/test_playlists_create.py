"""
RED-before-GREEN: on-the-spot POST /playlists/create (authed).

Covers auth gate, empty-selection rejection, share+track resolution with
ordered dedup + skipped-unmatched reporting, and the no-resolvable-tracks
400. Spotify + Dynamo edges are patched -- no real AWS/network.
"""

import json

import lambdas.playlists_create.handler as H
from lambdas.common.models import CreatePlaylistRequest


class TestCreatePlaylistModel:
    def test_normalizes_track_ids_and_requires_selection(self):
        req = CreatePlaylistRequest(
            name="  My Mix  ",
            trackIds=["https://open.spotify.com/track/4uLU6hMCjMI75M1A2tKUQC", "spotify:track:abc"],
        )
        assert req.name == "My Mix"
        assert req.trackIds == ["4uLU6hMCjMI75M1A2tKUQC", "abc"]
        assert req.has_selection() is True

    def test_empty_is_no_selection(self):
        assert CreatePlaylistRequest(name="x").has_selection() is False


class TestHandler:
    def test_requires_auth(self, public_event, mock_context):
        resp = H.handler(public_event(body=json.dumps({"name": "x", "trackIds": ["abc"]})), mock_context)
        assert resp["statusCode"] == 401

    def test_missing_selection_is_400(self, authorized_event, mock_context):
        resp = H.handler(authorized_event(body=json.dumps({"name": "x"})), mock_context)
        assert resp["statusCode"] == 400

    def test_creates_from_shares_and_tracks(self, monkeypatch, authorized_event, mock_context):
        monkeypatch.setattr(
            H, "get_share",
            lambda sid: {"resolvedSpotifyUri": "spotify:track:s1"} if sid == "sh1" else None,
        )
        captured = {}

        async def fake_create(session, name, description, uris, owner_email=None):
            captured["name"] = name
            captured["uris"] = uris
            captured["description"] = description
            return "plid"

        monkeypatch.setattr(H, "create_playlist", fake_create)

        body = {
            "name": "My Mix",
            "shareIds": ["sh1", "sh_missing"],
            "trackIds": ["4uLU6hMCjMI75M1A2tKUQC"],
        }
        resp = H.handler(authorized_event(body=json.dumps(body)), mock_context)

        assert resp["statusCode"] == 200
        data = json.loads(resp["body"])["data"]
        assert data["playlistId"] == "plid"
        assert data["url"] == "https://open.spotify.com/playlist/plid"
        assert data["trackCount"] == 2
        assert data["skippedShareIds"] == ["sh_missing"]
        # order preserved: matched share first, then normalized track id
        assert captured["uris"] == ["spotify:track:s1", "spotify:track:4uLU6hMCjMI75M1A2tKUQC"]

    def test_no_resolvable_tracks_is_400(self, monkeypatch, authorized_event, mock_context):
        monkeypatch.setattr(H, "get_share", lambda sid: None)
        called = {"create": False}

        async def fake_create(session, name, description, uris, owner_email=None):
            called["create"] = True
            return "nope"

        monkeypatch.setattr(H, "create_playlist", fake_create)

        resp = H.handler(authorized_event(body=json.dumps({"name": "x", "shareIds": ["missing"]})), mock_context)
        assert resp["statusCode"] == 400
        assert called["create"] is False  # never hit Spotify with an empty list

    def test_dedupes_shared_and_explicit_track(self, monkeypatch, authorized_event, mock_context):
        monkeypatch.setattr(
            H, "get_share",
            lambda sid: {"resolvedSpotifyUri": "spotify:track:dup"},
        )
        captured = {}

        async def fake_create(session, name, description, uris, owner_email=None):
            captured["uris"] = uris
            return "plid"

        monkeypatch.setattr(H, "create_playlist", fake_create)

        body = {"name": "Mix", "shareIds": ["sh1"], "trackIds": ["dup"]}
        resp = H.handler(authorized_event(body=json.dumps(body)), mock_context)
        assert resp["statusCode"] == 200
        assert captured["uris"] == ["spotify:track:dup"]  # deduped across sources
