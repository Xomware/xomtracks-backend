"""
RED-before-GREEN: lambdas/common/matching.py -- the cross-platform matching
module (PLAN.md Phase 3, "genuinely new code").

Branches by platform:
- spotify -> regex-extract track id -> hydrate via GET /v1/tracks/{id} ->
  matched, matchConfidence=1.0
- soundcloud -> resolve title+artist (xomcloud's scraped client_id path) ->
  Spotify /search -> fuzzy match
- apple -> resolve title+artist (public itunes.apple.com/lookup, no auth) ->
  Spotify /search -> fuzzy match

Fuzzy match: normalize artist+title, token-set ratio, confidence threshold.
Above -> matched; below -> unmatched (permanent-unmatched is expected for
SC remixes/bootlegs not on Spotify -- excluded from playlists, not an error).

All external HTTP is mocked/injected -- no real network calls in tests.
"""

import pytest

from lambdas.common.matching import (
    extract_spotify_track_id,
    fuzzy_best_match,
    match_share,
    apply_manual_override,
)


class FakeSpotify:
    def __init__(self, track_by_id=None, search_results=None):
        self._track_by_id = track_by_id or {}
        self._search_results = search_results if search_results is not None else []
        self.search_calls = []
        self.get_track_calls = []

    async def aiohttp_get_track(self, track_id):
        self.get_track_calls.append(track_id)
        return self._track_by_id.get(track_id)

    async def aiohttp_search_track(self, query, limit=5):
        self.search_calls.append(query)
        return self._search_results


def _spotify_track(track_id="abc123", name="Song Name", artist="Artist Name"):
    return {
        "id": track_id,
        "name": name,
        "artists": [{"name": artist}],
        "uri": f"spotify:track:{track_id}",
        "album": {
            "name": "Album Name",
            "images": [
                {"url": "https://i.scdn.co/image/large", "height": 640, "width": 640},
                {"url": "https://i.scdn.co/image/medium", "height": 300, "width": 300},
                {"url": "https://i.scdn.co/image/small", "height": 64, "width": 64},
            ],
        },
    }


class TestExtractSpotifyTrackId:
    def test_extracts_from_open_spotify_url(self):
        assert extract_spotify_track_id(
            "https://open.spotify.com/track/4uLU6hMCjMI75M1A2tKUQC?si=abc"
        ) == "4uLU6hMCjMI75M1A2tKUQC"

    def test_extracts_from_uri(self):
        assert extract_spotify_track_id("spotify:track:4uLU6hMCjMI75M1A2tKUQC") == "4uLU6hMCjMI75M1A2tKUQC"

    def test_returns_none_for_non_spotify_url(self):
        assert extract_spotify_track_id("https://soundcloud.com/artist/track") is None


class TestFuzzyBestMatch:
    def test_exact_match_high_score(self):
        candidates = [_spotify_track(track_id="t1", name="Blinding Lights", artist="The Weeknd")]
        best, score = fuzzy_best_match("Blinding Lights", "The Weeknd", candidates)
        assert best["id"] == "t1"
        assert score > 0.9

    def test_no_candidates_returns_none(self):
        best, score = fuzzy_best_match("Song", "Artist", [])
        assert best is None
        assert score == 0.0

    def test_picks_highest_scoring_candidate(self):
        candidates = [
            _spotify_track(track_id="wrong", name="Completely Different Track", artist="Someone Else"),
            _spotify_track(track_id="right", name="Blinding Lights", artist="The Weeknd"),
        ]
        best, score = fuzzy_best_match("Blinding Lights", "The Weeknd", candidates)
        assert best["id"] == "right"


class TestMatchShareSpotifyBranch:
    @pytest.mark.asyncio
    async def test_valid_spotify_url_matches_with_confidence_1(self):
        share = {"platform": "spotify", "sourceUrl": "https://open.spotify.com/track/abc123"}
        spotify = FakeSpotify(track_by_id={"abc123": _spotify_track("abc123", "Song", "Artist")})

        result = await match_share(share, spotify)

        assert result["matchStatus"] == "matched"
        assert result["matchConfidence"] == 1.0
        assert result["resolvedSpotifyId"] == "abc123"
        assert result["resolvedSpotifyUri"] == "spotify:track:abc123"
        assert result["trackTitle"] == "Song"
        assert result["trackArtist"] == "Artist"
        # Album art + name are persisted so the browse feed can render covers
        # without any client-side Spotify calls. The ~300px (medium) image is
        # chosen for card display, not the 640px original.
        assert result["albumName"] == "Album Name"
        assert result["albumArtUrl"] == "https://i.scdn.co/image/medium"

    @pytest.mark.asyncio
    async def test_unmatched_has_null_album_fields(self):
        share = {"platform": "spotify", "sourceUrl": "https://open.spotify.com/track/gone"}
        spotify = FakeSpotify(track_by_id={})

        result = await match_share(share, spotify)

        assert result["albumArtUrl"] is None
        assert result["albumName"] is None

    @pytest.mark.asyncio
    async def test_track_without_album_images_degrades_to_null_art(self):
        share = {"platform": "spotify", "sourceUrl": "https://open.spotify.com/track/noart"}
        track = {"id": "noart", "name": "S", "artists": [{"name": "A"}], "uri": "spotify:track:noart"}
        spotify = FakeSpotify(track_by_id={"noart": track})

        result = await match_share(share, spotify)

        assert result["matchStatus"] == "matched"
        assert result["albumArtUrl"] is None
        assert result["albumName"] is None

    @pytest.mark.asyncio
    async def test_track_not_found_is_unmatched(self):
        share = {"platform": "spotify", "sourceUrl": "https://open.spotify.com/track/gone"}
        spotify = FakeSpotify(track_by_id={})

        result = await match_share(share, spotify)

        assert result["matchStatus"] == "unmatched"
        assert result["matchConfidence"] is None

    @pytest.mark.asyncio
    async def test_unparseable_url_is_unmatched(self):
        share = {"platform": "spotify", "sourceUrl": "https://open.spotify.com/not-a-track"}
        spotify = FakeSpotify()

        result = await match_share(share, spotify)

        assert result["matchStatus"] == "unmatched"
        assert spotify.get_track_calls == []


class TestMatchShareSoundcloudBranch:
    @pytest.mark.asyncio
    async def test_resolves_metadata_then_searches_and_matches_above_threshold(self):
        share = {"platform": "soundcloud", "sourceUrl": "https://soundcloud.com/artist/blinding-lights"}
        spotify = FakeSpotify(search_results=[_spotify_track("t1", "Blinding Lights", "The Weeknd")])

        async def fake_resolver(url):
            return ("Blinding Lights", "The Weeknd")

        result = await match_share(share, spotify, soundcloud_resolver=fake_resolver)

        assert result["matchStatus"] == "matched"
        assert result["resolvedSpotifyId"] == "t1"
        assert spotify.search_calls == ["The Weeknd Blinding Lights"]

    @pytest.mark.asyncio
    async def test_below_threshold_is_unmatched_not_an_error(self):
        share = {"platform": "soundcloud", "sourceUrl": "https://soundcloud.com/artist/underground-remix"}
        spotify = FakeSpotify(search_results=[_spotify_track("t1", "Completely Unrelated Track", "Nobody")])

        async def fake_resolver(url):
            return ("Underground Bootleg Remix VIP", "DJ Nobody Known")

        result = await match_share(share, spotify, soundcloud_resolver=fake_resolver)

        assert result["matchStatus"] == "unmatched"
        assert result["resolvedSpotifyId"] is None

    @pytest.mark.asyncio
    async def test_resolver_failure_is_unmatched_not_raised(self):
        share = {"platform": "soundcloud", "sourceUrl": "https://soundcloud.com/artist/track"}
        spotify = FakeSpotify()

        async def failing_resolver(url):
            raise RuntimeError("SoundCloud API down")

        result = await match_share(share, spotify, soundcloud_resolver=failing_resolver)

        assert result["matchStatus"] == "unmatched"


class TestMatchShareAppleBranch:
    @pytest.mark.asyncio
    async def test_resolves_metadata_then_searches_and_matches(self):
        share = {"platform": "apple", "sourceUrl": "https://music.apple.com/us/song/blinding-lights/1488408555"}
        spotify = FakeSpotify(search_results=[_spotify_track("t1", "Blinding Lights", "The Weeknd")])

        async def fake_resolver(url):
            return ("Blinding Lights", "The Weeknd")

        result = await match_share(share, spotify, apple_resolver=fake_resolver)

        assert result["matchStatus"] == "matched"
        assert result["resolvedSpotifyId"] == "t1"

    @pytest.mark.asyncio
    async def test_resolver_returns_none_is_unmatched(self):
        share = {"platform": "apple", "sourceUrl": "https://music.apple.com/us/song/unknown/000"}
        spotify = FakeSpotify()

        async def fake_resolver(url):
            return None

        result = await match_share(share, spotify, apple_resolver=fake_resolver)

        assert result["matchStatus"] == "unmatched"


class TestApplyManualOverride:
    @pytest.mark.asyncio
    async def test_valid_track_id_sets_manual_status(self):
        spotify = FakeSpotify(track_by_id={"abc123": _spotify_track("abc123", "Song", "Artist")})

        result = await apply_manual_override(spotify, "abc123")

        assert result["matchStatus"] == "manual"
        assert result["resolvedSpotifyId"] == "abc123"
        assert result["matchConfidence"] == 1.0

    @pytest.mark.asyncio
    async def test_invalid_track_id_raises(self):
        from lambdas.common.errors import ValidationError

        spotify = FakeSpotify(track_by_id={})

        with pytest.raises(ValidationError):
            await apply_manual_override(spotify, "nonexistent")
