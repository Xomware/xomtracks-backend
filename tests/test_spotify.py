"""
RED-before-GREEN: xomtracks' vendored + trimmed Spotify client
(lambdas/common/spotify.py).

Per PLAN.md: trimmed from xomify-backend's spotify.py to
aiohttp_get_access_token / get_access_token / _persist_rotated_refresh_token
/ batch track hydrate, PLUS two genuinely-new additions matching.py needs
(aiohttp_get_track, aiohttp_search_track) -- xomify's copy has neither since
groups_add_song_url hits the /tracks/{id} endpoint inline rather than via a
Spotify class method.
"""

import pytest
from unittest.mock import patch

from tests.fakes_aiohttp import FakeResponse, FakeSession


@pytest.fixture
def user():
    return {"userId": "u1", "email": "dom@example.com", "refreshToken": "old-refresh-token"}


class TestAiohttpGetAccessToken:
    @pytest.mark.asyncio
    async def test_success_no_rotation(self, user):
        from lambdas.common.spotify import Spotify

        session = FakeSession()
        session.queue("post", FakeResponse(200, {"access_token": "AT1"}))

        spotify = Spotify(user, session)
        token = await spotify.aiohttp_get_access_token()

        assert token == "AT1"
        assert spotify.refresh_token == "old-refresh-token"

    @pytest.mark.asyncio
    async def test_rotated_refresh_token_persisted(self, user):
        from lambdas.common.spotify import Spotify

        session = FakeSession()
        session.queue("post", FakeResponse(200, {"access_token": "AT1", "refresh_token": "NEW-token"}))

        spotify = Spotify(user, session)
        with patch.object(spotify, "_persist_rotated_refresh_token") as persist_mock:
            token = await spotify.aiohttp_get_access_token()

        assert token == "AT1"
        assert spotify.refresh_token == "NEW-token"
        persist_mock.assert_called_once_with("NEW-token")

    @pytest.mark.asyncio
    async def test_error_response_raises(self, user):
        from lambdas.common.spotify import Spotify

        session = FakeSession()
        session.queue("post", FakeResponse(400, {"error": "invalid_grant"}))

        spotify = Spotify(user, session)
        with pytest.raises(Exception):
            await spotify.aiohttp_get_access_token()


class TestAppOnlyClientCredentials:
    """NEW (xomtracks-only): client-credentials app-token auth for the
    read-only matcher endpoints -- no user refresh token required."""

    def test_sync_app_token_populates_headers(self):
        from lambdas.common import spotify as spotify_mod
        from lambdas.common.spotify import Spotify
        import requests

        class FakeSyncResponse:
            status_code = 200

            def json(self):
                return {"access_token": "APP-TOKEN"}

        with patch.object(requests, "post", return_value=FakeSyncResponse()):
            client = Spotify(app_only=True)

        assert client.app_only is True
        assert client.refresh_token == ""
        assert client.access_token == "APP-TOKEN"
        assert client.headers["Authorization"] == "Bearer APP-TOKEN"

    @pytest.mark.asyncio
    async def test_async_app_token_initialize(self):
        from lambdas.common.spotify import Spotify

        session = FakeSession()
        session.queue("post", FakeResponse(200, {"access_token": "ASYNC-APP-TOKEN"}))

        client = Spotify(app_only=True, session=session)
        await client.aiohttp_initialize_app_token()

        assert client.access_token == "ASYNC-APP-TOKEN"
        assert client.headers["Authorization"] == "Bearer ASYNC-APP-TOKEN"

    @pytest.mark.asyncio
    async def test_async_app_token_error_raises(self):
        from lambdas.common.spotify import Spotify

        session = FakeSession()
        session.queue("post", FakeResponse(400, {"error": "invalid_client"}))

        client = Spotify(app_only=True, session=session)
        with pytest.raises(Exception):
            await client.aiohttp_initialize_app_token()


class TestBatchGetTracks:
    def test_chunks_over_50_and_filters_none(self, user):
        from lambdas.common.spotify import Spotify
        import requests

        spotify = Spotify.__new__(Spotify)
        spotify.headers = {"Authorization": "Bearer AT1"}

        ids = [f"id{i}" for i in range(75)]

        call_log = []

        class FakeSyncResponse:
            def __init__(self, ids_chunk):
                self.status_code = 200
                self._ids_chunk = ids_chunk

            def json(self):
                return {"tracks": [{"id": i} if i != "id5" else None for i in self._ids_chunk]}

        def fake_get(url, headers=None):
            requested = url.split("ids=")[1].split(",")
            call_log.append(requested)
            return FakeSyncResponse(requested)

        with patch.object(requests, "get", side_effect=fake_get):
            tracks = spotify.get_tracks_by_ids(ids)

        # 75 ids -> 2 batches of <=50
        assert len(call_log) == 2
        assert len(call_log[0]) == 50
        assert len(call_log[1]) == 25
        # id5 was filtered out as null
        assert all(t["id"] != "id5" for t in tracks) if tracks else True

    def test_empty_ids_returns_empty_list(self, user):
        from lambdas.common.spotify import Spotify

        spotify = Spotify.__new__(Spotify)
        spotify.headers = {}
        assert spotify.get_tracks_by_ids([]) == []
        assert spotify.get_tracks_by_ids(None) == []


class TestBatchGetArtists:
    def test_chunks_over_50_and_filters_none(self, user):
        from lambdas.common.spotify import Spotify
        import requests

        spotify = Spotify.__new__(Spotify)
        spotify.headers = {"Authorization": "Bearer AT1"}

        ids = [f"art{i}" for i in range(75)]
        call_log = []

        class FakeSyncResponse:
            def __init__(self, ids_chunk):
                self.status_code = 200
                self._ids_chunk = ids_chunk

            def json(self):
                return {"artists": [
                    {"id": i, "genres": ["rock"]} if i != "art5" else None
                    for i in self._ids_chunk
                ]}

        def fake_get(url, headers=None):
            requested = url.split("ids=")[1].split(",")
            call_log.append(requested)
            return FakeSyncResponse(requested)

        with patch.object(requests, "get", side_effect=fake_get):
            artists = spotify.get_artists_by_ids(ids)

        assert len(call_log) == 2
        assert len(call_log[0]) == 50
        assert len(call_log[1]) == 25
        assert all(a["id"] != "art5" for a in artists)

    def test_empty_ids_returns_empty_list(self, user):
        from lambdas.common.spotify import Spotify

        spotify = Spotify.__new__(Spotify)
        spotify.headers = {}
        assert spotify.get_artists_by_ids([]) == []
        assert spotify.get_artists_by_ids(None) == []

    def test_non_200_raises(self, user):
        from lambdas.common.spotify import Spotify
        import requests

        spotify = Spotify.__new__(Spotify)
        spotify.headers = {"Authorization": "Bearer AT1"}

        class FakeSyncResponse:
            status_code = 429
            text = "rate limited"

        with patch.object(requests, "get", return_value=FakeSyncResponse()):
            with pytest.raises(Exception, match="429"):
                spotify.get_artists_by_ids(["art1"])


class TestAiohttpGetTrack:
    @pytest.mark.asyncio
    async def test_fetches_single_track(self, user):
        from lambdas.common.spotify import Spotify

        session = FakeSession()
        session.queue("get", FakeResponse(200, {"id": "abc", "name": "Song", "artists": [{"name": "Artist"}]}))

        spotify = Spotify.__new__(Spotify)
        spotify.aiohttp_session = session
        spotify.headers = {"Authorization": "Bearer AT1"}

        track = await spotify.aiohttp_get_track("abc")
        assert track["id"] == "abc"
        assert track["name"] == "Song"

    @pytest.mark.asyncio
    async def test_not_found_returns_none(self, user):
        from lambdas.common.spotify import Spotify

        session = FakeSession()
        session.queue("get", FakeResponse(404, {}))

        spotify = Spotify.__new__(Spotify)
        spotify.aiohttp_session = session
        spotify.headers = {"Authorization": "Bearer AT1"}

        track = await spotify.aiohttp_get_track("missing")
        assert track is None


class TestAiohttpSearchTrack:
    @pytest.mark.asyncio
    async def test_returns_track_list(self, user):
        from lambdas.common.spotify import Spotify

        session = FakeSession()
        session.queue(
            "get",
            FakeResponse(200, {"tracks": {"items": [{"id": "t1", "name": "Song", "artists": [{"name": "Artist"}]}]}}),
        )

        spotify = Spotify.__new__(Spotify)
        spotify.aiohttp_session = session
        spotify.headers = {"Authorization": "Bearer AT1"}

        results = await spotify.aiohttp_search_track("Artist Song")
        assert len(results) == 1
        assert results[0]["id"] == "t1"

    @pytest.mark.asyncio
    async def test_no_results_returns_empty_list(self, user):
        from lambdas.common.spotify import Spotify

        session = FakeSession()
        session.queue("get", FakeResponse(200, {"tracks": {"items": []}}))

        spotify = Spotify.__new__(Spotify)
        spotify.aiohttp_session = session
        spotify.headers = {"Authorization": "Bearer AT1"}

        results = await spotify.aiohttp_search_track("nonsense query")
        assert results == []


class TestAiohttpRecentlyPlayed:
    @pytest.mark.asyncio
    async def test_returns_items(self, user):
        from lambdas.common.spotify import Spotify

        session = FakeSession()
        session.queue(
            "get",
            FakeResponse(200, {"items": [
                {"track": {"id": "t1", "name": "Song"}, "played_at": "2026-07-20T12:00:00Z"},
            ]}),
        )

        spotify = Spotify.__new__(Spotify)
        spotify.aiohttp_session = session
        spotify.headers = {"Authorization": "Bearer AT1"}

        items = await spotify.aiohttp_get_recently_played(limit=50)
        assert len(items) == 1
        assert items[0]["track"]["id"] == "t1"
        # limit is capped at Spotify's max of 50.
        assert "limit=50" in session.calls[0][1]

    @pytest.mark.asyncio
    async def test_missing_scope_403_raises(self, user):
        from lambdas.common.spotify import Spotify

        session = FakeSession()
        session.queue("get", FakeResponse(403, {"error": "insufficient scope"}))

        spotify = Spotify.__new__(Spotify)
        spotify.aiohttp_session = session
        spotify.headers = {"Authorization": "Bearer AT1"}

        with pytest.raises(Exception, match="403"):
            await spotify.aiohttp_get_recently_played()
