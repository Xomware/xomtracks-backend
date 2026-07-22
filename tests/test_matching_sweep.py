"""
RED-before-GREEN: lambdas/matching_sweep/handler.py -- the matching sweep
runner that resolves stored `pending` shares into Spotify tracks.

Core logic is pure/injectable so it can be tested with zero network:
- partition_shares: split pending shares into the batched Spotify-URL path
  (direct id lookup) vs the fuzzy-search path (SoundCloud/Apple).
- collect_track_ids: dedupe parseable Spotify track ids for the
  GET /tracks?ids= batch endpoint (skips unparseable urls).
- spotify_batch_results: given a prefetched {id: track} map, produce the
  per-share match-result fields (matched when the id resolved, unmatched
  when the track is missing or the url was unparseable).
- run_sweep: orchestrate batch + search + persist over injected edges,
  returning a summary (counts, examples, errors) with no live AWS/Spotify.
"""

import pytest

from lambdas.matching_sweep.handler import (
    _batch_fetch_edge,
    collect_track_ids,
    partition_shares,
    run_sweep,
    spotify_batch_results,
    summarize,
)


def _share(share_id, platform, url, direction="in"):
    return {
        "shareId": share_id,
        "platform": platform,
        "sourceUrl": url,
        "direction": direction,
    }


def _track(track_id="abc123", name="Song Name", artist="Artist Name"):
    return {
        "id": track_id,
        "name": name,
        "artists": [{"name": artist}],
        "uri": f"spotify:track:{track_id}",
    }


class TestPartitionShares:
    def test_splits_spotify_from_search_platforms(self):
        shares = [
            _share("s1", "spotify", "https://open.spotify.com/track/a"),
            _share("s2", "soundcloud", "https://soundcloud.com/x/y"),
            _share("s3", "apple", "https://music.apple.com/us/song/z/1?i=2"),
            _share("s4", "spotify", "https://open.spotify.com/track/b"),
        ]
        spotify_shares, search_shares = partition_shares(shares)
        assert [s["shareId"] for s in spotify_shares] == ["s1", "s4"]
        assert [s["shareId"] for s in search_shares] == ["s2", "s3"]

    def test_unknown_platform_goes_to_search_bucket(self):
        # Unknown platforms have no direct-id shortcut -- route them through
        # the resolver path, which degrades them to unmatched safely.
        shares = [_share("s1", "youtube", "https://youtu.be/x")]
        spotify_shares, search_shares = partition_shares(shares)
        assert spotify_shares == []
        assert [s["shareId"] for s in search_shares] == ["s1"]


_ID_A = "0kir0EgDtekhaB37RsUQaa"  # 22-char base62
_ID_B = "72qknjLxZXE6iE6h27sWQb"  # 22-char base62


class TestCollectTrackIds:
    def test_dedupes_and_skips_unparseable(self):
        shares = [
            _share("s1", "spotify", f"https://open.spotify.com/track/{_ID_A}?si=1"),
            _share("s2", "spotify", f"spotify:track:{_ID_B}"),
            _share("s3", "spotify", f"https://open.spotify.com/track/{_ID_A}?si=2"),  # dupe id
            _share("s4", "spotify", "https://open.spotify.com/playlist/nope"),  # no track id
        ]
        ids = collect_track_ids(shares)
        assert ids == [_ID_A, _ID_B]

    def test_skips_malformed_ids(self):
        # Real data contains non-track URLs and `...WHttpURL` truncation
        # artifacts -- neither yields a valid 22-char base62 id, and letting
        # one into a batch 400s the whole chunk.
        shares = [
            _share("s1", "spotify", "https://open.spotify.com/track/72qknjLxZXE6iE6h27sWHttpURL/"),
            _share("s2", "spotify", "https://open.spotify.com/track/6n6P509BRrmz0Ku83dq"),  # 19 chars
            _share("s3", "spotify", "https://open.spotify.com/artist/6xvpfMjWTougrRRtK7iikz"),
            _share("s4", "spotify", f"https://open.spotify.com/track/{_ID_A}"),
        ]
        assert collect_track_ids(shares) == [_ID_A]


class TestSpotifyBatchResults:
    def test_matched_when_id_present_in_map(self):
        shares = [_share("s1", "spotify", f"https://open.spotify.com/track/{_ID_A}")]
        tracks_by_id = {_ID_A: _track(_ID_A, "Blinding Lights", "The Weeknd")}
        results = spotify_batch_results(shares, tracks_by_id)
        share, fields = results[0]
        assert share["shareId"] == "s1"
        assert fields["matchStatus"] == "matched"
        assert fields["matchConfidence"] == 1.0
        assert fields["resolvedSpotifyId"] == _ID_A
        assert fields["resolvedSpotifyUri"] == f"spotify:track:{_ID_A}"
        assert fields["trackTitle"] == "Blinding Lights"
        assert fields["trackArtist"] == "The Weeknd"

    def test_unmatched_when_track_missing_from_map(self):
        shares = [_share("s1", "spotify", f"https://open.spotify.com/track/{_ID_A}")]
        results = spotify_batch_results(shares, {})
        _, fields = results[0]
        assert fields["matchStatus"] == "unmatched"
        assert fields["resolvedSpotifyId"] is None

    def test_unmatched_when_id_malformed(self):
        # `...WHttpURL` truncation artifact -> not a valid 22-char id.
        shares = [_share("s1", "spotify", "https://open.spotify.com/track/6n6P509BRrmz0Ku83dqWHttpURL/")]
        results = spotify_batch_results(shares, {})
        _, fields = results[0]
        assert fields["matchStatus"] == "unmatched"

    def test_unmatched_when_url_unparseable(self):
        shares = [_share("s1", "spotify", "https://open.spotify.com/album/x")]
        results = spotify_batch_results(shares, {"x": _track("x")})
        _, fields = results[0]
        assert fields["matchStatus"] == "unmatched"


class TestSummarize:
    def test_counts_by_status_and_collects_examples(self):
        results = [
            (_share("s1", "spotify", "u", direction="in"),
             {"matchStatus": "matched", "trackTitle": "A", "trackArtist": "X"}),
            (_share("s2", "soundcloud", "u", direction="out"),
             {"matchStatus": "matched", "trackTitle": "B", "trackArtist": "Y"}),
            (_share("s3", "apple", "u"),
             {"matchStatus": "unmatched", "trackTitle": None, "trackArtist": None}),
        ]
        errors = [(_share("s4", "soundcloud", "u"), "boom")]
        summary = summarize(results, errors, examples_limit=8)
        assert summary["matched"] == 2
        assert summary["unmatched"] == 1
        assert summary["errors"] == 1
        assert summary["processed"] == 3
        assert len(summary["examples"]) == 2
        assert summary["examples"][0] == {
            "title": "A", "artist": "X", "platform": "spotify", "direction": "in",
        }

    def test_examples_capped_at_limit(self):
        results = [
            (_share(f"s{i}", "spotify", "u"),
             {"matchStatus": "matched", "trackTitle": f"T{i}", "trackArtist": "X"})
            for i in range(10)
        ]
        summary = summarize(results, [], examples_limit=3)
        assert len(summary["examples"]) == 3


class TestBatchFetchEdge:
    """The live batch edge must survive a chunk that Spotify 400s because of
    one bad id -- it bisects to isolate the offender and keeps the good
    tracks, and never sleeps for real in tests."""

    def test_bisects_around_bad_id(self):
        good = ["g1", "g2", "g3"]
        bad = "BADID"

        class FakeSpotify:
            def get_tracks_by_ids(self, ids):
                if bad in ids:
                    raise Exception("Batch get tracks failed (400): Invalid base62 id")
                return [{"id": i, "name": i, "artists": [{"name": "A"}], "uri": f"spotify:track:{i}"} for i in ids]

        edge = _batch_fetch_edge(FakeSpotify(), sleep_fn=lambda _s: None)
        tracks_by_id = edge(good[:1] + [bad] + good[1:])

        assert set(tracks_by_id) == set(good)  # bad id dropped, good kept

    def test_retries_on_429_then_succeeds(self):
        calls = {"n": 0}

        class FakeSpotify:
            def get_tracks_by_ids(self, ids):
                calls["n"] += 1
                if calls["n"] == 1:
                    raise Exception("Batch get tracks failed (429): rate limited")
                return [{"id": i} for i in ids]

        edge = _batch_fetch_edge(FakeSpotify(), sleep_fn=lambda _s: None)
        tracks_by_id = edge(["a"])

        assert calls["n"] == 2
        assert "a" in tracks_by_id


class TestRunSweep:
    def test_orchestrates_batch_search_and_persist(self):
        shares = [
            _share("s1", "spotify", f"https://open.spotify.com/track/{_ID_A}"),
            _share("s2", "spotify", f"https://open.spotify.com/track/{_ID_B}"),
            _share("s3", "soundcloud", "https://soundcloud.com/x/y"),
        ]

        def fake_batch_fetch(ids):
            assert ids == [_ID_A, _ID_B]
            return {_ID_A: _track(_ID_A, "Matched Song", "Some Artist")}

        def fake_search_batch(search_shares):
            out = []
            for s in search_shares:
                out.append((s, {
                    "matchStatus": "matched",
                    "matchConfidence": 0.91,
                    "resolvedSpotifyId": "sc1",
                    "resolvedSpotifyUri": "spotify:track:sc1",
                    "trackTitle": "SC Song",
                    "trackArtist": "SC Artist",
                }))
            return out

        persisted = {}

        def fake_persist(share_id, fields):
            persisted[share_id] = fields

        summary = run_sweep(
            shares,
            batch_fetch=fake_batch_fetch,
            search_batch=fake_search_batch,
            persist=fake_persist,
        )

        assert summary["matched"] == 2  # s1 (batch) + s3 (search)
        assert summary["unmatched"] == 1  # s2
        assert summary["errors"] == 0
        assert persisted["s1"]["resolvedSpotifyId"] == _ID_A
        assert persisted["s2"]["matchStatus"] == "unmatched"
        assert persisted["s3"]["resolvedSpotifyId"] == "sc1"

    def test_search_errors_are_counted_not_persisted(self):
        shares = [_share("s1", "soundcloud", "https://soundcloud.com/x/y")]

        def fake_batch_fetch(ids):
            return {}

        def fake_search_batch(search_shares):
            return [(search_shares[0], RuntimeError("spotify search 429"))]

        persisted = {}

        summary = run_sweep(
            shares,
            batch_fetch=fake_batch_fetch,
            search_batch=fake_search_batch,
            persist=lambda sid, f: persisted.__setitem__(sid, f),
        )

        assert summary["errors"] == 1
        assert summary["matched"] == 0
        assert "s1" not in persisted

    def test_no_spotify_shares_skips_batch_fetch(self):
        shares = [_share("s1", "apple", "https://music.apple.com/us/song/z/1?i=2")]
        called = {"batch": False}

        def fake_batch_fetch(ids):
            called["batch"] = True
            return {}

        def fake_search_batch(search_shares):
            return [(search_shares[0], {"matchStatus": "unmatched", "trackTitle": None, "trackArtist": None})]

        run_sweep(
            shares,
            batch_fetch=fake_batch_fetch,
            search_batch=fake_search_batch,
            persist=lambda sid, f: None,
        )
        assert called["batch"] is False
