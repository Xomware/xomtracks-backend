"""
RED-before-GREEN: lambdas/album_art_backfill/handler.py -- one-shot backfill
that hydrates `albumArtUrl` / `albumName` onto the historical `matched`
shares that were resolved before those fields existed on the Share model.

Strategy mirrors matching_sweep: collect the distinct `resolvedSpotifyId`s
of matched shares that are missing album art, batch-hydrate them through
Spotify's `GET /v1/tracks?ids=` (50/call), then write the two album fields
back per share. Pure logic (partition/collect/build) is unit-tested here
with injected edges -- no real network, no real DynamoDB.
"""

from lambdas.album_art_backfill.handler import (
    build_album_updates,
    collect_ids_needing_art,
    needs_album_art,
    run_backfill,
)


def _track(track_id, album_name="Album", art="https://i.scdn.co/image/medium"):
    images = (
        [
            {"url": "https://i.scdn.co/image/large", "height": 640, "width": 640},
            {"url": art, "height": 300, "width": 300},
            {"url": "https://i.scdn.co/image/small", "height": 64, "width": 64},
        ]
        if art
        else []
    )
    return {
        "id": track_id,
        "name": "Song",
        "uri": f"spotify:track:{track_id}",
        "artists": [{"name": "Artist"}],
        "album": {"name": album_name, "images": images},
    }


class TestNeedsAlbumArt:
    def test_matched_share_without_art_needs_it(self):
        assert needs_album_art({"matchStatus": "matched", "resolvedSpotifyId": "a1"}) is True

    def test_matched_share_with_art_is_skipped(self):
        share = {"matchStatus": "matched", "resolvedSpotifyId": "a1", "albumArtUrl": "x"}
        assert needs_album_art(share) is False

    def test_share_without_resolved_id_is_skipped(self):
        assert needs_album_art({"matchStatus": "matched"}) is False

    def test_unmatched_share_is_skipped(self):
        assert needs_album_art({"matchStatus": "unmatched", "resolvedSpotifyId": None}) is False


class TestCollectIds:
    def test_dedupes_and_preserves_order(self):
        shares = [
            {"matchStatus": "matched", "resolvedSpotifyId": "a"},
            {"matchStatus": "matched", "resolvedSpotifyId": "b"},
            {"matchStatus": "matched", "resolvedSpotifyId": "a"},
            {"matchStatus": "matched", "resolvedSpotifyId": "c", "albumArtUrl": "has"},
        ]
        assert collect_ids_needing_art(shares) == ["a", "b"]


class TestBuildAlbumUpdates:
    def test_maps_each_share_to_its_track_album_fields(self):
        shares = [
            {"shareId": "s1", "matchStatus": "matched", "resolvedSpotifyId": "a"},
            {"shareId": "s2", "matchStatus": "matched", "resolvedSpotifyId": "b"},
        ]
        tracks = {"a": _track("a", "Album A"), "b": _track("b", "Album B")}

        updates = build_album_updates(shares, tracks)

        assert updates == [
            ("s1", {"albumArtUrl": "https://i.scdn.co/image/medium", "albumName": "Album A"}),
            ("s2", {"albumArtUrl": "https://i.scdn.co/image/medium", "albumName": "Album B"}),
        ]

    def test_share_with_unresolvable_id_is_omitted(self):
        shares = [{"shareId": "s1", "matchStatus": "matched", "resolvedSpotifyId": "gone"}]
        assert build_album_updates(shares, {}) == []

    def test_track_without_images_still_backfills_album_name_only(self):
        shares = [{"shareId": "s1", "matchStatus": "matched", "resolvedSpotifyId": "a"}]
        tracks = {"a": _track("a", "Nameonly", art=None)}
        assert build_album_updates(shares, tracks) == [
            ("s1", {"albumArtUrl": None, "albumName": "Nameonly"}),
        ]


class TestRunBackfill:
    def test_hydrates_and_persists_only_shares_needing_art(self):
        shares = [
            {"shareId": "s1", "matchStatus": "matched", "resolvedSpotifyId": "a"},
            {"shareId": "s2", "matchStatus": "matched", "resolvedSpotifyId": "b", "albumArtUrl": "x"},
            {"shareId": "s3", "matchStatus": "unmatched", "resolvedSpotifyId": None},
        ]
        persisted = []

        summary = run_backfill(
            shares,
            batch_fetch=lambda ids: {i: _track(i) for i in ids},
            persist=lambda sid, fields: persisted.append((sid, fields)),
        )

        assert [p[0] for p in persisted] == ["s1"]
        assert summary["candidates"] == 1
        assert summary["updated"] == 1
        assert summary["skipped"] == 2
