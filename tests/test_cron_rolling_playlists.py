"""
RED-before-GREEN: rolling-playlists cron.

Covers the pure playable-URI selection (filter + newest-first + ordered
dedup) and the orchestration (create -> persist id; update-in-place -> no
re-persist; recreate-on-failure fallback). Network + SSM + Dynamo edges are
patched -- no real AWS.

OWNER_SCOPING_ENABLED defaults to false in the test env (conftest), so these
exercise the single service/default-owner path (legacy GSI-1 + SSM ids) --
Dom's exact pre-Phase-2 behavior. The per-owner iteration (scoping ON) is
covered in test_cron_rolling_playlists_owners.py.
"""

import lambdas.cron_rolling_playlists.handler as H
from lambdas.common.constants import DEFAULT_OWNER_ID


class _FakeSpotify:
    headers = {"Authorization": "Bearer AT"}


def _fake_build_owner_client():
    async def _build(session, owner_id):
        return _FakeSpotify(), "user1", True  # (spotify, user_id, is_service_fallback)
    return _build


class TestPlayableUris:
    def test_filters_orders_and_dedupes(self, monkeypatch):
        shares = [
            {"matchStatus": "matched", "resolvedSpotifyUri": "spotify:track:a", "messageDate": 100},
            {"matchStatus": "pending", "resolvedSpotifyUri": None, "messageDate": 200},
            {"matchStatus": "matched", "resolvedSpotifyUri": "spotify:track:b", "messageDate": 300},
            {"matchStatus": "manual", "resolvedSpotifyUri": "spotify:track:a", "messageDate": 400},
            {"matchStatus": "unmatched", "resolvedSpotifyUri": None, "messageDate": 500},
        ]
        monkeypatch.setattr(H, "query_shares_by_direction", lambda d, s: list(shares))

        uris = H._playable_uris(DEFAULT_OWNER_ID, "in", 0)

        # newest-first: 400(a) then 300(b); 100(a) is a dup, pending/unmatched dropped.
        assert uris == ["spotify:track:a", "spotify:track:b"]

    def test_empty_window_yields_no_uris(self, monkeypatch):
        monkeypatch.setattr(H, "query_shares_by_direction", lambda d, s: [])
        assert H._playable_uris(DEFAULT_OWNER_ID, "out", 0) == []


class TestRebuild:
    def _wire(self, monkeypatch, *, existing, upsert):
        monkeypatch.setattr(H, "build_owner_client", _fake_build_owner_client())
        monkeypatch.setattr(
            H, "query_shares_by_direction",
            lambda d, s: [{"matchStatus": "matched", "resolvedSpotifyUri": f"spotify:track:{d}", "messageDate": 1}],
        )
        monkeypatch.setattr(H, "get_ssm_param", lambda name: existing)
        puts: list = []
        monkeypatch.setattr(H, "put_ssm_param", lambda name, val: puts.append((name, val)))
        monkeypatch.setattr(H, "upsert_playlist", upsert)
        return puts

    def _playlists(self, result):
        """Single service owner in these tests -> unwrap to its per-direction map."""
        owners = result["owners"]
        assert len(owners) == 1
        assert owners[0]["ownerId"] == DEFAULT_OWNER_ID
        return owners[0]["playlists"]

    def test_creates_both_and_persists_ids(self, monkeypatch):
        captured = []

        async def fake_upsert(session, spotify, user_id, *, playlist_id, name, description, uris, image=None):
            captured.append({"playlist_id": playlist_id, "name": name, "uris": uris, "image": image})
            return "in-id" if "With Me" in name else "out-id"

        puts = self._wire(monkeypatch, existing="unset", upsert=fake_upsert)

        out = self._playlists(H.rebuild_rolling_playlists())

        assert set(out) == {"in", "out"}
        assert out["in"]["created"] is True and out["out"]["created"] is True
        assert out["in"]["playlistId"] == "in-id"
        assert out["in"]["url"] == "https://open.spotify.com/playlist/in-id"
        assert out["in"]["trackCount"] == 1
        # both were newly created -> both ids persisted to SSM
        assert len(puts) == 2
        # created path passes playlist_id=None (unset placeholder normalized away)
        assert all(c["playlist_id"] == "unset" for c in captured)
        # each direction gets its OWN xomify-branded cover (green in / purple out)
        by_dir = {"in" if "With Me" in c["name"] else "out": c["image"] for c in captured}
        assert by_dir["in"] == H.XOMIFY_COVER_IN_BASE_64
        assert by_dir["out"] == H.XOMIFY_COVER_OUT_BASE_64
        assert by_dir["in"] != by_dir["out"]

    def test_update_in_place_does_not_repersist(self, monkeypatch):
        async def fake_upsert(session, spotify, user_id, *, playlist_id, name, description, uris, image=None):
            return playlist_id  # unchanged id == existing

        puts = self._wire(monkeypatch, existing="existing-id", upsert=fake_upsert)

        out = self._playlists(H.rebuild_rolling_playlists())

        assert out["in"]["created"] is False
        assert out["in"]["playlistId"] == "existing-id"
        assert puts == []  # id unchanged -> no SSM write

    def test_recreate_only_when_playlist_gone(self, monkeypatch):
        async def fake_upsert(session, spotify, user_id, *, playlist_id, name, description, uris, image=None):
            if playlist_id and playlist_id != "unset":
                raise RuntimeError("404 playlist gone")
            return "fresh-id"

        async def gone(session, spotify, pid):
            return False  # genuinely deleted -> recreate is correct

        monkeypatch.setattr(H, "playlist_exists", gone)
        puts = self._wire(monkeypatch, existing="stale-id", upsert=fake_upsert)

        out = self._playlists(H.rebuild_rolling_playlists())

        assert out["in"]["playlistId"] == "fresh-id"
        assert out["in"]["created"] is True
        # both directions recreated -> both new ids persisted
        assert len(puts) == 2
        assert all(val == "fresh-id" for _, val in puts)

    def test_transient_failure_never_duplicates(self, monkeypatch):
        """Update throws but the playlist still exists -> re-raise, NO recreate,
        NO new id persisted (the core 'never create duplicates' guarantee)."""
        create_calls = {"n": 0}

        async def fake_upsert(session, spotify, user_id, *, playlist_id, name, description, uris, image=None):
            if playlist_id and playlist_id != "unset":
                raise RuntimeError("429 transient")
            create_calls["n"] += 1
            return "should-not-happen"

        async def still_there(session, spotify, pid):
            return True

        monkeypatch.setattr(H, "playlist_exists", still_there)
        puts = self._wire(monkeypatch, existing="live-id", upsert=fake_upsert)

        import pytest
        with pytest.raises(Exception):
            H.rebuild_rolling_playlists()

        assert create_calls["n"] == 0  # never created a duplicate
        assert puts == []  # never persisted a new id

    def test_handler_returns_summary(self, monkeypatch, mock_context):
        async def fake_upsert(session, spotify, user_id, *, playlist_id, name, description, uris, image=None):
            return "pid"

        self._wire(monkeypatch, existing="unset", upsert=fake_upsert)
        result = H.handler({}, mock_context)
        assert "owners" in result
        assert result["owners"][0]["ownerId"] == DEFAULT_OWNER_ID
