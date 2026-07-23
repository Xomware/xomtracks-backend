"""
RED-before-GREEN: token-keepalive cron.

Happy path: refresh + /me probe succeed -> {ok: True, spotifyUserId}.
Failure path: /me probe fails -> handler surfaces a non-2xx envelope
(so CloudWatch alarms on the cron error metric).
"""

import lambdas.cron_token_keepalive.handler as H


class _FakeSpotify:
    BASE_URL = "https://api.spotify.com/v1"

    def __init__(self, user, session):
        self.headers = {}

    async def aiohttp_initialize_user_token(self):
        self.headers = {"Authorization": "Bearer AT"}


def _wire(monkeypatch):
    monkeypatch.setattr(H, "get_app_service_user", lambda: {"email": "app@x", "userId": "user1"})
    monkeypatch.setattr(H, "Spotify", _FakeSpotify)


class TestKeepalive:
    def test_ok(self, monkeypatch):
        _wire(monkeypatch)

        async def fake_probe(session, spotify):
            assert spotify.headers["Authorization"] == "Bearer AT"
            return {"id": "user1"}

        monkeypatch.setattr(H, "_probe", fake_probe)

        result = H.keepalive()
        assert result["ok"] is True
        assert result["spotifyUserId"] == "user1"
        assert result["email"] == "app@x"

    def test_probe_failure_returns_error_envelope(self, monkeypatch, mock_context):
        _wire(monkeypatch)

        async def fake_probe(session, spotify):
            from lambdas.common.errors import SpotifyAPIError
            raise SpotifyAPIError(message="401 unauthorized", handler=H.HANDLER, function="_probe", endpoint="/me")

        monkeypatch.setattr(H, "_probe", fake_probe)

        resp = H.handler({}, mock_context)
        # SpotifyAPIError -> 502, surfaced (not swallowed) so the cron alarms.
        assert resp["statusCode"] == 502
