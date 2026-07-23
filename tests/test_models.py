"""
RED-before-GREEN: Pydantic 2.8 models for the Share data model and the
request/response boundary. Validate at the boundary per backend.md.
"""

import pytest
from pydantic import ValidationError as PydanticValidationError

from lambdas.common.models import (
    Share,
    ShareIngestRequest,
    MatchOverrideRequest,
    LinkPhoneRequest,
)


class TestShareIngestRequest:
    def test_valid_payload(self):
        req = ShareIngestRequest(
            messageGuid="guid-1",
            direction="in",
            sharerHandle="+13364042196",
            chatId="chat-1",
            platform="spotify",
            sourceUrl="https://open.spotify.com/track/4uLU6hMCjMI75M1A2tKUQC",
            messageDate=1753000000,
        )
        assert req.direction == "in"
        assert req.platform == "spotify"

    def test_direction_out_allows_null_sharer_handle(self):
        # is_from_me=1 -> direction=out -> Dom is the sender, no handle.
        req = ShareIngestRequest(
            messageGuid="guid-2",
            direction="out",
            sharerHandle=None,
            chatId="chat-1",
            platform="soundcloud",
            sourceUrl="https://soundcloud.com/artist/track",
            messageDate=1753000000,
        )
        assert req.sharerHandle is None

    def test_invalid_direction_rejected(self):
        with pytest.raises(PydanticValidationError):
            ShareIngestRequest(
                messageGuid="guid-3",
                direction="sideways",
                sharerHandle="+1",
                chatId="chat-1",
                platform="spotify",
                sourceUrl="https://open.spotify.com/track/abc",
                messageDate=1753000000,
            )

    def test_invalid_platform_rejected(self):
        with pytest.raises(PydanticValidationError):
            ShareIngestRequest(
                messageGuid="guid-4",
                direction="in",
                sharerHandle="+1",
                chatId="chat-1",
                platform="tidal",
                sourceUrl="https://tidal.com/track/abc",
                messageDate=1753000000,
            )

    def test_blank_source_url_rejected(self):
        with pytest.raises(PydanticValidationError):
            ShareIngestRequest(
                messageGuid="guid-5",
                direction="in",
                sharerHandle="+1",
                chatId="chat-1",
                platform="spotify",
                sourceUrl="   ",
                messageDate=1753000000,
            )

    def test_blank_message_guid_rejected(self):
        with pytest.raises(PydanticValidationError):
            ShareIngestRequest(
                messageGuid="   ",
                direction="in",
                sharerHandle="+1",
                chatId="chat-1",
                platform="spotify",
                sourceUrl="https://open.spotify.com/track/abc",
                messageDate=1753000000,
            )


class TestShare:
    def test_valid_share_defaults(self, sample_share):
        share = Share(**sample_share)
        assert share.matchStatus == "pending"
        assert share.matchConfidence is None

    def test_match_status_must_be_known_value(self, sample_share):
        sample_share["matchStatus"] = "bogus"
        with pytest.raises(PydanticValidationError):
            Share(**sample_share)

    def test_match_confidence_bounded_0_1(self, sample_share):
        sample_share["matchConfidence"] = 1.5
        with pytest.raises(PydanticValidationError):
            Share(**sample_share)

    def test_match_confidence_none_allowed_when_pending(self, sample_share):
        share = Share(**sample_share)
        assert share.matchConfidence is None

    def test_genres_defaults_to_empty_list(self, sample_share):
        share = Share(**sample_share)
        assert share.genres == []

    def test_genres_accepts_string_list(self, sample_share):
        sample_share["genres"] = ["indie rock", "art pop"]
        share = Share(**sample_share)
        assert share.genres == ["indie rock", "art pop"]


class TestMatchOverrideRequest:
    def test_valid_payload(self):
        req = MatchOverrideRequest(spotifyTrackId="4uLU6hMCjMI75M1A2tKUQC")
        assert req.spotifyTrackId == "4uLU6hMCjMI75M1A2tKUQC"

    def test_extracts_id_from_full_url(self):
        req = MatchOverrideRequest(
            spotifyTrackId="https://open.spotify.com/track/4uLU6hMCjMI75M1A2tKUQC?si=abc"
        )
        assert req.spotifyTrackId == "4uLU6hMCjMI75M1A2tKUQC"

    def test_blank_rejected(self):
        with pytest.raises(PydanticValidationError):
            MatchOverrideRequest(spotifyTrackId="   ")


class TestLinkPhoneRequest:
    def test_valid_payload(self):
        req = LinkPhoneRequest(phoneNumber="+1 (336) 404-2196")
        assert req.phoneNumber == "+1 (336) 404-2196"

    def test_blank_rejected(self):
        with pytest.raises(PydanticValidationError):
            LinkPhoneRequest(phoneNumber="   ")

    def test_no_digits_rejected(self):
        with pytest.raises(PydanticValidationError):
            LinkPhoneRequest(phoneNumber="not a number")
