"""
XOMTRACKS Genre Helpers
=======================
Genuinely new for the upcoming genre filter. Spotify TRACK objects do not
carry genres -- only ARTIST objects do -- so a track's genre is derived from
its primary artist (a second `GET /v1/artists?ids=` hop, batched + deduped
by artist id for efficiency).

This module holds only the network-free helpers (unit-tested). The live
edges (Spotify batch fetch, DynamoDB scan/write) live in the genre_backfill
lambda and the matching pipeline, which inject these pure functions.

Degrade gracefully: a track with no artist, an artist with no genres, or a
share whose track no longer resolves all yield an empty genre list -- never
an exception, so a missing genre can never 500 the feed or derail a sweep.
"""

from lambdas.common.logger import get_logger

log = get_logger(__file__)


def primary_artist_id(track: dict) -> str | None:
    """
    The Spotify artist id of a track's FIRST (primary) artist, or None.

    Genres are an artist-level attribute, and a track's primary artist is the
    right proxy for "the track's genre" -- featured artists muddy it.
    """
    artists = track.get("artists") or []
    if not artists:
        return None
    return artists[0].get("id")


def artist_genres(artist: dict) -> list[str]:
    """A Spotify artist object's genres as a plain list (never None)."""
    return list(artist.get("genres") or [])


def genres_by_artist_map(artists: list[dict]) -> dict[str, list[str]]:
    """Build {artistId: [genres]} from a list of Spotify artist objects."""
    out: dict[str, list[str]] = {}
    for artist in artists or []:
        artist_id = artist.get("id")
        if artist_id:
            out[artist_id] = artist_genres(artist)
    return out


def genres_for_track(track: dict, genres_by_artist: dict[str, list[str]]) -> list[str]:
    """
    Resolve a single track's genres from a prefetched {artistId: genres} map.
    Empty list when the track has no primary artist or that artist carries no
    genres -- callers persist `[]`, which the feed filter reads as "unknown".
    """
    artist_id = primary_artist_id(track)
    if not artist_id:
        return []
    return list(genres_by_artist.get(artist_id) or [])


def ensure_genres(shares: list[dict]) -> list[dict]:
    """
    Guarantee every share dict carries a `genres` LIST (in place), defaulting
    to [] when the attribute is absent or not yet a list.

    The /shares/list + /me/shares feeds call this so the frontend genre filter
    can always read `share.genres` as a string[] -- historical shares that
    predate genre enrichment still surface as an empty list rather than a
    missing key.
    """
    for share in shares or []:
        value = share.get("genres")
        if not isinstance(value, list):
            share["genres"] = []
    return shares
