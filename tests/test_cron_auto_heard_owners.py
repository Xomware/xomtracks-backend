"""
RED-before-GREEN: auto-heard cron with OWNER SCOPING ON (Phase 2).

Verifies per-owner iteration: each connected owner's recently-played is fetched
via THEIR token and marked heard keyed to THEIR Cognito email; Dom (not
connected) falls back to the service account keyed to AUTO_HEARD_RATER_EMAIL.
"""

import lambdas.cron_auto_heard.handler as H
from lambdas.common.constants import AUTO_HEARD_RATER_EMAIL, DEFAULT_OWNER_ID


class _FakeSpotify:
    def __init__(self, items):
        self._items = items

    async def aiohttp_get_recently_played(self, limit=50):
        return self._items


def _item(track_id):
    return {"track": {"id": track_id}, "played_at": "2026-07-20T12:00:00Z"}


def test_iterates_connected_owner_and_service_fallback(monkeypatch):
    monkeypatch.setattr(H, "OWNER_SCOPING_ENABLED", True)
    connected = [{"ownerId": "sub-a", "email": "a@example.com", "refreshToken": "RT"}]
    monkeypatch.setattr(H, "list_spotify_connected_users", lambda: connected)

    # different recently-played per owner so we can assert routing by rater
    per_owner_items = {
        "sub-a": [_item("aaa")],
        DEFAULT_OWNER_ID: [_item("ddd")],
    }

    async def build(session, owner_id):
        return _FakeSpotify(per_owner_items[owner_id]), "uid", owner_id == DEFAULT_OWNER_ID

    monkeypatch.setattr(H, "build_owner_client", build)

    persisted: list = []
    monkeypatch.setattr(H, "set_heard", lambda tk, email, heard, heard_at=None: persisted.append((tk, email)))

    summary = H.auto_mark_heard()

    assert summary["marked"] == 2
    routed = dict(persisted)
    # connected owner's track keyed to THEIR email; Dom's keyed to the rater email
    assert routed["spotify:aaa"] == "a@example.com"
    assert routed["spotify:ddd"] == AUTO_HEARD_RATER_EMAIL


def test_scoping_off_single_service_owner(monkeypatch):
    monkeypatch.setattr(H, "OWNER_SCOPING_ENABLED", False)

    async def build(session, owner_id):
        assert owner_id == DEFAULT_OWNER_ID
        return _FakeSpotify([_item("ddd")]), "uid", True

    monkeypatch.setattr(H, "build_owner_client", build)
    persisted: list = []
    monkeypatch.setattr(H, "set_heard", lambda tk, email, heard, heard_at=None: persisted.append((tk, email)))

    summary = H.auto_mark_heard()
    assert summary["marked"] == 1
    assert persisted == [("spotify:ddd", AUTO_HEARD_RATER_EMAIL)]
