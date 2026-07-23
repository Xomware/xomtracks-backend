"""
XOMTRACKS Cross-Platform Matching
==================================
Genuinely new code (PLAN.md Phase 3) -- xomify's groups_add_song_url only
handles Spotify URLs; resolving SoundCloud/Apple Music links to Spotify
tracks has no precedent to vendor.

Branches by platform:
- spotify -> regex-extract track id (extract_spotify_track_id, same
  pattern as xomify's groups_add_song_url.extract_track_id) -> hydrate via
  GET /v1/tracks/{id} -> matched, matchConfidence=1.0.
- soundcloud -> resolve title+artist via the xomcloud scraped-client-id
  path -> Spotify /search -> fuzzy match.
- apple -> resolve title+artist via the public itunes.apple.com/lookup
  (no auth needed) -> Spotify /search -> fuzzy match.

Fuzzy match: normalize artist+title into one string, rapidfuzz
token_set_ratio, confidence threshold (MATCH_CONFIDENCE_THRESHOLD).
Above -> matched; below -> unmatched. Permanent-unmatched is EXPECTED for
SC remixes/bootlegs that were never released to Spotify -- it is not an
error state, and is excluded from playlists (Phase 4/5).

Resolver failures (network errors, no-metadata-found) degrade to
`unmatched` rather than propagating -- one bad SoundCloud/Apple link
should never blow up a matching sweep across many pending shares.
"""

import re

import requests
from rapidfuzz import fuzz

from lambdas.common.constants import MATCH_CONFIDENCE_THRESHOLD
from lambdas.common.errors import ValidationError
from lambdas.common.logger import get_logger

log = get_logger(__file__)

_SPOTIFY_TRACK_ID_PATTERNS = (
    re.compile(r"track/([a-zA-Z0-9]+)"),
    re.compile(r"spotify:track:([a-zA-Z0-9]+)"),
)


def extract_spotify_track_id(url: str) -> str | None:
    """Extract a bare Spotify track id from a URL or URI. None if not found."""
    for pattern in _SPOTIFY_TRACK_ID_PATTERNS:
        match = pattern.search(url)
        if match:
            return match.group(1)
    return None


def _track_title_artist(track: dict) -> tuple[str, str]:
    title = track.get("name") or ""
    artists = track.get("artists") or []
    artist = artists[0].get("name") if artists else ""
    return title, artist


def album_art_url(track: dict) -> str | None:
    """
    Pick a card-sized cover URL from a Spotify track's album images.

    Spotify returns album images largest-first (typically 640 / 300 / 64).
    The browse feed renders covers at ~250px, so the middle (~300px) image
    is the right tradeoff between crispness and bytes; fall back to the
    first (largest) when only one size exists, and None when a track has no
    album images at all (rare, but real -- degrade to no cover, not a crash).
    """
    images = ((track.get("album") or {}).get("images")) or []
    if not images:
        return None
    chosen = images[1] if len(images) > 1 else images[0]
    return chosen.get("url")


def _album_fields(track: dict) -> dict:
    """Album cover + name persisted on the share so the feed needs no
    client-side Spotify calls to render a card."""
    return {
        "albumArtUrl": album_art_url(track),
        "albumName": (track.get("album") or {}).get("name"),
    }


def _spotify_result(
    track: dict,
    match_status: str,
    confidence: float | None,
    genres: list[str] | None = None,
) -> dict:
    title, artist = _track_title_artist(track)
    result = {
        "matchStatus": match_status,
        "matchConfidence": confidence,
        "resolvedSpotifyId": track.get("id"),
        "resolvedSpotifyUri": track.get("uri"),
        "trackTitle": title,
        "trackArtist": artist,
        **_album_fields(track),
    }
    # Only attach `genres` when the caller actually resolved them. Omitting
    # the key (rather than persisting `[]`) is deliberate: it lets the genre
    # backfill distinguish "never enriched" (key absent) from "enriched, no
    # genres found" (key present as []), so a freshly-matched share written
    # by the sweep -- which does NOT fetch artist genres -- is still picked
    # up by genre_backfill on its next run.
    if genres is not None:
        result["genres"] = list(genres)
    return result


def _unmatched_result(title: str | None = None, artist: str | None = None) -> dict:
    """
    Fields for a share that resolves to NO Spotify track.

    title/artist default to None (Spotify-URL branch: a bad/removed id has no
    recoverable metadata). The SoundCloud/Apple branches pass the resolved
    source title/artist through so an unmatched share still shows its REAL
    name in the feed -- a SoundCloud-only track that was never released to
    Spotify renders as e.g. "Artist — Track" instead of "Untitled", even
    though it has no resolvedSpotifyId and is excluded from playlists.
    """
    return {
        "matchStatus": "unmatched",
        "matchConfidence": None,
        "resolvedSpotifyId": None,
        "resolvedSpotifyUri": None,
        "trackTitle": title,
        "trackArtist": artist,
        "albumArtUrl": None,
        "albumName": None,
    }


def fuzzy_best_match(
    title: str,
    artist: str,
    candidates: list[dict],
) -> tuple[dict | None, float]:
    """
    Score every candidate Spotify track against (title, artist) using
    rapidfuzz's token_set_ratio (order/duplicate-word insensitive -- handles
    "Song (feat. X)" vs "Song" and reordered artist/title strings well).

    Returns:
        (best_candidate_or_None, score_0_to_1)
    """
    if not candidates:
        return None, 0.0

    query = f"{artist} {title}".strip().lower()

    best_candidate = None
    best_score = -1.0
    for candidate in candidates:
        cand_title, cand_artist = _track_title_artist(candidate)
        target = f"{cand_artist} {cand_title}".strip().lower()
        score = fuzz.token_set_ratio(query, target) / 100.0
        if score > best_score:
            best_score = score
            best_candidate = candidate

    return best_candidate, max(best_score, 0.0)


async def default_soundcloud_resolver(url: str) -> tuple[str, str] | None:
    """
    Resolve a SoundCloud URL to (title, artist) via the scraped
    `client_id` path (same credential xomcloud-backend's downloader.py
    uses -- see lambdas/common/ssm_helpers.SOUNDCLOUD_CLIENT_ID).

    Uses the synchronous `requests` library (matches xomcloud's own scdl
    usage, which is also sync) -- briefly blocks the event loop per call.
    Acceptable at this app's scale (personal-use matching sweeps, not a
    high-concurrency service); revisit with a thread executor if that
    changes.
    """
    from lambdas.common import ssm_helpers

    client_id = ssm_helpers.SOUNDCLOUD_CLIENT_ID
    resp = requests.get(
        "https://api-v2.soundcloud.com/resolve",
        params={"url": url, "client_id": client_id},
        timeout=10,
    )
    if resp.status_code != 200:
        log.warning(f"SoundCloud resolve failed ({resp.status_code}) for {url}")
        return None

    data = resp.json()
    title = data.get("title")
    artist = (data.get("user") or {}).get("username")
    if not title:
        return None
    return title, artist or ""


_APPLE_TRACK_ID_PATTERN = re.compile(r"[?&]i=(\d+)")
_APPLE_ALBUM_ID_PATTERN = re.compile(r"/(\d+)(?:\?|$)")


def _extract_apple_track_id(url: str) -> str | None:
    """
    Apple Music song links carry the individual track id as `?i=<id>`
    when shared from an album, or as the trailing path segment for a
    standalone song link. Prefer `i=` when present (album-context share).
    """
    match = _APPLE_TRACK_ID_PATTERN.search(url)
    if match:
        return match.group(1)
    match = _APPLE_ALBUM_ID_PATTERN.search(url)
    if match:
        return match.group(1)
    return None


async def default_apple_music_resolver(url: str) -> tuple[str, str] | None:
    """
    Resolve an Apple Music URL to (title, artist) via the public,
    unauthenticated `itunes.apple.com/lookup` endpoint. Sync `requests`
    call inside an async function -- see default_soundcloud_resolver's
    docstring for the tradeoff.
    """
    track_id = _extract_apple_track_id(url)
    if not track_id:
        return None

    resp = requests.get(
        "https://itunes.apple.com/lookup",
        params={"id": track_id, "entity": "song"},
        timeout=10,
    )
    if resp.status_code != 200:
        log.warning(f"Apple Music lookup failed ({resp.status_code}) for {url}")
        return None

    data = resp.json()
    results = data.get("results") or []
    if not results:
        return None

    result = results[0]
    title = result.get("trackName")
    artist = result.get("artistName")
    if not title:
        return None
    return title, artist or ""


async def match_share(
    share: dict,
    spotify,
    soundcloud_resolver=default_soundcloud_resolver,
    apple_resolver=default_apple_music_resolver,
    threshold: float = MATCH_CONFIDENCE_THRESHOLD,
) -> dict:
    """
    Resolve a single `pending` share to a Spotify track (or permanently
    `unmatched`). Never raises for expected "couldn't resolve" cases --
    those degrade to `unmatched` so a matching sweep over many pending
    shares can't be derailed by one bad link.

    Args:
        share: dict with at least `platform` and `sourceUrl`.
        spotify: an object exposing `aiohttp_get_track(id)` and
            `aiohttp_search_track(query, limit=5)` (both async) -- normally
            a lambdas.common.spotify.Spotify instance, injectable for tests.
        soundcloud_resolver / apple_resolver: async callables
            `(url) -> (title, artist) | None`. Both default to the real
            network resolvers above; injectable for tests.

    Returns:
        Partial dict of fields to persist via shares_dynamo.update_match_result.
    """
    platform = share.get("platform")
    url = share.get("sourceUrl")

    if platform == "spotify":
        return await _match_spotify(url, spotify)

    if platform == "soundcloud":
        return await _match_via_resolver(url, spotify, soundcloud_resolver, threshold, "soundcloud")

    if platform == "apple":
        return await _match_via_resolver(url, spotify, apple_resolver, threshold, "apple")

    log.warning(f"Unknown platform for matching: {platform!r}")
    return _unmatched_result()


async def _match_spotify(url: str, spotify) -> dict:
    track_id = extract_spotify_track_id(url)
    if not track_id:
        log.warning(f"Could not extract Spotify track id from URL: {url}")
        return _unmatched_result()

    track = await spotify.aiohttp_get_track(track_id)
    if not track:
        return _unmatched_result()

    return _spotify_result(track, "matched", 1.0)


async def _match_via_resolver(url: str, spotify, resolver, threshold: float, platform: str) -> dict:
    try:
        resolved = await resolver(url)
    except Exception as err:
        log.warning(f"{platform} resolver failed for {url}: {err}")
        return _unmatched_result()

    if not resolved:
        return _unmatched_result()

    title, artist = resolved
    query = f"{artist} {title}".strip()
    candidates = await spotify.aiohttp_search_track(query)

    best, score = fuzzy_best_match(title, artist, candidates)
    if not best or score < threshold:
        # Preserve the resolved SoundCloud/Apple title+artist even though the
        # track isn't on Spotify -- the feed shows its real name, not "Untitled".
        return _unmatched_result(title=title, artist=artist)

    return _spotify_result(best, "matched", round(score, 4))


async def apply_manual_override(spotify, spotify_track_id: str) -> dict:
    """
    POST /shares/{id}/match-override -- Dom (or a signed-in user) picks the
    correct Spotify track by hand. Raises ValidationError (400) if the
    given id doesn't resolve to a real Spotify track.
    """
    track = await spotify.aiohttp_get_track(spotify_track_id)
    if not track:
        raise ValidationError(
            message=f"Spotify track not found: {spotify_track_id}",
            handler="shares_match_override",
            function="apply_manual_override",
            field="spotifyTrackId",
        )
    return _spotify_result(track, "manual", 1.0)
