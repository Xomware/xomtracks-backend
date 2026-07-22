"""
XOMTRACKS Matching Sweep Runner
===============================
Resolves stored `pending` shares in xomtracks-shares into real Spotify
tracks. Nothing else invokes matching over stored shares -- this is the
runner that closes that loop, wired to the matching-trigger cron
(Terraform-provisioned `xomtracks-matching-sweep`, folder domain/rest =
`matching`/`sweep`).

Two resolution strategies, split by platform:

- Spotify URLs (the bulk): the track id is parseable straight from the URL
  -- no search needed. Ids are batched through Spotify's
  `GET /v1/tracks?ids=` endpoint (up to 50 ids/call) and each share is
  matched (confidence 1.0) or marked `unmatched` if the id no longer
  resolves. This is dramatically cheaper than one call per share.

- SoundCloud / Apple Music URLs: resolve title+artist (SoundCloud via the
  scraped client_id path, Apple via the public itunes.apple.com/lookup),
  then fuzzy-search Spotify via lambdas.common.matching.match_share and
  take the best confident match, else `unmatched`.

Note on statuses: this repo's canonical "confidently not findable on
Spotify" status is `unmatched` (see constants.MATCH_STATUSES and the
matching module) -- that is the state the product/UI already understands as
"no Spotify equivalent" (a.k.a. not-on-Spotify), and it is NOT an error.

Auth: read-only catalog access (GET /tracks, /search) only, so the sweep
authenticates with a Spotify *app* token via the client-credentials flow
(Spotify(app_only=True)) using the /xomtracks/spotify/* app credentials --
no user refresh token, no users-table row required.

Rate limits: Spotify 429s are retried with exponential backoff (honoring
Retry-After when present) on both the batch and search paths; a small
inter-batch pause keeps the sweep polite.

Results are written back per share via shares_dynamo.update_match_result
(matchStatus, matchConfidence, resolvedSpotifyId, resolvedSpotifyUri,
trackTitle, trackArtist). The raw `sourceUrl` and every other field are left
untouched.
"""

import asyncio
import re
import time
from typing import Any, Callable

import aiohttp

from lambdas.common.constants import MATCH_CONFIDENCE_THRESHOLD, PLATFORMS
from lambdas.common.errors import handle_errors
from lambdas.common.logger import get_logger
from lambdas.common.matching import (
    _spotify_result,
    _unmatched_result,
    extract_spotify_track_id,
    match_share,
)
from lambdas.common.shares_dynamo import scan_shares_by_match_status, update_match_result
from lambdas.common.spotify import Spotify

log = get_logger(__file__)

HANDLER = "matching_sweep"

# Spotify caps GET /tracks?ids= at 50 ids per request.
_BATCH_LIMIT = 50
# Polite pause between batch calls (seconds) -- keeps a large sweep well
# under Spotify's rolling rate window without meaningfully slowing it.
_INTER_BATCH_PAUSE = 0.1
# 429 backoff.
_MAX_RETRIES = 5
_BASE_BACKOFF = 1.0

# A well-formed Spotify track id is exactly 22 base62 chars. Filtering to
# this BEFORE the batch call matters: Spotify 400s the entire /tracks?ids=
# chunk if a single id is malformed, and real data contains both non-track
# URLs (artist/album/playlist) and truncation artifacts (a `...WHttpURL`
# suffix left by link-preview parsing upstream). Malformed ids simply never
# enter a batch -> their shares resolve to `unmatched` via the map lookup.
_VALID_TRACK_ID = re.compile(r"^[A-Za-z0-9]{22}$")


def _valid_track_id(url: str) -> str | None:
    """Extract a Spotify track id from a URL only if it is well-formed."""
    track_id = extract_spotify_track_id(url or "")
    if track_id and _VALID_TRACK_ID.match(track_id):
        return track_id
    return None


# ============================================
# Pure logic (network-free, unit-tested)
# ============================================

def partition_shares(shares: list[dict]) -> tuple[list[dict], list[dict]]:
    """
    Split pending shares into (spotify_direct, search) buckets. Only
    `platform == "spotify"` takes the direct-id batch path; everything else
    (soundcloud, apple, and any unknown platform) goes through the resolver
    + fuzzy-search path, which degrades safely to `unmatched`.
    """
    spotify_shares = [s for s in shares if s.get("platform") == "spotify"]
    search_shares = [s for s in shares if s.get("platform") != "spotify"]
    return spotify_shares, search_shares


def collect_track_ids(spotify_shares: list[dict]) -> list[str]:
    """Deduped, order-preserving list of parseable Spotify track ids."""
    ids: list[str] = []
    seen: set[str] = set()
    for share in spotify_shares:
        track_id = _valid_track_id(share.get("sourceUrl"))
        if track_id and track_id not in seen:
            seen.add(track_id)
            ids.append(track_id)
    return ids


def spotify_batch_results(
    spotify_shares: list[dict],
    tracks_by_id: dict[str, dict],
) -> list[tuple[dict, dict]]:
    """
    Map each Spotify-URL share to its match-result fields using a prefetched
    {track_id: track} map. Matched (confidence 1.0) when the id resolved,
    `unmatched` when the id was unparseable or the track no longer exists.
    """
    results: list[tuple[dict, dict]] = []
    for share in spotify_shares:
        track_id = _valid_track_id(share.get("sourceUrl"))
        track = tracks_by_id.get(track_id) if track_id else None
        if track:
            results.append((share, _spotify_result(track, "matched", 1.0)))
        else:
            results.append((share, _unmatched_result()))
    return results


def summarize(
    results: list[tuple[dict, dict]],
    errors: list[tuple[dict, str]],
    examples_limit: int = 8,
) -> dict:
    """Build the sweep summary: status counts, example matches, error count."""
    matched = sum(1 for _, f in results if f.get("matchStatus") == "matched")
    unmatched = sum(1 for _, f in results if f.get("matchStatus") == "unmatched")

    examples: list[dict] = []
    for share, fields in results:
        if fields.get("matchStatus") == "matched" and len(examples) < examples_limit:
            examples.append({
                "title": fields.get("trackTitle"),
                "artist": fields.get("trackArtist"),
                "platform": share.get("platform"),
                "direction": share.get("direction"),
            })

    return {
        "processed": len(results),
        "matched": matched,
        "unmatched": unmatched,
        "errors": len(errors),
        "examples": examples,
    }


def run_sweep(
    shares: list[dict],
    *,
    batch_fetch: Callable[[list[str]], dict[str, dict]],
    search_batch: Callable[[list[dict]], list[tuple[dict, Any]]],
    persist: Callable[[str, dict], Any],
    examples_limit: int = 8,
) -> dict:
    """
    Orchestrate a matching sweep over `shares` across injectable edges.

    Args:
        shares: the pending share dicts to resolve.
        batch_fetch: (track_ids) -> {track_id: track} -- the batched Spotify
            /tracks lookup (with rate-limit backoff) at the real edge.
        search_batch: (search_shares) -> [(share, fields | Exception)] --
            the SoundCloud/Apple resolve + Spotify fuzzy-search path. An
            Exception outcome is counted as an error and NOT persisted.
        persist: (shareId, fields) -> None -- writes match results back.

    Returns:
        The summary dict from summarize().
    """
    spotify_shares, search_shares = partition_shares(shares)

    results: list[tuple[dict, dict]] = []
    errors: list[tuple[dict, str]] = []

    if spotify_shares:
        tracks_by_id = batch_fetch(collect_track_ids(spotify_shares))
        results.extend(spotify_batch_results(spotify_shares, tracks_by_id))

    if search_shares:
        for share, outcome in search_batch(search_shares):
            if isinstance(outcome, Exception):
                errors.append((share, str(outcome)))
            else:
                results.append((share, outcome))

    for share, fields in results:
        persist(share["shareId"], fields)

    return summarize(results, errors, examples_limit=examples_limit)


# ============================================
# Network edges (rate-limit aware)
# ============================================

def _is_rate_limited(err: Exception) -> bool:
    return "429" in str(err)


def _batch_fetch_edge(spotify: Spotify, sleep_fn: Callable[[float], None] = time.sleep):
    """
    Build a `batch_fetch(ids) -> {id: track}` bound to a live sync Spotify
    client, chunking at 50 ids/call and backing off on 429.
    """
    def batch_fetch(ids: list[str]) -> dict[str, dict]:
        tracks_by_id: dict[str, dict] = {}
        for start in range(0, len(ids), _BATCH_LIMIT):
            chunk = ids[start:start + _BATCH_LIMIT]
            for track in _fetch_chunk_resilient(spotify, chunk, sleep_fn):
                if track and track.get("id"):
                    tracks_by_id[track["id"]] = track
            sleep_fn(_INTER_BATCH_PAUSE)
        return tracks_by_id

    return batch_fetch


def _fetch_chunk_resilient(
    spotify: Spotify,
    chunk: list[str],
    sleep_fn: Callable[[float], None],
) -> list[dict]:
    """
    Fetch one <=50-id chunk, retrying 429s. If Spotify rejects the chunk for
    a non-rate-limit reason (e.g. a 400 "Invalid base62 id" that slipped
    past pre-validation), bisect and recurse so one bad id can never sink
    the other 49. A single offending id is logged and dropped.
    """
    if not chunk:
        return []
    try:
        return _call_sync_with_backoff(lambda: spotify.get_tracks_by_ids(chunk), sleep_fn)
    except Exception as err:
        if _is_rate_limited(err):
            raise  # exhausted 429 retries -- genuinely fatal
        if len(chunk) == 1:
            log.warning(f"Dropping unresolvable Spotify id {chunk[0]!r}: {err}")
            return []
        mid = len(chunk) // 2
        log.warning(f"Chunk of {len(chunk)} rejected ({err}); bisecting to isolate bad id(s)")
        return (
            _fetch_chunk_resilient(spotify, chunk[:mid], sleep_fn)
            + _fetch_chunk_resilient(spotify, chunk[mid:], sleep_fn)
        )


def _call_sync_with_backoff(fn: Callable[[], Any], sleep_fn: Callable[[float], None]) -> Any:
    attempt = 0
    while True:
        try:
            return fn()
        except Exception as err:
            attempt += 1
            if not _is_rate_limited(err) or attempt > _MAX_RETRIES:
                raise
            wait = _BASE_BACKOFF * (2 ** (attempt - 1))
            log.warning(f"Spotify rate-limited (429); backing off {wait:.1f}s (attempt {attempt})")
            sleep_fn(wait)


def _search_batch_edge(threshold: float = MATCH_CONFIDENCE_THRESHOLD):
    """
    Build a `search_batch(shares) -> [(share, fields | Exception)]` that
    runs the async SoundCloud/Apple resolve + Spotify fuzzy-search path over
    a single aiohttp session and app token. Per-share failures are returned
    as the Exception outcome (counted as an error, not fatal to the sweep).
    """
    def search_batch(search_shares: list[dict]) -> list[tuple[dict, Any]]:
        if not search_shares:
            return []
        return asyncio.run(_search_all(search_shares, threshold))

    return search_batch


async def _search_all(search_shares: list[dict], threshold: float) -> list[tuple[dict, Any]]:
    out: list[tuple[dict, Any]] = []
    async with aiohttp.ClientSession() as session:
        spotify = Spotify(app_only=True, session=session)
        await spotify.aiohttp_initialize_app_token()
        for share in search_shares:
            try:
                fields = await _match_with_backoff(share, spotify, threshold)
                out.append((share, fields))
            except Exception as err:
                log.warning(f"Search resolve failed for share {share.get('shareId')}: {err}")
                out.append((share, err))
    return out


async def _match_with_backoff(share: dict, spotify: Spotify, threshold: float) -> dict:
    attempt = 0
    while True:
        try:
            return await match_share(share, spotify, threshold=threshold)
        except Exception as err:
            attempt += 1
            if not _is_rate_limited(err) or attempt > _MAX_RETRIES:
                raise
            wait = _BASE_BACKOFF * (2 ** (attempt - 1))
            log.warning(f"Spotify search rate-limited (429); backing off {wait:.1f}s (attempt {attempt})")
            await asyncio.sleep(wait)


# ============================================
# Entry points
# ============================================

def sweep_pending(match_status: str = "pending", examples_limit: int = 8) -> dict:
    """
    Load every share with the given matchStatus and resolve it. This is the
    runnable core shared by the Lambda handler and local invocation
    (`python -m lambdas.matching_sweep.handler`).
    """
    shares = scan_shares_by_match_status(match_status)
    log.info(f"Matching sweep starting: {len(shares)} share(s) with matchStatus={match_status!r}")

    sync_spotify = Spotify(app_only=True)  # sync app token for the batch path

    def persist(share_id: str, fields: dict) -> None:
        update_match_result(share_id, **fields)

    summary = run_sweep(
        shares,
        batch_fetch=_batch_fetch_edge(sync_spotify),
        search_batch=_search_batch_edge(),
        persist=persist,
        examples_limit=examples_limit,
    )

    log.info(
        f"Matching sweep complete: processed={summary['processed']} "
        f"matched={summary['matched']} unmatched={summary['unmatched']} "
        f"errors={summary['errors']}"
    )
    return summary


@handle_errors(HANDLER)
def handler(event: dict, context: Any) -> dict:
    """
    Cron/entry handler. `event` may carry `matchStatus` (default 'pending')
    to re-run over a different bucket. Returns the sweep summary directly
    (this is an invoked worker, not an API-Gateway route, so no response
    envelope is imposed).
    """
    match_status = (event or {}).get("matchStatus", "pending")
    return sweep_pending(match_status=match_status)


if __name__ == "__main__":
    import json

    result = sweep_pending()
    print(json.dumps(result, indent=2))
