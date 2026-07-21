"""
RED-before-GREEN: POST /auth/login -- mint per-user Xomtracks JWT from a
Spotify access token. Ported from xomify-backend's auth_login/handler.py.
Public route (no authorizer) -- verifies against Spotify's /me.
"""

import json
from unittest.mock import patch

import pytest


class TestAuthLoginHandler:
    def test_missing_token_returns_400(self, public_event, mock_context):
        from lambdas.auth_login.handler import handler

        event = public_event(httpMethod="POST", body=json.dumps({}))
        response = handler(event, mock_context)

        assert response["statusCode"] == 400

    @patch("lambdas.auth_login.handler.requests.get")
    def test_valid_spotify_token_mints_jwt(self, mock_get, public_event, mock_context):
        from lambdas.auth_login.handler import handler

        mock_get.return_value.status_code = 200
        mock_get.return_value.json.return_value = {"email": "dom@example.com", "id": "spotify-user-1"}

        event = public_event(httpMethod="POST", body=json.dumps({"spotifyAccessToken": "AT1"}))
        response = handler(event, mock_context)

        assert response["statusCode"] == 200
        body = json.loads(response["body"])
        assert "token" in body["data"]
        assert "expiresAt" in body["data"]

    @patch("lambdas.auth_login.handler.requests.get")
    def test_spotify_rejects_token_returns_401(self, mock_get, public_event, mock_context):
        from lambdas.auth_login.handler import handler

        mock_get.return_value.status_code = 401
        mock_get.return_value.json.return_value = {"error": "invalid token"}

        event = public_event(httpMethod="POST", body=json.dumps({"spotifyAccessToken": "bad"}))
        response = handler(event, mock_context)

        assert response["statusCode"] == 401

    @patch("lambdas.auth_login.handler.requests.get")
    def test_missing_email_in_spotify_me_is_502(self, mock_get, public_event, mock_context):
        from lambdas.auth_login.handler import handler

        mock_get.return_value.status_code = 200
        mock_get.return_value.json.return_value = {"id": "spotify-user-1"}  # no email

        event = public_event(httpMethod="POST", body=json.dumps({"spotifyAccessToken": "AT1"}))
        response = handler(event, mock_context)

        assert response["statusCode"] == 502
