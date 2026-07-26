"""
XOMTRACKS Playlist Service (shared orchestration)
=================================================
The build/update glue shared by the rolling-playlists cron and the
on-the-spot create endpoint. Wraps the vendored Playlist client with:

- service-account auth (single Spotify-connected account -- Dom's row in
  xomtracks-users, per PLAN.md's single-service-account model),
- ordered-unique URI collection (dedup preserving newest-first order --
  NOT release-radar's order-losing set()),
- create-or-replace-in-place upsert with the xomify-branded cover.

Every playlist Xomtracks builds is PUBLIC on Dom's profile (locked
decision -- Playlist defaults public=True).
"""

import aiohttp

from lambdas.common.constants import PLAYLIST_ID_UNSET
from lambdas.common.dynamo_helpers import (
    get_app_service_user,
    get_spotify_user_by_owner,
)
from lambdas.common.logger import get_logger
from lambdas.common.covers import XOMIFY_COVER_BASE_64
from lambdas.common.playlist import Playlist
from lambdas.common.spotify import Spotify

log = get_logger(__file__)


def playlist_url(playlist_id: str) -> str:
    """Public Spotify web URL for a playlist id."""
    return f"https://open.spotify.com/playlist/{playlist_id}"


def ordered_unique_uris(shares: list[dict]) -> list[str]:
    """
    Collect resolvedSpotifyUri from an ALREADY-ordered share list, deduping
    while preserving order (first occurrence wins). Shares without a
    resolved URI (pending/unmatched) are skipped -- they have nothing to add.
    """
    seen: set[str] = set()
    out: list[str] = []
    for share in shares:
        uri = share.get("resolvedSpotifyUri")
        if uri and uri not in seen:
            seen.add(uri)
            out.append(uri)
    return out


async def build_service_client(session: aiohttp.ClientSession) -> tuple[Spotify, str]:
    """
    Construct the app's Spotify client for its single service-account row
    and initialize a user access token (refresh flow, persists rotation).

    Returns:
        (spotify, user_id) -- user_id owns every playlist we create.

    Raises:
        NotFoundError: xomtracks-users has no service row yet (needs seeding).
    """
    user = get_app_service_user()
    spotify = Spotify(user, session)
    await spotify.aiohttp_initialize_user_token()
    return spotify, user.get("userId", "")


def _user_id_from_row(row: dict) -> str:
    """The Spotify account id a connected row builds playlists under. Prefers
    `userId` (what the vendored clients read) then the explicit spotifyUserId."""
    return row.get("userId") or row.get("spotifyUserId") or ""


async def _client_from_row(session: aiohttp.ClientSession, row: dict) -> tuple[Spotify, str]:
    spotify = Spotify(row, session)
    await spotify.aiohttp_initialize_user_token()
    return spotify, _user_id_from_row(row)


async def build_owner_client(
    session: aiohttp.ClientSession, owner_id: str
) -> tuple[Spotify, str, bool]:
    """
    Build a Spotify client for a given ownerId (Cognito sub) -- Phase 2.

    Uses the owner's OWN connected row (their OAuth refresh token) when they've
    connected; otherwise falls back to the shared service account (Dom's seeded
    token) so an un-connected owner -- including Dom before he re-connects --
    keeps working exactly as today.

    Returns (spotify, user_id, is_service_fallback). `is_service_fallback` lets
    callers pick the right playlist-id store (per-row vs the legacy SSM params).
    """
    row = get_spotify_user_by_owner(owner_id)
    if row and row.get("refreshToken"):
        spotify, user_id = await _client_from_row(session, row)
        return spotify, user_id, False
    spotify, user_id = await build_service_client(session)
    return spotify, user_id, True


async def build_client_by_email(
    session: aiohttp.ClientSession, email: str
) -> tuple[Spotify, str, bool]:
    """
    Build a Spotify client for a caller identified by Cognito email (the
    /playlists/create path -- the caller's identity is their email at request
    time). Uses the caller's connected row if present, else the service account.

    Returns (spotify, user_id, is_service_fallback).
    """
    from lambdas.common.user_links import get_user_record

    row = get_user_record(email)
    if row and row.get("refreshToken"):
        spotify, user_id = await _client_from_row(session, row)
        return spotify, user_id, False
    spotify, user_id = await build_service_client(session)
    return spotify, user_id, True


async def _reassert_cover(playlist: Playlist, image: str | None) -> None:
    """
    Best-effort cover re-upload on the update path. A failed cover re-assert
    (Spotify image uploads are heavily rate-limited) must NEVER fail the run
    or bubble up -- the cover survives a track PUT-replace anyway, so this is
    cosmetic insurance, not load-bearing. Swallowing it here is what stops a
    transient image 429 from being mistaken for a broken playlist upstream.
    """
    if not image:
        return
    try:
        # aiohttp_add_playlist_image reads self.image (only set by build);
        # assign it explicitly for the update path.
        playlist.image = image
        await playlist.aiohttp_add_playlist_image()
    except Exception as err:  # noqa: BLE001 -- cosmetic, deliberately non-fatal
        log.warning(f"Cover re-assert failed for playlist {playlist.id} (non-fatal): {err}")


async def playlist_exists(session: aiohttp.ClientSession, spotify: Spotify, playlist_id: str) -> bool:
    """
    True if the playlist still exists/is reachable (GET /playlists/{id} 200),
    False on 404. Used to gate recreate-on-failure so a transient update
    error never spawns a DUPLICATE playlist for one that's actually fine.
    """
    url = f"{spotify.BASE_URL}/playlists/{playlist_id}?fields=id"
    async with session.get(url, headers=spotify.headers) as resp:
        if resp.status == 200:
            return True
        if resp.status == 404:
            return False
        # Unknown status -- assume it exists (fail safe: never duplicate).
        log.warning(f"playlist_exists({playlist_id}) got status {resp.status}; assuming it exists")
        return True


async def upsert_playlist(
    session: aiohttp.ClientSession,
    spotify: Spotify,
    user_id: str,
    *,
    playlist_id: str | None,
    name: str,
    description: str,
    uris: list[str],
    image: str | None = XOMIFY_COVER_BASE_64,
) -> str:
    """
    Create a new public playlist (cover + tracks) when playlist_id is unset,
    else atomically PUT-replace the existing playlist's tracks in place and
    re-assert the cover (best-effort -- see _reassert_cover).

    Returns the playlist id (new one on create, the same one on update). The
    track replace is the only load-bearing call on the update path; the cover
    re-assert is deliberately non-fatal so it can never trigger a recreate.
    """
    playlist = Playlist(user_id, name, description, spotify.headers, session, public=True)

    is_existing = bool(playlist_id) and playlist_id != PLAYLIST_ID_UNSET
    if is_existing:
        log.info(f"Updating existing playlist {playlist_id} ({name}) with {len(uris)} track(s)")
        playlist.set_id(playlist_id)
        await playlist.aiohttp_update_playlist(uris)
        await _reassert_cover(playlist, image)
        return playlist_id

    log.info(f"Creating new playlist ({name}) with {len(uris)} track(s)")
    await playlist.aiohttp_build_playlist(uris, image=image)
    return playlist.id


async def create_playlist(
    session: aiohttp.ClientSession,
    name: str,
    description: str,
    uris: list[str],
    *,
    owner_email: str | None = None,
    image: str | None = XOMIFY_COVER_BASE_64,
) -> str:
    """
    One-shot: build a client for the CALLER (their own Spotify if connected,
    else the service account fallback) and create a fresh public playlist
    (on-the-spot endpoint path). Returns the new playlist id.

    Phase 2: when `owner_email` is given the playlist is created on that caller's
    connected account; otherwise (or if they haven't connected) it falls back to
    the shared service account -- preserving the pre-OAuth behavior.
    """
    if owner_email:
        spotify, user_id, _is_fallback = await build_client_by_email(session, owner_email)
    else:
        spotify, user_id = await build_service_client(session)
    return await upsert_playlist(
        session,
        spotify,
        user_id,
        playlist_id=None,
        name=name,
        description=description,
        uris=uris,
        image=image,
    )
