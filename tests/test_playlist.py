"""
RED-before-GREEN: xomtracks' vendored Playlist class (lambdas/common/playlist.py).

Per PLAN.md Approach: reuse aiohttp_build_playlist (on-the-spot + first
rolling build) and aiohttp_update_playlist/aiohttp_replace_playlist_songs
(atomic PUT-replace for the rolling refresh). xomify's create_playlist
hardcodes `public: True` -- xomtracks parameterizes the flag, DEFAULT
public, and keeps public for all three playlists (both rolling +
on-the-spot) per PLAN.md's locked decision.
"""

import pytest

from tests.fakes_aiohttp import FakeResponse, FakeSession


class TestCreatePlaylistPublicFlag:
    @pytest.mark.asyncio
    async def test_default_is_public(self):
        from lambdas.common.playlist import Playlist

        session = FakeSession()
        session.queue("post", FakeResponse(201, {"id": "pl1"}))

        playlist = Playlist("user1", "Xomtracks In (30d)", "desc", {"Authorization": "Bearer AT"}, session)
        await playlist.aiohttp_create_playlist()

        method, url, kwargs = session.calls[0]
        assert kwargs["json"]["public"] is True
        assert playlist.id == "pl1"

    @pytest.mark.asyncio
    async def test_can_override_to_private(self):
        from lambdas.common.playlist import Playlist

        session = FakeSession()
        session.queue("post", FakeResponse(201, {"id": "pl2"}))

        playlist = Playlist("user1", "name", "desc", {"Authorization": "Bearer AT"}, session, public=False)
        await playlist.aiohttp_create_playlist()

        method, url, kwargs = session.calls[0]
        assert kwargs["json"]["public"] is False


class TestAiohttpBuildPlaylist:
    @pytest.mark.asyncio
    async def test_full_build_sequence_public(self, monkeypatch):
        from lambdas.common.playlist import Playlist

        session = FakeSession()
        session.queue("post", FakeResponse(201, {"id": "pl1"}))  # create
        session.queue("put", FakeResponse(202, {}))  # image
        session.queue("post", FakeResponse(201, {}))  # add songs

        playlist = Playlist("user1", "name", "desc", {"Authorization": "Bearer AT"}, session)

        async def no_sleep(*args, **kwargs):
            return None
        monkeypatch.setattr("lambdas.common.playlist.asyncio.sleep", no_sleep)

        await playlist.aiohttp_build_playlist(["spotify:track:a", "spotify:track:b"], "base64img")

        assert playlist.id == "pl1"
        # create -> image -> add songs, in that order
        methods = [c[0] for c in session.calls]
        assert methods == ["post", "put", "post"]
        create_call = session.calls[0]
        assert create_call[2]["json"]["public"] is True


class TestAiohttpReplacePlaylistSongs:
    @pytest.mark.asyncio
    async def test_empty_uri_list_clears_playlist(self, monkeypatch):
        from lambdas.common.playlist import Playlist

        session = FakeSession()
        session.queue("put", FakeResponse(200, {"snapshot_id": "s1"}))

        playlist = Playlist("user1", "name", "desc", {"Authorization": "Bearer AT"}, session)
        playlist.set_id("pl1")

        await playlist.aiohttp_update_playlist([])

        method, url, kwargs = session.calls[0]
        assert method == "put"
        assert kwargs["json"]["uris"] == []

    @pytest.mark.asyncio
    async def test_over_100_uris_put_then_post(self):
        from lambdas.common.playlist import Playlist

        uris = [f"spotify:track:{i}" for i in range(150)]

        session = FakeSession()
        session.queue("put", FakeResponse(200, {"snapshot_id": "s1"}))
        session.queue("post", FakeResponse(201, {"snapshot_id": "s2"}))

        playlist = Playlist("user1", "name", "desc", {"Authorization": "Bearer AT"}, session)
        playlist.set_id("pl1")

        await playlist.aiohttp_update_playlist(uris)

        methods = [c[0] for c in session.calls]
        assert methods == ["put", "post"]
        put_call = session.calls[0]
        post_call = session.calls[1]
        assert len(put_call[2]["json"]["uris"]) == 100
        assert len(post_call[2]["json"]["uris"]) == 50
