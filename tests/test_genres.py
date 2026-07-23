"""
RED-before-GREEN: lambdas/common/genres.py -- the network-free genre helpers
behind the feed genre filter. Spotify tracks carry no genres; a track's
genre is derived from its PRIMARY artist (an artist-level attribute).

Degrade gracefully everywhere: no artist / no genres / unknown artist all
yield [] (never an exception), and ensure_genres guarantees every share
carries a string[] for the frontend filter.
"""

from lambdas.common.genres import (
    artist_genres,
    ensure_genres,
    genres_by_artist_map,
    genres_for_track,
    primary_artist_id,
)


def _track(track_id="t1", artist_id="a1"):
    return {"id": track_id, "name": "Song", "artists": [{"id": artist_id, "name": "Artist"}]}


class TestPrimaryArtistId:
    def test_returns_first_artist_id(self):
        track = {"artists": [{"id": "a1"}, {"id": "a2"}]}
        assert primary_artist_id(track) == "a1"

    def test_none_when_no_artists(self):
        assert primary_artist_id({"artists": []}) is None
        assert primary_artist_id({}) is None


class TestArtistGenres:
    def test_returns_genres_list(self):
        assert artist_genres({"genres": ["indie rock", "art pop"]}) == ["indie rock", "art pop"]

    def test_empty_when_missing(self):
        assert artist_genres({}) == []
        assert artist_genres({"genres": None}) == []


class TestGenresByArtistMap:
    def test_maps_id_to_genres(self):
        artists = [
            {"id": "a1", "genres": ["rock"]},
            {"id": "a2", "genres": []},
        ]
        assert genres_by_artist_map(artists) == {"a1": ["rock"], "a2": []}

    def test_skips_artists_without_id(self):
        assert genres_by_artist_map([{"genres": ["x"]}]) == {}


class TestGenresForTrack:
    def test_resolves_via_primary_artist(self):
        genres_by_artist = {"a1": ["indie rock"]}
        assert genres_for_track(_track(artist_id="a1"), genres_by_artist) == ["indie rock"]

    def test_empty_when_artist_unknown(self):
        assert genres_for_track(_track(artist_id="missing"), {"a1": ["rock"]}) == []

    def test_empty_when_no_primary_artist(self):
        assert genres_for_track({"artists": []}, {"a1": ["rock"]}) == []


class TestEnsureGenres:
    def test_adds_empty_list_when_absent(self):
        shares = [{"shareId": "s1"}]
        ensure_genres(shares)
        assert shares[0]["genres"] == []

    def test_preserves_existing_list(self):
        shares = [{"shareId": "s1", "genres": ["hip hop"]}]
        ensure_genres(shares)
        assert shares[0]["genres"] == ["hip hop"]

    def test_replaces_non_list_value(self):
        shares = [{"shareId": "s1", "genres": None}]
        ensure_genres(shares)
        assert shares[0]["genres"] == []

    def test_handles_empty_input(self):
        assert ensure_genres([]) == []
