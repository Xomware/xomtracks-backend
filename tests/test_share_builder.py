"""
RED-before-GREEN: extractor/share_builder.py -- turns a chat_reader row
into zero or more share-ingest dicts (POST /shares/ingest body shape).
"""

from extractor.chat_reader import apple_epoch_to_unix
from tests.chat_db_fixture import (
    apple_ns_from_unix,
    bplist_attributed_body,
    typedstream_attributed_body,
)


def _row(
    rowid=1,
    guid="guid-1",
    text=None,
    attributed_body=None,
    is_from_me=0,
    unix_date=1753000000,
    handle_identifier=None,
    chat_id="chat-1",
):
    return {
        "rowid": rowid,
        "guid": guid,
        "text": text,
        "attributed_body": attributed_body,
        "is_from_me": is_from_me,
        "date": apple_ns_from_unix(unix_date),
        "handle_identifier": handle_identifier,
        "chat_id": chat_id,
    }


class TestBuildSharesFromMessage:
    def test_no_urls_returns_empty_list(self):
        from extractor.share_builder import build_shares_from_message

        row = _row(text="just saying hi")
        assert build_shares_from_message(row) == []

    def test_single_text_url_incoming(self):
        from extractor.share_builder import build_shares_from_message

        row = _row(
            text="check this https://open.spotify.com/track/abc123",
            is_from_me=0,
            handle_identifier="+13364042196",
            unix_date=1753000000,
        )
        shares = build_shares_from_message(row)

        assert len(shares) == 1
        share = shares[0]
        assert share["messageGuid"] == "guid-1"
        assert share["direction"] == "in"
        assert share["sharerHandle"] == "+13364042196"
        assert share["chatId"] == "chat-1"
        assert share["platform"] == "spotify"
        assert share["sourceUrl"] == "https://open.spotify.com/track/abc123"
        assert share["messageDate"] == 1753000000

    def test_outgoing_message_has_no_sharer_handle(self):
        from extractor.share_builder import build_shares_from_message

        row = _row(
            text="https://soundcloud.com/artist/track",
            is_from_me=1,
            handle_identifier=None,
        )
        shares = build_shares_from_message(row)

        assert shares[0]["direction"] == "out"
        assert shares[0]["sharerHandle"] is None

    def test_attributed_body_only_link_is_found(self):
        from extractor.share_builder import build_shares_from_message

        blob = bplist_attributed_body("https://music.apple.com/us/album/song/123?i=456")
        row = _row(text=None, attributed_body=blob, is_from_me=0, handle_identifier="+1")
        shares = build_shares_from_message(row)

        assert len(shares) == 1
        assert shares[0]["platform"] == "apple"
        assert shares[0]["sourceUrl"] == "https://music.apple.com/us/album/song/123?i=456"

    def test_text_and_attributed_body_urls_are_combined_and_deduped(self):
        from extractor.share_builder import build_shares_from_message

        url = "https://open.spotify.com/track/abc123"
        blob = bplist_attributed_body(url)
        row = _row(text=f"check this {url}", attributed_body=blob, is_from_me=0, handle_identifier="+1")
        shares = build_shares_from_message(row)

        # Same URL present in both text and attributedBody -> one share, not two.
        assert len(shares) == 1

    def test_text_and_typedstream_body_same_link_dedups_to_one_share(self):
        # Regression for the production "everything is duplicated" bug: a
        # legacy link-preview message carries the URL cleanly in `text` and
        # again in a typedstream `attributedBody` whose recovered URL had a
        # `WHttpURL/` tail glued on. Once the extractor strips that tail, the
        # two copies are byte-identical and collapse to a SINGLE share.
        from extractor.share_builder import build_shares_from_message

        url = "https://open.spotify.com/track/18FN1Kz7KMF0ujN6ID4ans?si=51O0VWtpSOakkx"
        blob = typedstream_attributed_body(url, glue_url_tail=True)
        row = _row(text=url, attributed_body=blob, is_from_me=0, handle_identifier="+1")
        shares = build_shares_from_message(row)

        assert len(shares) == 1
        assert shares[0]["sourceUrl"] == url

    def test_multiple_distinct_urls_produce_multiple_shares(self):
        from extractor.share_builder import build_shares_from_message

        row = _row(
            text="two: https://open.spotify.com/track/aaa and https://soundcloud.com/x/bbb",
            is_from_me=0,
            handle_identifier="+1",
        )
        shares = build_shares_from_message(row)

        assert len(shares) == 2
        platforms = {s["platform"] for s in shares}
        assert platforms == {"spotify", "soundcloud"}
        # Every share carries the SAME messageGuid -- one text message, two links.
        assert all(s["messageGuid"] == "guid-1" for s in shares)

    def test_no_chat_id_is_allowed(self):
        from extractor.share_builder import build_shares_from_message

        row = _row(text="https://open.spotify.com/track/aaa", chat_id=None)
        shares = build_shares_from_message(row)
        assert shares[0]["chatId"] is None


class TestSharerNameResolution:
    def test_incoming_share_gets_resolved_sharer_name(self):
        from extractor.share_builder import build_shares_from_message

        row = _row(
            text="https://open.spotify.com/track/abc123",
            is_from_me=0,
            handle_identifier="+13364042196",
        )
        shares = build_shares_from_message(row, resolve_name=lambda h: "Jordan Reyes")
        assert shares[0]["sharerHandle"] == "+13364042196"
        assert shares[0]["sharerName"] == "Jordan Reyes"

    def test_unresolved_handle_leaves_name_none_but_keeps_handle(self):
        from extractor.share_builder import build_shares_from_message

        row = _row(text="https://open.spotify.com/track/abc123", is_from_me=0, handle_identifier="+15550001111")
        shares = build_shares_from_message(row, resolve_name=lambda h: None)
        assert shares[0]["sharerHandle"] == "+15550001111"
        assert shares[0]["sharerName"] is None

    def test_outgoing_share_has_no_name_lookup(self):
        from extractor.share_builder import build_shares_from_message

        calls = []

        def resolver(h):
            calls.append(h)
            return "should not be used"

        row = _row(text="https://soundcloud.com/x/y", is_from_me=1, handle_identifier=None)
        shares = build_shares_from_message(row, resolve_name=resolver)
        assert shares[0]["sharerName"] is None
        assert calls == []

    def test_no_resolver_defaults_name_to_none(self):
        from extractor.share_builder import build_shares_from_message

        row = _row(text="https://open.spotify.com/track/abc123", is_from_me=0, handle_identifier="+1")
        shares = build_shares_from_message(row)
        assert shares[0]["sharerName"] is None


class TestBuildSharesFromMessages:
    def test_flattens_across_multiple_rows(self):
        from extractor.share_builder import build_shares_from_messages

        rows = [
            _row(rowid=1, guid="g1", text="https://open.spotify.com/track/aaa"),
            _row(rowid=2, guid="g2", text="no link here"),
            _row(rowid=3, guid="g3", text="https://soundcloud.com/x/bbb"),
        ]
        shares = build_shares_from_messages(rows)
        assert len(shares) == 2
        assert {s["messageGuid"] for s in shares} == {"g1", "g3"}
