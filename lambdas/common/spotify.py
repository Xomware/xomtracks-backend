"""
XOMTRACKS Spotify Client (vendored + trimmed)
==============================================
Vendored from xomify-backend's lambdas/common/spotify.py per
docs/features/xomtracks/PLAN.md's Approach section ("Vendor a trimmed
copy... clean app boundary with zero risk to the deployed xomify").

Trimmed to what xomtracks actually needs:
- aiohttp_get_access_token / get_access_token -- refresh-token OAuth flow
- _persist_rotated_refresh_token -- rotated-token persistence (xomtracks'
  OWN users table, not xomify's -- self-contained per Option 3)
- get_tracks_by_ids -- batch track hydrate (Spotify URL -> full track object)

Dropped from xomify's copy: aiohttp_initialize_wrapped,
aiohttp_initialize_top_items, aiohttp_initialize_release_radar (and their
TrackList/ArtistList/top-genre dependencies), get_playback_state,
get_recently_played, get_artists_by_ids, __get_last_month_data -- none of
that is wrapped/release-radar/top-items scope, which PLAN.md explicitly
says to drop.

NEW (not in xomify's copy -- genuinely new for cross-platform matching,
PLAN.md Phase 3): aiohttp_get_track (single-track hydrate for the
Spotify-URL resolver branch) and aiohttp_search_track (title+artist ->
Spotify /search, for the SoundCloud/Apple Music resolver branches).
groups_add_song_url in xomify hits these endpoints inline rather than via
a Spotify class method -- xomtracks needs both branches reusable from
matching.py, so they're proper methods here.
"""

import requests
import aiohttp
from lambdas.common import ssm_helpers
from lambdas.common.logger import get_logger

log = get_logger(__file__)


class Spotify:
    """
    Spotify API client for a single user (xomtracks' own users/token row --
    does not touch xomify's).
    """

    BASE_URL = "https://api.spotify.com/v1"

    def __init__(self, user: dict = None, session: aiohttp.ClientSession = None, app_only: bool = False):
        # app_only: NEW (xomtracks-only, not in xomify's copy) -- authenticate
        # via the client-credentials (app-token) flow instead of a user
        # refresh token. Valid ONLY for the public, read-only endpoints the
        # cross-platform matcher uses (GET /tracks, /search); it grants no
        # user scope (no playlist writes). Lets the matching sweep run with
        # just the /xomtracks/spotify/* app credentials -- no per-user
        # refresh token, no users-table row required.
        self.app_only = app_only
        user = user or {}
        log.info(f"Initializing Spotify Client for User {user.get('email', 'unknown')}.")
        # Accessed via the module object (not `from ... import NAME`) so the
        # SSM fetch stays genuinely lazy -- deferred to first Spotify()
        # construction, not Python-import time. `from module import NAME`
        # against ssm_helpers' PEP 562 __getattr__ resolves eagerly at
        # import time, which is what xomify-backend's copy of this file
        # does; that pattern makes the module untestable without live AWS
        # creds, so xomtracks' vendored copy deliberately does not repeat it.
        self.client_id: str = ssm_helpers.SPOTIFY_CLIENT_ID
        self.client_secret: str = ssm_helpers.SPOTIFY_CLIENT_SECRET
        self.aiohttp_session = session

        # User info
        self.user = user
        self.user_id: str = self.user.get('userId', '')
        self.email: str = self.user.get('email', '')
        self.refresh_token: str = self.user.get('refreshToken', '')

        # Auth - initialized later for async
        self.access_token: str = None
        self.headers: dict = {}

        # Initialize synchronously if no aiohttp session
        if not self.aiohttp_session:
            self.access_token = (
                self.get_app_access_token() if self.app_only else self.get_access_token()
            )
            self.headers = self._auth_headers(self.access_token)

    @staticmethod
    def _auth_headers(access_token: str) -> dict:
        return {
            'Authorization': f'Bearer {access_token}',
            'Content-Type': 'application/json',
        }

    # ------------------------
    # App-token auth (client-credentials) -- NEW, xomtracks-only.
    # ------------------------
    # Not in xomify's vendored copy: xomify always acts as a specific user
    # (playlist writes need user scope). xomtracks' matching sweep only
    # reads public catalog data (GET /tracks, /search), which the
    # client-credentials flow covers without any user refresh token.
    def get_app_access_token(self) -> str:
        """Get an app access token via client-credentials (synchronous)."""
        try:
            log.info("Getting spotify app access token (client-credentials)...")
            response = requests.post(
                "https://accounts.spotify.com/api/token",
                data={
                    'grant_type': 'client_credentials',
                    'client_id': self.client_id,
                    'client_secret': self.client_secret,
                },
            )
            response_data = response.json()
            if response.status_code != 200:
                raise Exception(f"Error getting app token: {response_data}")
            log.info("Successfully retrieved spotify app access token!")
            return response_data['access_token']
        except Exception as err:
            log.error(f"Get Spotify App Access Token: {err}")
            raise Exception(f"Get Spotify App Access Token: {err}") from err

    async def aiohttp_get_app_access_token(self) -> str:
        """Get an app access token via client-credentials (async)."""
        try:
            log.info("Getting spotify app access token (aiohttp, client-credentials)...")
            async with self.aiohttp_session.post(
                "https://accounts.spotify.com/api/token",
                data={
                    'grant_type': 'client_credentials',
                    'client_id': self.client_id,
                    'client_secret': self.client_secret,
                },
            ) as response:
                response_data = await response.json()
                if response.status != 200:
                    raise Exception(f"Error getting app token: {response_data}")
                log.info("Successfully retrieved spotify app access token!")
                return response_data['access_token']
        except Exception as err:
            log.error(f"AIOHTTP Get Spotify App Access Token: {err}")
            raise Exception(f"AIOHTTP Get Spotify App Access Token: {err}") from err

    async def aiohttp_initialize_app_token(self) -> None:
        """
        Fetch an app token and populate self.headers for the async
        endpoints (aiohttp_get_track / aiohttp_search_track). Call once
        after constructing with app_only=True and an aiohttp session.
        """
        self.access_token = await self.aiohttp_get_app_access_token()
        self.headers = self._auth_headers(self.access_token)

    async def aiohttp_initialize_user_token(self) -> None:
        """
        Fetch a USER access token (refresh-token flow) and populate
        self.headers for the async playlist endpoints. Call once after
        constructing with a real user row + an aiohttp session (mirrors
        aiohttp_initialize_app_token, but for the user-scoped flow the
        playlist writes + cover upload require). Also persists a rotated
        refresh token when Spotify returns one (see aiohttp_get_access_token).
        """
        self.access_token = await self.aiohttp_get_access_token()
        self.headers = self._auth_headers(self.access_token)

    def get_access_token(self) -> str:
        """Get access token using refresh token (synchronous)."""
        try:
            log.info("Getting spotify access token...")
            url = "https://accounts.spotify.com/api/token"

            data = {
                'grant_type': 'refresh_token',
                'refresh_token': self.refresh_token,
                'client_id': self.client_id,
                'client_secret': self.client_secret,
            }

            response = requests.post(url, data=data)
            response_data = response.json()

            if response.status_code != 200:
                raise Exception(f"Error refreshing token: {response_data}")

            log.info("Successfully retrieved spotify access token!")
            return response_data['access_token']

        except Exception as err:
            log.error(f"Get Spotify Access Token: {err}")
            raise Exception(f"Get Spotify Access Token: {err}") from err

    async def aiohttp_get_access_token(self) -> str:
        """Get access token using refresh token (async).

        Persists a rotated refresh_token back to DynamoDB when Spotify
        returns one -- prevents token-revocation drift.
        """
        try:
            log.info("Getting spotify access token (aiohttp)...")
            url = "https://accounts.spotify.com/api/token"

            data = {
                'grant_type': 'refresh_token',
                'refresh_token': self.refresh_token,
                'client_id': self.client_id,
                'client_secret': self.client_secret,
            }

            async with self.aiohttp_session.post(url, data=data) as response:
                response_data = await response.json()

                if response.status != 200:
                    raise Exception(f"Error refreshing token: {response_data}")

                new_refresh = response_data.get('refresh_token')
                if new_refresh and new_refresh != self.refresh_token:
                    log.info("Spotify rotated refresh token — persisting new token")
                    self.refresh_token = new_refresh
                    self._persist_rotated_refresh_token(new_refresh)

                log.info("Successfully retrieved spotify access token!")
                return response_data['access_token']

        except Exception as err:
            log.error(f"AIOHTTP Get Spotify Access Token: {err}")
            raise Exception(f"AIOHTTP Get Spotify Access Token: {err}") from err

    def _persist_rotated_refresh_token(self, new_token: str):
        """Save a rotated refresh token back to xomtracks' OWN users table."""
        try:
            from lambdas.common.dynamo_helpers import update_table_item_field
            from lambdas.common.constants import USERS_TABLE_NAME
            update_table_item_field(
                USERS_TABLE_NAME, 'email', self.email,
                'refreshToken', new_token
            )
        except Exception as err:
            log.warning(f"Failed to persist rotated refresh token: {err}")

    # ------------------------
    # Batch track hydrate
    # ------------------------
    # Spotify caps the batch endpoint at 50 tracks per request.
    _BATCH_LIMIT = 50

    def get_tracks_by_ids(self, track_ids: list) -> list:
        """
        Batch-hydrate full track objects from bare Spotify track IDs.

        Uses `GET /v1/tracks?ids=` (synchronous, via the sync `headers` set
        up in `__init__`/by the caller). Spotify caps the call at 50 ids,
        so we chunk transparently and concatenate. Missing/None entries in
        Spotify's response are dropped so callers always get real objects.
        """
        clean_ids = [i for i in (track_ids or []) if i]
        if not clean_ids:
            return []

        tracks: list = []
        for start in range(0, len(clean_ids), self._BATCH_LIMIT):
            chunk = clean_ids[start:start + self._BATCH_LIMIT]
            url = f"{self.BASE_URL}/tracks?ids={','.join(chunk)}"

            response = requests.get(url, headers=self.headers)
            if response.status_code != 200:
                raise Exception(f"Batch get tracks failed ({response.status_code}): {response.text}")

            data = response.json()
            tracks.extend([t for t in (data.get('tracks') or []) if t])

        return tracks

    # ------------------------
    # Single-track hydrate (Spotify-URL resolver branch, matching.py)
    # ------------------------
    async def aiohttp_get_track(self, track_id: str) -> dict | None:
        """
        Fetch a single track by id. Returns None on 404 (bad/removed
        track id) rather than raising -- callers treat that as
        `matchStatus=unmatched`, not a hard failure.
        """
        url = f"{self.BASE_URL}/tracks/{track_id}"
        async with self.aiohttp_session.get(url, headers=self.headers) as response:
            if response.status == 404:
                log.warning(f"Track not found: {track_id}")
                return None
            if response.status != 200:
                text = await response.text()
                raise Exception(f"Get track failed ({response.status}): {text}")
            return await response.json()

    # ------------------------
    # Search (SoundCloud/Apple-Music resolver branches, matching.py)
    # ------------------------
    async def aiohttp_search_track(self, query: str, limit: int = 5) -> list:
        """
        `GET /v1/search?type=track` -- used by the cross-platform matcher
        to resolve a SoundCloud/Apple Music title+artist to Spotify search
        candidates for fuzzy-match scoring.
        """
        import urllib.parse
        encoded = urllib.parse.quote(query)
        url = f"{self.BASE_URL}/search?q={encoded}&type=track&limit={limit}"

        async with self.aiohttp_session.get(url, headers=self.headers) as response:
            if response.status != 200:
                text = await response.text()
                raise Exception(f"Search track failed ({response.status}): {text}")
            data = await response.json()
            return (data.get('tracks') or {}).get('items') or []
