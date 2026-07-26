"""
RED-before-GREEN: extractor/url_extractor.py.

Real-world finding this module exists to fix: text-only matching found 0
music links across Dom's real chat.db; text+attributedBody found 13. Link-
preview URLs live in attributedBody, not the plain `text` column, so
extraction must cover both -- and attributedBody is a binary blob
(NSKeyedArchiver bplist on modern macOS, legacy "typedstream" on older
messages), not plain text.
"""

from extractor.url_extractor import (
    detect_platform,
    extract_urls_from_text,
    extract_urls_from_attributed_body,
)
from tests.chat_db_fixture import bplist_attributed_body, typedstream_attributed_body


class TestDetectPlatform:
    def test_spotify(self):
        assert detect_platform("https://open.spotify.com/track/abc123") == "spotify"

    def test_soundcloud(self):
        assert detect_platform("https://soundcloud.com/artist/track") == "soundcloud"

    def test_soundcloud_with_www(self):
        assert detect_platform("https://www.soundcloud.com/artist/track") == "soundcloud"

    def test_soundcloud_short_on_host(self):
        # The exact link that failed live (shares_found: 0) before this fix --
        # a share-sheet short link with an opaque hash, no user/track slug.
        assert detect_platform("https://on.soundcloud.com/LA3v2rA4vjcaIcTjyU") == "soundcloud"

    def test_soundcloud_mobile_host(self):
        assert detect_platform("https://m.soundcloud.com/artist/track") == "soundcloud"

    def test_soundcloud_firebase_dynamic_link(self):
        assert detect_platform("https://soundcloud.app.goo.gl/abc123XYZ") == "soundcloud"

    def test_soundcloud_snd_sc_shortener(self):
        assert detect_platform("https://snd.sc/aBcD1234") == "soundcloud"

    def test_soundcloud_lookalike_host_is_not_matched(self):
        # Guard the subdomain regex against a phishing-style lookalike host --
        # `notsoundcloud.com` and `soundcloud.com.evil.io` must NOT match.
        assert detect_platform("https://notsoundcloud.com/artist/track") is None
        assert detect_platform("https://soundcloud.com.evil.io/x") is None

    def test_apple_music(self):
        assert detect_platform("https://music.apple.com/us/album/x/123") == "apple"

    def test_unsupported_returns_none(self):
        assert detect_platform("https://youtube.com/watch?v=abc") is None


class TestExtractUrlsFromText:
    def test_finds_spotify_link(self):
        text = "yo check this out https://open.spotify.com/track/4uLU6hMCjMI75M1A2tKUQC?si=abc it's fire"
        urls = extract_urls_from_text(text)
        assert urls == ["https://open.spotify.com/track/4uLU6hMCjMI75M1A2tKUQC?si=abc"]

    def test_strips_trailing_punctuation(self):
        text = "listen to this (https://soundcloud.com/artist/track)."
        urls = extract_urls_from_text(text)
        assert urls == ["https://soundcloud.com/artist/track"]

    def test_finds_multiple_urls(self):
        text = "two songs: https://open.spotify.com/track/aaa and https://soundcloud.com/x/bbb"
        urls = extract_urls_from_text(text)
        assert urls == [
            "https://open.spotify.com/track/aaa",
            "https://soundcloud.com/x/bbb",
        ]

    def test_keeps_soundcloud_short_link(self):
        # Detection gates which URLs survive _clean_and_filter -- a short link
        # must be KEPT (not dropped as unsupported) so the share gets ingested.
        text = "listen https://on.soundcloud.com/LA3v2rA4vjcaIcTjyU"
        urls = extract_urls_from_text(text)
        assert urls == ["https://on.soundcloud.com/LA3v2rA4vjcaIcTjyU"]

    def test_ignores_unsupported_platform_links(self):
        text = "watch this https://youtube.com/watch?v=abc123"
        urls = extract_urls_from_text(text)
        assert urls == []

    def test_none_text_returns_empty_list(self):
        assert extract_urls_from_text(None) == []

    def test_plain_text_no_urls(self):
        assert extract_urls_from_text("just saying hi") == []


class TestExtractUrlsFromAttributedBody:
    def test_finds_url_in_bplist_shaped_blob(self):
        blob = bplist_attributed_body("https://open.spotify.com/track/4uLU6hMCjMI75M1A2tKUQC")
        urls = extract_urls_from_attributed_body(blob)
        assert "https://open.spotify.com/track/4uLU6hMCjMI75M1A2tKUQC" in urls

    def test_finds_url_in_typedstream_fallback_blob(self):
        blob = typedstream_attributed_body("https://soundcloud.com/artist/track-name")
        urls = extract_urls_from_attributed_body(blob)
        assert "https://soundcloud.com/artist/track-name" in urls

    def test_typedstream_url_tail_marker_is_stripped(self):
        # Real link-preview typedstream blobs glue the archived class token
        # `WHttpURL/` directly onto the URL (no null separator) -- all
        # URL-legal chars, so the greedy regex swallows them. The extractor
        # must recover the CLEAN url, byte-identical to the `text` column's
        # copy, or the same link doubles into two shares.
        clean = "https://open.spotify.com/track/0kir0EgDtekhaB37RsU?si=abc123"
        blob = typedstream_attributed_body(clean, glue_url_tail=True)
        urls = extract_urls_from_attributed_body(blob)
        assert urls == [clean]
        assert not any("WHttpURL" in u for u in urls)

    def test_none_blob_returns_empty_list(self):
        assert extract_urls_from_attributed_body(None) == []

    def test_empty_blob_returns_empty_list(self):
        assert extract_urls_from_attributed_body(b"") == []

    def test_garbage_blob_does_not_raise(self):
        assert extract_urls_from_attributed_body(b"\x00\x01\x02not a plist or typedstream") == []
