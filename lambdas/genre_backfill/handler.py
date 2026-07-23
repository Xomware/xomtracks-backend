"""
XOMTRACKS Genre Backfill
========================
Populates the `genres` field on `matched` / `manual` shares for the upcoming
feed genre filter. Spotify TRACK objects carry no genres -- only ARTIST
objects do -- so this is a two-hop, batched, dedupe-by-artist enrichment:

1. Gather the distinct `resolvedSpotifyId`s of matched/manual shares that do
   not yet have a `genres` field.
2. Batch-hydrate those tracks (`GET /v1/tracks?ids=`, 50/call) to read each
   track's PRIMARY artist id.
3. Dedupe the primary artist ids and batch-hydrate the ARTISTS
   (`GET /v1/artists?ids=`, 50/call) to read each artist's `genres`.
4. Write `genres` (a list of strings, possibly empty) back per share.

This one runner covers BOTH the historical backfill (shares matched before
`genres` existed) and ongoing enrichment: freshly-matched shares written by
the matching sweep have no `genres` key, so they are naturally picked up on
the next backfill run. `needs_genres` keys off the ABSENCE of the field --
a share already enriched with `[]` (artist has no genres) is not retried.

Read-only Spotify catalog access -> app token (client-credentials), no user
refresh token required (same as matching_sweep / album_art_backfill).

Degrade gracefully: a track that no longer resolves is left for a later run;
an artist with no genres yields `[]`. Nothing here raises for the expected
"couldn't resolve" cases.

Pure logic (needs_genres / collect_* / build_genre_updates / run_backfill) is
network- and DynamoDB-free and unit-tested; the live edges are injected.
"""

from typing import Any, Callable

from lambdas.common.genres import (
    genres_for_track,
    primary_artist_id,
)
from lambdas.common.logger import get_logger

# NOTE: shares_dynamo / spotify are imported lazily inside the live-edge
# functions (not at module top) -- both create a boto3 resource at import
# time, and keeping them off this module's import path lets the pure unit
# tests import the logic without touching boto3 (same rationale as
# album_art_backfill).

log = get_logger(__file__)

HANDLER = "genre_backfill"

# Spotify caps GET /tracks?ids= and GET /artists?ids= at 50 ids per request.
_BATCH_LIMIT = 50


# ============================================
# Pure logic (network-free, unit-tested)
# ============================================

def needs_genres(share: dict) -> bool:
    """
    A matched/manual share with a resolved Spotify id that has never been
    genre-enriched (the `genres` key is absent). A share already carrying
    `genres` -- even an empty list -- is considered done and skipped.
    """
    return (
        share.get("matchStatus") in ("matched", "manual")
        and bool(share.get("resolvedSpotifyId"))
        and share.get("genres") is None
    )


def collect_track_ids_needing_genres(shares: list[dict]) -> list[str]:
    """Deduped, order-preserving Spotify track ids for shares needing genres."""
    ids: list[str] = []
    seen: set[str] = set()
    for share in shares:
        if not needs_genres(share):
            continue
        track_id = share.get("resolvedSpotifyId")
        if track_id and track_id not in seen:
            seen.add(track_id)
            ids.append(track_id)
    return ids


def collect_primary_artist_ids(tracks_by_id: dict[str, dict]) -> list[str]:
    """Deduped, order-preserving primary-artist ids across the fetched tracks."""
    ids: list[str] = []
    seen: set[str] = set()
    for track in tracks_by_id.values():
        artist_id = primary_artist_id(track)
        if artist_id and artist_id not in seen:
            seen.add(artist_id)
            ids.append(artist_id)
    return ids


def build_genre_updates(
    shares: list[dict],
    tracks_by_id: dict[str, dict],
    genres_by_artist: dict[str, list[str]],
) -> list[tuple[str, dict]]:
    """
    Map each share needing genres to `(shareId, {"genres": [...]})`.

    A share whose track didn't resolve (removed from the catalog) is OMITTED
    so it is retried on a later run rather than pinned to `[]`. A track whose
    primary artist simply has no genres yields `{"genres": []}` -- a real,
    terminal "unknown genre" that the feed filter reads as such.
    """
    updates: list[tuple[str, dict]] = []
    for share in shares:
        if not needs_genres(share):
            continue
        track = tracks_by_id.get(share.get("resolvedSpotifyId"))
        if not track:
            continue
        updates.append((share["shareId"], {"genres": genres_for_track(track, genres_by_artist)}))
    return updates


def run_backfill(
    shares: list[dict],
    *,
    track_fetch: Callable[[list[str]], dict[str, dict]],
    artist_fetch: Callable[[list[str]], dict[str, list[str]]],
    persist: Callable[[str, dict], Any],
) -> dict:
    """
    Orchestrate the genre backfill over `shares` across injectable edges.

    Args:
        shares: matched/manual share dicts (typically the full matched set).
        track_fetch: (track_ids) -> {track_id: track} -- batched Spotify
            /tracks hydrate (needed only to read each track's primary artist).
        artist_fetch: (artist_ids) -> {artist_id: [genres]} -- batched Spotify
            /artists hydrate.
        persist: (shareId, {"genres": [...]}) -> None.

    Returns:
        Summary dict: candidates (shares needing genres), updated,
        withGenres (of those updated, how many got a non-empty list),
        skipped.
    """
    track_ids = collect_track_ids_needing_genres(shares)
    tracks_by_id = track_fetch(track_ids) if track_ids else {}

    artist_ids = collect_primary_artist_ids(tracks_by_id)
    genres_by_artist = artist_fetch(artist_ids) if artist_ids else {}

    updates = build_genre_updates(shares, tracks_by_id, genres_by_artist)
    for share_id, fields in updates:
        persist(share_id, fields)

    candidates = sum(1 for s in shares if needs_genres(s))
    with_genres = sum(1 for _, fields in updates if fields["genres"])
    return {
        "candidates": candidates,
        "updated": len(updates),
        "withGenres": with_genres,
        "skipped": len(shares) - candidates,
    }


# ============================================
# Live edges
# ============================================

def _track_fetch_edge(spotify) -> Callable[[list[str]], dict[str, dict]]:
    """`track_fetch(ids) -> {id: track}` bound to a live sync app-token client."""

    def track_fetch(ids: list[str]) -> dict[str, dict]:
        tracks_by_id: dict[str, dict] = {}
        for start in range(0, len(ids), _BATCH_LIMIT):
            chunk = ids[start:start + _BATCH_LIMIT]
            for track in spotify.get_tracks_by_ids(chunk):
                if track and track.get("id"):
                    tracks_by_id[track["id"]] = track
        return tracks_by_id

    return track_fetch


def _artist_fetch_edge(spotify) -> Callable[[list[str]], dict[str, list[str]]]:
    """`artist_fetch(ids) -> {id: [genres]}` bound to a live sync app-token client."""
    from lambdas.common.genres import genres_by_artist_map

    def artist_fetch(ids: list[str]) -> dict[str, list[str]]:
        genres_by_artist: dict[str, list[str]] = {}
        for start in range(0, len(ids), _BATCH_LIMIT):
            chunk = ids[start:start + _BATCH_LIMIT]
            genres_by_artist.update(genres_by_artist_map(spotify.get_artists_by_ids(chunk)))
        return genres_by_artist

    return artist_fetch


def backfill() -> dict:
    """
    Load the matched/manual shares and backfill genres onto the ones that
    have never been enriched. Runnable core shared by the Lambda handler and
    local invocation (`python -m lambdas.genre_backfill.handler`).
    """
    from lambdas.common.shares_dynamo import scan_shares_by_match_status, update_match_result
    from lambdas.common.spotify import Spotify

    shares = scan_shares_by_match_status("matched") + scan_shares_by_match_status("manual")
    log.info(f"Genre backfill starting: {len(shares)} matched/manual share(s) scanned")

    spotify = Spotify(app_only=True)  # read-only catalog -> app token

    summary = run_backfill(
        shares,
        track_fetch=_track_fetch_edge(spotify),
        artist_fetch=_artist_fetch_edge(spotify),
        persist=lambda share_id, fields: update_match_result(share_id, **fields),
    )

    log.info(
        f"Genre backfill complete: candidates={summary['candidates']} "
        f"updated={summary['updated']} withGenres={summary['withGenres']} "
        f"skipped={summary['skipped']}"
    )
    return summary


def handler(event: dict, context: Any) -> dict:
    """Invoke entrypoint for a one-shot genre backfill."""
    return backfill()


if __name__ == "__main__":
    import json

    print(json.dumps(backfill(), indent=2))
