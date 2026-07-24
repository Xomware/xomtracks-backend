"""
RED-before-GREEN: rolling-playlists cron with OWNER SCOPING ON (Phase 2).

Verifies per-owner iteration: a connected user builds playlists on THEIR token
from THEIR owner-scoped shares, with ids persisted to THEIR user row; Dom (not
connected) is served by the service account with ids in SSM -- so his path is
untouched. Network/SSM/Dynamo edges are patched.
"""

import lambdas.cron_rolling_playlists.handler as H
from lambdas.common.constants import DEFAULT_OWNER_ID


class _FakeSpotify:
    headers = {"Authorization": "Bearer AT"}


def _wire(monkeypatch, connected):
    monkeypatch.setattr(H, "OWNER_SCOPING_ENABLED", True)
    monkeypatch.setattr(H, "list_spotify_connected_users", lambda: connected)

    async def build(session, owner_id):
        # connected owners are not the service account; DEFAULT is the fallback
        is_service = owner_id == DEFAULT_OWNER_ID and owner_id not in {r["ownerId"] for r in connected}
        return _FakeSpotify(), f"uid-{owner_id}", is_service

    monkeypatch.setattr(H, "build_owner_client", build)

    # each owner sees exactly one matched share in each direction, tagged by owner
    monkeypatch.setattr(
        H, "query_shares_by_owner_direction",
        lambda owner_id, direction, since: [
            {"matchStatus": "matched", "resolvedSpotifyUri": f"spotify:track:{owner_id}-{direction}", "messageDate": 1}
        ],
    )

    row_writes: list = []
    monkeypatch.setattr(
        H, "update_table_item_field",
        lambda table, key, email, attr, val: row_writes.append((email, attr, val)),
    )
    ssm_writes: list = []
    monkeypatch.setattr(H, "put_ssm_param", lambda name, val: ssm_writes.append((name, val)))
    # connected owner rows have no rolling ids yet; service owner reads "unset"
    monkeypatch.setattr(H, "get_ssm_param", lambda name: "unset")

    async def fake_upsert(session, spotify, user_id, *, playlist_id, name, description, uris, image=None):
        return f"plid-{user_id}-{'in' if 'With Me' in name else 'out'}"

    monkeypatch.setattr(H, "upsert_playlist", fake_upsert)
    return row_writes, ssm_writes


def test_connected_user_and_service_fallback_both_built(monkeypatch):
    connected = [{"ownerId": "sub-a", "email": "a@example.com", "refreshToken": "RT", "userId": "sp-a"}]
    row_writes, ssm_writes = _wire(monkeypatch, connected)

    result = H.rebuild_rolling_playlists()
    owners = {o["ownerId"]: o for o in result["owners"]}

    # both the connected user AND Dom (service fallback) were processed
    assert set(owners) == {"sub-a", DEFAULT_OWNER_ID}
    assert owners["sub-a"]["serviceFallback"] is False
    assert owners[DEFAULT_OWNER_ID]["serviceFallback"] is True

    # connected owner's shares were owner-scoped (uri carries its ownerId)
    assert owners["sub-a"]["playlists"]["in"]["trackCount"] == 1

    # connected owner's ids persisted to their USER ROW (not SSM)
    a_writes = [w for w in row_writes if w[0] == "a@example.com"]
    assert {attr for _e, attr, _v in a_writes} == {"rollingInPlaylistId", "rollingOutPlaylistId"}

    # the service/default owner's ids persisted to SSM
    assert len(ssm_writes) == 2


def test_scoping_off_only_service_owner(monkeypatch):
    # default (false in test env) -> single service owner, legacy direction query
    monkeypatch.setattr(H, "OWNER_SCOPING_ENABLED", False)
    monkeypatch.setattr(
        H, "query_shares_by_direction",
        lambda direction, since: [
            {"matchStatus": "matched", "resolvedSpotifyUri": f"spotify:track:{direction}", "messageDate": 1}
        ],
    )

    async def build(session, owner_id):
        return _FakeSpotify(), "uid", True

    monkeypatch.setattr(H, "build_owner_client", build)
    monkeypatch.setattr(H, "get_ssm_param", lambda name: "unset")
    monkeypatch.setattr(H, "put_ssm_param", lambda name, val: None)

    async def fake_upsert(session, spotify, user_id, *, playlist_id, name, description, uris, image=None):
        return "plid"

    monkeypatch.setattr(H, "upsert_playlist", fake_upsert)

    result = H.rebuild_rolling_playlists()
    assert len(result["owners"]) == 1
    assert result["owners"][0]["ownerId"] == DEFAULT_OWNER_ID
