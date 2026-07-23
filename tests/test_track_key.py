"""
RED-before-GREEN: track_key.derive_track_key -- a rating follows the SONG, so a
share maps to a normalized track identity: resolvedSpotifyId first, then a raw
Spotify URL, else a normalized sourceUrl. Same song across share instances ->
same key.
"""

from lambdas.common.track_key import derive_track_key, normalize_source_url


class TestNormalizeSourceUrl:
    def test_strips_scheme_www_query_and_trailing_slash(self):
        assert normalize_source_url(
            "https://www.soundcloud.com/artist/song-name/?utm=1"
        ) == "soundcloud.com/artist/song-name"

    def test_same_link_different_casing_and_format_collapse(self):
        a = normalize_source_url("HTTPS://SoundCloud.com/Artist/Song/")
        b = normalize_source_url("http://soundcloud.com/Artist/Song")
        assert a == b

    def test_empty_is_empty(self):
        assert normalize_source_url(None) == ""
        assert normalize_source_url("") == ""


class TestDeriveTrackKey:
    def test_resolved_spotify_id_wins(self):
        share = {
            "resolvedSpotifyId": "4uLU6hMCjMI75M1A2tKUQC",
            "sourceUrl": "https://soundcloud.com/x/y",
        }
        assert derive_track_key(share) == "spotify:4uLU6hMCjMI75M1A2tKUQC"

    def test_raw_spotify_url_without_resolution(self):
        share = {"sourceUrl": "https://open.spotify.com/track/4uLU6hMCjMI75M1A2tKUQC?si=abc"}
        assert derive_track_key(share) == "spotify:4uLU6hMCjMI75M1A2tKUQC"

    def test_spotify_uri_form(self):
        share = {"sourceUrl": "spotify:track:4uLU6hMCjMI75M1A2tKUQC"}
        assert derive_track_key(share) == "spotify:4uLU6hMCjMI75M1A2tKUQC"

    def test_spotify_key_is_stable_across_pending_to_matched(self):
        pending = {"sourceUrl": "https://open.spotify.com/track/ABC123"}
        matched = {"resolvedSpotifyId": "ABC123", "sourceUrl": "https://open.spotify.com/track/ABC123"}
        assert derive_track_key(pending) == derive_track_key(matched)

    def test_non_spotify_unmatched_uses_url_key(self):
        share = {"sourceUrl": "https://soundcloud.com/artist/song"}
        assert derive_track_key(share) == "url:soundcloud.com/artist/song"

    def test_two_shares_of_same_soundcloud_link_collapse(self):
        a = {"sourceUrl": "https://www.soundcloud.com/artist/song/"}
        b = {"sourceUrl": "http://soundcloud.com/artist/song?utm=2"}
        assert derive_track_key(a) == derive_track_key(b)

    def test_missing_source_url_does_not_raise(self):
        assert derive_track_key({}) == "url:"
