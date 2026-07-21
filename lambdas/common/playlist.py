"""
XOMTRACKS Playlist Client (vendored)
=====================================
Vendored from xomify-backend's lambdas/common/playlist.py per
docs/features/xomtracks/PLAN.md's Approach section. Reuses
aiohttp_build_playlist (on-the-spot + first rolling build) and
aiohttp_update_playlist / aiohttp_replace_playlist_songs (atomic PUT-replace
for the weekly rolling refresh).

Trimmed to the aiohttp paths only -- xomtracks has no synchronous Lambda
call sites (everything runs under asyncio.run(), matching the cron/handler
pattern this repo uses throughout), so xomify's sync
create_playlist/add_playlist_songs/update_playlist/delete_playlist_songs/
add_playlist_image methods were dropped.

DEVIATION from xomify's copy (PLAN.md, locked decision): xomify's
create_playlist hardcodes `"public": True`. Xomtracks parameterizes it via
a `public` constructor arg, DEFAULTING to True -- every xomtracks playlist
(both rolling in/out + on-the-spot) stays public per the locked decision,
but the flag exists so it's not silently unchangeable.
"""

import asyncio
import aiohttp
from lambdas.common.logger import get_logger
from lambdas.common.aiohttp_helper import post_json, put_data, put_json

log = get_logger(__file__)


class Playlist:

    BASE_URL = "https://api.spotify.com/v1"

    def __init__(
        self,
        user_id: str,
        name: str,
        description: str,
        headers: dict,
        session: aiohttp.ClientSession = None,
        public: bool = True,
    ):
        log.info(f"Initializing Playlist '{name}' for user_id '{user_id}' (public={public}).")
        self.aiohttp_session = session
        self.user_id = user_id
        self.name = name
        self.description = description
        self.headers = headers
        self.public = public
        self.uri_list = None
        self.image = None
        self.playlist = None
        self.id = None

    # ------------------------
    # Shared Methods
    # ------------------------
    def set_id(self, id: str):
        self.id = id

    # ------------------------
    # Build / Update Flows
    # ------------------------
    async def aiohttp_build_playlist(self, uri_list: list, image: str = None):
        try:
            log.info(f"Building playlist (aiohttp): {self.name} with {len(uri_list)} tracks")
            self.uri_list = uri_list
            self.image = image
            await self.aiohttp_create_playlist()
            if self.image:
                await asyncio.sleep(2)
                await self.aiohttp_add_playlist_image()
            await asyncio.sleep(2)
            await self.aiohttp_add_playlist_songs()
            log.info(f"Playlist '{self.name}' Complete!")
        except Exception as err:
            log.error(f"AIOHTTP Build Playlist: {err}")
            raise Exception(f"AIOHTTP Build Playlist: {err}") from err

    async def aiohttp_update_playlist(self, uri_list: list):
        """Replace all tracks atomically via PUT (first 100) + POST (remainder)."""
        try:
            log.info(f"Updating playlist (aiohttp): {self.name} with {len(uri_list)} tracks")
            self.uri_list = uri_list
            await self.aiohttp_replace_playlist_songs()
            log.info(f"Playlist '{self.name}' Updated!")
        except Exception as err:
            log.error(f"AIOHTTP Update Playlist: {err}")
            raise Exception(f"AIOHTTP Update Playlist: {err}") from err

    # ------------------------
    # Create Playlist
    # ------------------------
    async def aiohttp_create_playlist(self):
        try:
            log.info("Creating playlist (aiohttp)..")
            url = f"{self.BASE_URL}/users/{self.user_id}/playlists"
            body = {"name": self.name, "description": self.description, "public": self.public}
            data = await post_json(self.aiohttp_session, url, headers=self.headers, json=body)
            self.playlist = data
            self.id = self.playlist['id']
            log.info(f"AIOHTTP Playlist Creation Complete. ID: {self.id}")
        except Exception as err:
            log.error(f"AIOHTTP Create Playlist: {err}")
            raise Exception(f"AIOHTTP Create Playlist: {err}") from err

    # ------------------------
    # Add Playlist Songs
    # ------------------------
    async def aiohttp_add_playlist_songs(self):
        try:
            if not self.uri_list or len(self.uri_list) == 0:
                log.info("No tracks to add this week. Skipping.")
                return

            log.info(f"Adding {len(self.uri_list)} songs to Playlist '{self.name}' (aiohttp)")
            batch_size = 100
            url = f"{self.BASE_URL}/playlists/{self.id}/tracks"

            for i in range(0, len(self.uri_list), batch_size):
                batch_uris = self.uri_list[i:i + batch_size]
                body = {"uris": batch_uris}
                await post_json(self.aiohttp_session, url, headers=self.headers, json=body)
                log.debug(f"AIOHTTP Added {len(batch_uris)} tracks (batch {i // batch_size + 1})")

            log.info(f"AIOHTTP All {len(self.uri_list)} tracks added successfully.")
        except Exception as err:
            log.error(f"AIOHTTP Add Playlist Songs: {err}")
            raise Exception(f"AIOHTTP Add Playlist Songs: {err}") from err

    # ------------------------
    # Replace Playlist Songs (atomic PUT + POST)
    # ------------------------
    async def aiohttp_replace_playlist_songs(self):
        """PUT first 100 URIs (replaces all tracks), then POST remaining batches."""
        try:
            if not self.uri_list or len(self.uri_list) == 0:
                log.info("No tracks to add. Clearing playlist.")
                url = f"{self.BASE_URL}/playlists/{self.id}/tracks"
                await put_json(self.aiohttp_session, url, headers=self.headers, json={"uris": []})
                return

            batch_size = 100
            url = f"{self.BASE_URL}/playlists/{self.id}/tracks"

            first_batch = self.uri_list[:batch_size]
            await put_json(self.aiohttp_session, url, headers=self.headers, json={"uris": first_batch})
            log.debug(f"AIOHTTP Replaced with {len(first_batch)} tracks (PUT)")

            for i in range(batch_size, len(self.uri_list), batch_size):
                batch_uris = self.uri_list[i:i + batch_size]
                await post_json(self.aiohttp_session, url, headers=self.headers, json={"uris": batch_uris})
                log.debug(f"AIOHTTP Added {len(batch_uris)} tracks (POST batch)")

            log.info(f"AIOHTTP All {len(self.uri_list)} tracks replaced successfully.")
        except Exception as err:
            log.error(f"AIOHTTP Replace Playlist Songs: {err}")
            raise Exception(f"AIOHTTP Replace Playlist Songs: {err}") from err

    # ------------------------
    # Add Playlist Image
    # ------------------------
    async def aiohttp_add_playlist_image(self, retried=False):
        try:
            log.info(f"Adding Image to Playlist {self.id} (aiohttp)...")
            url = f'{self.BASE_URL}/playlists/{self.id}/images'
            body = self.image.replace('\n', '')

            await put_data(self.aiohttp_session, url, data=body, headers=self.headers)
            log.info("AIOHTTP Image added to Playlist.")
        except Exception as err:
            log.error(f"AIOHTTP Add Playlist Image: {err}")
            if not retried:
                log.warning("Retrying image upload...")
                await asyncio.sleep(2)
                await self.aiohttp_add_playlist_image(True)
            else:
                raise Exception(f"AIOHTTP Add Playlist Image: {err}") from err
