"""
XOMTRACKS Album-Art Backfill
============================
One-shot backfill that hydrates `albumArtUrl` / `albumName` onto the
historical `matched` shares that were resolved by the matching sweep BEFORE
those two fields existed on the Share model. New matches already carry
album art (matching._spotify_result now persists it); this closes the gap
for the rows already in xomtracks-shares.

Strategy mirrors matching_sweep's Spotify-direct path: gather the distinct
`resolvedSpotifyId`s of matched shares still missing a cover, batch-hydrate
them through Spotify's `GET /v1/tracks?ids=` (50 ids/call, app token), then
write the two album fields back per share via update_match_result.

Read-only Spotify catalog access only -> app token (client-credentials),
no user refresh token required (same as the sweep).

Pure logic (needs_album_art / collect_ids_needing_art / build_album_updates
/ run_backfill) is network- and DynamoDB-free and unit-tested; the live
edges (Spotify batch fetch, DynamoDB scan + write) are injected.
"""

from typing import Any, Callable

from lambdas.common.logger import get_logger
from lambdas.common.matching import _album_fields

# NOTE: shares_dynamo / spotify are imported lazily inside the live-edge
# functions below (not at module top) on purpose. Those modules create a
# boto3 DynamoDB resource at import time; keeping them out of the module
# import path means importing THIS module for its pure logic (the unit
# tests) never touches boto3 at collection time -- which keeps the pure
# tests fast and free of the suite's import-order-sensitive moto setup.

log = get_logger(__file__)

HANDLER = "album_art_backfill"

# Spotify caps GET /tracks?ids= at 50 ids per request.
_BATCH_LIMIT = 50


# ============================================
# Pure logic (network-free, unit-tested)
# ============================================

def needs_album_art(share: dict) -> bool:
    """A matched share with a resolved Spotify id but no cover URL yet."""
    return (
        share.get("matchStatus") in ("matched", "manual")
        and bool(share.get("resolvedSpotifyId"))
        and not share.get("albumArtUrl")
    )


def collect_ids_needing_art(shares: list[dict]) -> list[str]:
    """Deduped, order-preserving Spotify ids for shares that still need art."""
    ids: list[str] = []
    seen: set[str] = set()
    for share in shares:
        if not needs_album_art(share):
            continue
        track_id = share.get("resolvedSpotifyId")
        if track_id and track_id not in seen:
            seen.add(track_id)
            ids.append(track_id)
    return ids


def build_album_updates(
    shares: list[dict],
    tracks_by_id: dict[str, dict],
) -> list[tuple[str, dict]]:
    """
    Map each share needing art to `(shareId, {albumArtUrl, albumName})` using
    a prefetched {track_id: track} map. Shares whose id didn't resolve (track
    removed from the catalog) are omitted -- nothing to write.
    """
    updates: list[tuple[str, dict]] = []
    for share in shares:
        if not needs_album_art(share):
            continue
        track = tracks_by_id.get(share.get("resolvedSpotifyId"))
        if not track:
            continue
        updates.append((share["shareId"], _album_fields(track)))
    return updates


def run_backfill(
    shares: list[dict],
    *,
    batch_fetch: Callable[[list[str]], dict[str, dict]],
    persist: Callable[[str, dict], Any],
) -> dict:
    """
    Orchestrate the backfill over `shares` across injectable edges.

    Args:
        shares: matched/manual share dicts (typically the full matched set).
        batch_fetch: (track_ids) -> {track_id: track} -- batched Spotify
            /tracks hydrate.
        persist: (shareId, {albumArtUrl, albumName}) -> None.

    Returns:
        Summary dict: candidates (shares needing art), updated, skipped.
    """
    ids = collect_ids_needing_art(shares)
    tracks_by_id = batch_fetch(ids) if ids else {}
    updates = build_album_updates(shares, tracks_by_id)

    for share_id, fields in updates:
        persist(share_id, fields)

    candidates = sum(1 for s in shares if needs_album_art(s))
    return {
        "candidates": candidates,
        "updated": len(updates),
        "skipped": len(shares) - candidates,
    }


# ============================================
# Live edges
# ============================================

def _batch_fetch_edge(spotify) -> Callable[[list[str]], dict[str, dict]]:
    """Build a `batch_fetch(ids) -> {id: track}` bound to a live sync app-token
    Spotify client, chunking at 50 ids/call via get_tracks_by_ids."""

    def batch_fetch(ids: list[str]) -> dict[str, dict]:
        tracks_by_id: dict[str, dict] = {}
        for start in range(0, len(ids), _BATCH_LIMIT):
            chunk = ids[start:start + _BATCH_LIMIT]
            for track in spotify.get_tracks_by_ids(chunk):
                if track and track.get("id"):
                    tracks_by_id[track["id"]] = track
        return tracks_by_id

    return batch_fetch


def backfill() -> dict:
    """
    Load the matched shares and backfill album art onto the ones still
    missing it. Runnable core shared by the Lambda handler and local
    invocation (`python -m lambdas.album_art_backfill.handler`).
    """
    from lambdas.common.shares_dynamo import scan_shares_by_match_status, update_match_result
    from lambdas.common.spotify import Spotify

    # Both matched (auto) and manual overrides can predate the album-art
    # fields, so backfill covers both status buckets.
    shares = scan_shares_by_match_status("matched") + scan_shares_by_match_status("manual")
    log.info(f"Album-art backfill starting: {len(shares)} matched/manual share(s) scanned")

    # Read-only catalog hydrate -> Spotify *app* token (client-credentials),
    # exactly like matching_sweep. No user refresh token / users-table row
    # required (get_tracks_by_ids is a sync `requests` call).
    spotify = Spotify(app_only=True)

    summary = run_backfill(
        shares,
        batch_fetch=_batch_fetch_edge(spotify),
        persist=lambda share_id, fields: update_match_result(share_id, **fields),
    )

    log.info(
        f"Album-art backfill complete: candidates={summary['candidates']} "
        f"updated={summary['updated']} skipped={summary['skipped']}"
    )
    return summary


def handler(event: dict, context: Any) -> dict:
    """Invoke entrypoint for a one-shot album-art backfill."""
    return backfill()


if __name__ == "__main__":
    import json

    print(json.dumps(backfill(), indent=2))
