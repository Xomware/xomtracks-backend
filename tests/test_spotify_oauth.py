"""
RED-before-GREEN: lambdas/common/spotify_oauth.py -- the per-user Spotify
Authorization-Code flow (Phase 2). Pure URL build + code exchange + /me id
resolve, with the SSM app creds pre-seeded by conftest and `requests` patched.
"""

from unittest.mock import patch
from urllib.parse import parse_qs, urlparse

import pytest

from lambdas.common import spotify_oauth
from lambdas.common.errors import AuthorizationError, SpotifyAPIError


class TestBuildAuthorizeUrl:
    def test_carries_client_id_scopes_state_redirect(self):
        url = spotify_oauth.build_authorize_url("st4te", "https://xomtracks.xomware.com/callback")
        parsed = urlparse(url)
        q = parse_qs(parsed.query)

        assert parsed.netloc == "accounts.spotify.com"
        assert q["response_type"] == ["code"]
        assert q["state"] == ["st4te"]
        assert q["redirect_uri"] == ["https://xomtracks.xomware.com/callback"]
        assert q["client_id"] == ["test-spotify-client-id"]
        # all four required scopes present, space-delimited
        scope = q["scope"][0]
        for s in ("playlist-modify-public", "playlist-modify-private",
                  "ugc-image-upload", "user-read-recently-played"):
            assert s in scope


class TestExchangeCode:
    @patch("lambdas.common.spotify_oauth.requests.post")
    def test_returns_token_payload(self, mock_post):
        mock_post.return_value.status_code = 200
        mock_post.return_value.json.return_value = {
            "access_token": "AT", "refresh_token": "RT", "scope": "playlist-modify-public",
        }
        payload = spotify_oauth.exchange_code("code123", "https://xomtracks.xomware.com/callback")
        assert payload["refresh_token"] == "RT"
        # sent as a confidential client (client_secret in the POST body)
        sent = mock_post.call_args.kwargs["data"]
        assert sent["grant_type"] == "authorization_code"
        assert sent["client_secret"] == "test-spotify-client-secret"

    @patch("lambdas.common.spotify_oauth.requests.post")
    def test_bad_code_is_401(self, mock_post):
        mock_post.return_value.status_code = 400
        mock_post.return_value.json.return_value = {"error": "invalid_grant"}
        with pytest.raises(AuthorizationError):
            spotify_oauth.exchange_code("bad", "https://xomtracks.xomware.com/callback")

    @patch("lambdas.common.spotify_oauth.requests.post")
    def test_missing_refresh_token_is_502(self, mock_post):
        mock_post.return_value.status_code = 200
        mock_post.return_value.json.return_value = {"access_token": "AT"}  # no refresh_token
        with pytest.raises(SpotifyAPIError):
            spotify_oauth.exchange_code("code", "https://xomtracks.xomware.com/callback")


class TestFetchSpotifyUserId:
    @patch("lambdas.common.spotify_oauth.requests.get")
    def test_returns_id(self, mock_get):
        mock_get.return_value.status_code = 200
        mock_get.return_value.json.return_value = {"id": "spotify-uid", "display_name": "Dom"}
        assert spotify_oauth.fetch_spotify_user_id("AT") == "spotify-uid"

    @patch("lambdas.common.spotify_oauth.requests.get")
    def test_non_200_is_502(self, mock_get):
        mock_get.return_value.status_code = 403
        with pytest.raises(SpotifyAPIError):
            spotify_oauth.fetch_spotify_user_id("AT")
