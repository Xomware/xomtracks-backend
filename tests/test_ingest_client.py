"""
RED-before-GREEN: extractor/ingest_client.py -- pushes share dicts to
POST /shares/ingest with the SSM-scoped bearer key.
"""

import pytest


class FakeResponse:
    def __init__(self, status_code=200, json_data=None):
        self.status_code = status_code
        self._json_data = json_data or {}

    def json(self):
        return self._json_data


class TestPushShare:
    def test_sends_bearer_header_and_json_body(self):
        from extractor.ingest_client import push_share

        calls = []

        def fake_post(url, json=None, headers=None, timeout=None):
            calls.append((url, json, headers, timeout))
            return FakeResponse(200, {"data": {"created": True}})

        share = {"messageGuid": "g1", "sourceUrl": "https://open.spotify.com/track/aaa"}
        ok = push_share(share, "https://api.example.com/shares/ingest", "secret-key", http_post=fake_post)

        assert ok is True
        url, json_body, headers, timeout = calls[0]
        assert url == "https://api.example.com/shares/ingest"
        assert json_body == share
        assert headers["Authorization"] == "Bearer secret-key"

    def test_non_2xx_response_returns_false(self):
        from extractor.ingest_client import push_share

        def fake_post(url, json=None, headers=None, timeout=None):
            return FakeResponse(500, {"error": {"message": "boom"}})

        ok = push_share({}, "https://api.example.com/shares/ingest", "key", http_post=fake_post)
        assert ok is False

    def test_network_exception_returns_false(self):
        from extractor.ingest_client import push_share

        def fake_post(url, json=None, headers=None, timeout=None):
            raise ConnectionError("host asleep / no network")

        ok = push_share({}, "https://api.example.com/shares/ingest", "key", http_post=fake_post)
        assert ok is False

    def test_already_exists_is_still_success(self):
        """Idempotent re-ingest (created=False) is a successful push, not a failure."""
        from extractor.ingest_client import push_share

        def fake_post(url, json=None, headers=None, timeout=None):
            return FakeResponse(200, {"data": {"created": False}})

        ok = push_share({}, "https://api.example.com/shares/ingest", "key", http_post=fake_post)
        assert ok is True
