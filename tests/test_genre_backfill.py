"""
RED-before-GREEN: lambdas/genre_backfill/handler.py -- the two-hop, batched,
dedupe-by-artist backfill that populates `genres` on matched/manual shares
for the feed genre filter.

Spotify tracks carry no genres, so the backfill hydrates tracks (to read each
track's primary artist), then hydrates the deduped artists (to read genres),
then writes a `genres` list per share. Pure logic (needs/collect/build/run) is
unit-tested here with injected edges -- no real network, no real DynamoDB.
"""

from lambdas.genre_backfill.handler import (
    build_genre_updates,
    collect_primary_artist_ids,
    collect_track_ids_needing_genres,
    needs_genres,
    run_backfill,
)


def _track(track_id, artist_id):
    return {"id": track_id, "name": "Song", "artists": [{"id": artist_id, "name": "Artist"}]}


class TestNeedsGenres:
    def test_matched_without_genres_needs_it(self):
        assert needs_genres({"matchStatus": "matched", "resolvedSpotifyId": "a1"}) is True

    def test_manual_without_genres_needs_it(self):
        assert needs_genres({"matchStatus": "manual", "resolvedSpotifyId": "a1"}) is True

    def test_share_already_enriched_empty_is_skipped(self):
        # `[]` means "enriched, artist had no genres" -- terminal, not retried.
        assert needs_genres({"matchStatus": "matched", "resolvedSpotifyId": "a1", "genres": []}) is False

    def test_share_with_genres_is_skipped(self):
        assert needs_genres({"matchStatus": "matched", "resolvedSpotifyId": "a1", "genres": ["rock"]}) is False

    def test_share_without_resolved_id_is_skipped(self):
        assert needs_genres({"matchStatus": "matched"}) is False

    def test_unmatched_share_is_skipped(self):
        assert needs_genres({"matchStatus": "unmatched", "resolvedSpotifyId": None}) is False


class TestCollectTrackIds:
    def test_dedupes_and_preserves_order(self):
        shares = [
            {"matchStatus": "matched", "resolvedSpotifyId": "a"},
            {"matchStatus": "matched", "resolvedSpotifyId": "b"},
            {"matchStatus": "matched", "resolvedSpotifyId": "a"},
            {"matchStatus": "matched", "resolvedSpotifyId": "c", "genres": ["has"]},
        ]
        assert collect_track_ids_needing_genres(shares) == ["a", "b"]


class TestCollectPrimaryArtistIds:
    def test_dedupes_across_tracks(self):
        tracks_by_id = {
            "t1": _track("t1", "art1"),
            "t2": _track("t2", "art1"),  # same artist -> deduped
            "t3": _track("t3", "art2"),
        }
        assert collect_primary_artist_ids(tracks_by_id) == ["art1", "art2"]


class TestBuildGenreUpdates:
    def test_maps_each_share_to_its_primary_artist_genres(self):
        shares = [
            {"shareId": "s1", "matchStatus": "matched", "resolvedSpotifyId": "t1"},
            {"shareId": "s2", "matchStatus": "matched", "resolvedSpotifyId": "t2"},
        ]
        tracks = {"t1": _track("t1", "art1"), "t2": _track("t2", "art2")}
        genres_by_artist = {"art1": ["indie rock"], "art2": []}

        updates = build_genre_updates(shares, tracks, genres_by_artist)

        assert updates == [
            ("s1", {"genres": ["indie rock"]}),
            ("s2", {"genres": []}),
        ]

    def test_share_with_unresolvable_track_is_omitted(self):
        # Track removed from the catalog -> leave for a later run, don't pin [].
        shares = [{"shareId": "s1", "matchStatus": "matched", "resolvedSpotifyId": "gone"}]
        assert build_genre_updates(shares, {}, {}) == []


class TestRunBackfill:
    def test_two_hop_hydrate_and_persist(self):
        shares = [
            {"shareId": "s1", "matchStatus": "matched", "resolvedSpotifyId": "t1"},
            {"shareId": "s2", "matchStatus": "matched", "resolvedSpotifyId": "t2", "genres": ["done"]},
            {"shareId": "s3", "matchStatus": "unmatched", "resolvedSpotifyId": None},
        ]
        persisted = []
        artist_fetch_calls = []

        def track_fetch(ids):
            assert ids == ["t1"]  # only the un-enriched, resolvable share
            return {"t1": _track("t1", "art1")}

        def artist_fetch(ids):
            artist_fetch_calls.append(ids)
            return {"art1": ["hyperpop"]}

        summary = run_backfill(
            shares,
            track_fetch=track_fetch,
            artist_fetch=artist_fetch,
            persist=lambda sid, fields: persisted.append((sid, fields)),
        )

        assert persisted == [("s1", {"genres": ["hyperpop"]})]
        assert artist_fetch_calls == [["art1"]]
        assert summary["candidates"] == 1
        assert summary["updated"] == 1
        assert summary["withGenres"] == 1
        assert summary["skipped"] == 2

    def test_artist_without_genres_persists_empty_list(self):
        shares = [{"shareId": "s1", "matchStatus": "matched", "resolvedSpotifyId": "t1"}]
        persisted = []

        run_backfill(
            shares,
            track_fetch=lambda ids: {"t1": _track("t1", "art1")},
            artist_fetch=lambda ids: {"art1": []},
            persist=lambda sid, fields: persisted.append((sid, fields)),
        )

        assert persisted == [("s1", {"genres": []})]

    def test_nothing_to_do_skips_fetches(self):
        shares = [{"shareId": "s1", "matchStatus": "matched", "resolvedSpotifyId": "t1", "genres": ["x"]}]
        called = {"track": False, "artist": False}

        summary = run_backfill(
            shares,
            track_fetch=lambda ids: called.__setitem__("track", True) or {},
            artist_fetch=lambda ids: called.__setitem__("artist", True) or {},
            persist=lambda sid, fields: None,
        )

        assert called == {"track": False, "artist": False}
        assert summary["updated"] == 0
