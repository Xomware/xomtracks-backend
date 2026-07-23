"""
XOMTRACKS Pydantic Models
=========================
Request/response boundary validation, per backend.md ("validate at the
boundary: Pydantic"). Mirrors the Share data model in
docs/features/xomtracks/PLAN.md.
"""

import re

from pydantic import BaseModel, Field, field_validator

from lambdas.common.constants import MATCH_STATUSES, PLATFORMS

DIRECTIONS = ("in", "out")


class ShareIngestRequest(BaseModel):
    """
    What the extractor POSTs to /shares/ingest for every music link found
    in a scan. One record per (messageGuid, sourceUrl) pair -- a message
    with multiple links produces multiple ingest requests.
    """

    messageGuid: str = Field(min_length=1)
    direction: str
    sharerHandle: str | None = None
    sharerName: str | None = None
    chatId: str | None = None
    platform: str
    sourceUrl: str = Field(min_length=1)
    messageDate: int  # unix epoch seconds (already converted from Apple epoch)

    @field_validator("messageGuid", "sourceUrl")
    @classmethod
    def not_blank(cls, v: str) -> str:
        if not v.strip():
            raise ValueError("must not be blank")
        return v.strip()

    @field_validator("direction")
    @classmethod
    def direction_is_known(cls, v: str) -> str:
        if v not in DIRECTIONS:
            raise ValueError(f"direction must be one of {DIRECTIONS}")
        return v

    @field_validator("platform")
    @classmethod
    def platform_is_known(cls, v: str) -> str:
        if v not in PLATFORMS:
            raise ValueError(f"platform must be one of {PLATFORMS}")
        return v


class Share(BaseModel):
    """The full stored/returned Share record -- matches the DynamoDB item shape."""

    shareId: str
    messageGuid: str
    direction: str
    sharerHandle: str | None = None
    sharerName: str | None = None
    chatId: str | None = None
    platform: str
    sourceUrl: str
    messageDate: int
    trackTitle: str | None = None
    trackArtist: str | None = None
    albumName: str | None = None
    albumArtUrl: str | None = None
    resolvedSpotifyId: str | None = None
    resolvedSpotifyUri: str | None = None
    matchStatus: str = "pending"
    matchConfidence: float | None = Field(default=None, ge=0.0, le=1.0)
    createdAt: str

    @field_validator("direction")
    @classmethod
    def direction_is_known(cls, v: str) -> str:
        if v not in DIRECTIONS:
            raise ValueError(f"direction must be one of {DIRECTIONS}")
        return v

    @field_validator("platform")
    @classmethod
    def platform_is_known(cls, v: str) -> str:
        if v not in PLATFORMS:
            raise ValueError(f"platform must be one of {PLATFORMS}")
        return v

    @field_validator("matchStatus")
    @classmethod
    def match_status_is_known(cls, v: str) -> str:
        if v not in MATCH_STATUSES:
            raise ValueError(f"matchStatus must be one of {MATCH_STATUSES}")
        return v


_SPOTIFY_TRACK_ID_PATTERNS = (
    re.compile(r"track/([a-zA-Z0-9]+)"),
    re.compile(r"spotify:track:([a-zA-Z0-9]+)"),
)


def extract_spotify_track_id(value: str) -> str:
    """Accept either a bare track id or a full Spotify URL/URI."""
    for pattern in _SPOTIFY_TRACK_ID_PATTERNS:
        match = pattern.search(value)
        if match:
            return match.group(1)
    return value


class MatchOverrideRequest(BaseModel):
    """POST /shares/{id}/match-override -- Dom (or a signed-in user) picks
    the correct Spotify track by hand for a permanently-unmatched share."""

    spotifyTrackId: str = Field(min_length=1)

    @field_validator("spotifyTrackId")
    @classmethod
    def not_blank_and_normalized(cls, v: str) -> str:
        if not v.strip():
            raise ValueError("spotifyTrackId must not be blank")
        return extract_spotify_track_id(v.strip())


class LinkPhoneRequest(BaseModel):
    """
    POST /me/link-phone -- a signed-in group member links their phone number
    to their Cognito identity so they can see + be attributed for their own
    shares. Accepts any human phone format; the handler normalizes to last-10
    digits (see phone.normalize_phone). We keep the raw string here and only
    require that it contains at least one digit -- normalization/validation of
    the digit count happens in the handler.
    """

    phoneNumber: str = Field(min_length=1)

    @field_validator("phoneNumber")
    @classmethod
    def has_digits(cls, v: str) -> str:
        if not v.strip():
            raise ValueError("phoneNumber must not be blank")
        if not any(ch.isdigit() for ch in v):
            raise ValueError("phoneNumber must contain digits")
        return v.strip()


class CreatePlaylistRequest(BaseModel):
    """
    POST /playlists/create -- on-the-spot playlist build from a hand-picked
    selection. The feed's multi-select "make a playlist from history" action
    calls this with a list of shareIds (resolved to their Spotify URIs
    server-side) and/or raw Spotify trackIds, plus a name.

    At least one of shareIds/trackIds must be non-empty -- an empty playlist
    request is a client bug, not a valid create.
    """

    name: str = Field(min_length=1, max_length=100)
    shareIds: list[str] = Field(default_factory=list)
    trackIds: list[str] = Field(default_factory=list)
    description: str | None = Field(default=None, max_length=300)

    @field_validator("name")
    @classmethod
    def name_not_blank(cls, v: str) -> str:
        if not v.strip():
            raise ValueError("name must not be blank")
        return v.strip()

    @field_validator("trackIds")
    @classmethod
    def normalize_track_ids(cls, v: list[str]) -> list[str]:
        # Accept bare ids, full URLs, or URIs -- normalize each to a bare id.
        return [extract_spotify_track_id(t.strip()) for t in v if t and t.strip()]

    @field_validator("shareIds")
    @classmethod
    def strip_share_ids(cls, v: list[str]) -> list[str]:
        return [s.strip() for s in v if s and s.strip()]

    def has_selection(self) -> bool:
        return bool(self.shareIds or self.trackIds)
