"""
RED-before-GREEN: extractor/run.py -- the end-to-end orchestration
(run_once). Integration-level: real fixture chat.db + fake push_share (no
real network), covering the properties PLAN.md's Test Strategy calls out
explicitly:
- finds links across ALL threads (no thread filter)
- watermark incrementality + dedup (re-run yields zero new pushes)
- backfill: a message inserted LATER (higher ROWID) with an OLDER date is
  still picked up -- proves ROWID, not date, drives the watermark
- a failed push does not silently skip a message (no data loss on retry)
"""

import sqlite3

import pytest

from tests.chat_db_fixture import create_fixture_db, insert_chat, insert_handle, insert_message


@pytest.fixture
def fixture_db_path(tmp_path):
    path = str(tmp_path / "chat.db")
    conn = create_fixture_db(path)
    conn.close()
    return path


@pytest.fixture
def state_path(tmp_path):
    return str(tmp_path / "state.json")


def _fake_push_recorder():
    calls = []

    def fake_push(share, ingest_url, bearer_key, **kwargs):
        calls.append(share)
        return True

    return fake_push, calls


class TestRunOnceBasics:
    def test_finds_links_across_multiple_conversations(self, fixture_db_path, state_path):
        from extractor.run import run_once

        conn = sqlite3.connect(fixture_db_path)
        chat_a = insert_chat(conn, "guid-a", "thread-a")
        chat_b = insert_chat(conn, "guid-b", "group-thread-b")
        h1 = insert_handle(conn, "+13364042196")
        insert_message(conn, chat_a, "g1", "https://open.spotify.com/track/aaa", 0, 1753000000, h1)
        insert_message(conn, chat_b, "g2", "https://soundcloud.com/x/bbb", 1, 1753000100)
        insert_message(conn, chat_a, "g3", "no link here", 0, 1753000200, h1)
        conn.close()

        fake_push, calls = _fake_push_recorder()
        stats = run_once(fixture_db_path, state_path, "https://api.example.com/shares/ingest", "key", push_share=fake_push)

        assert stats["scanned"] == 3
        assert stats["shares_found"] == 2
        assert stats["shares_pushed"] == 2
        assert stats["failed"] is False
        assert {c["messageGuid"] for c in calls} == {"g1", "g2"}

    def test_watermark_advances_and_persists(self, fixture_db_path, state_path):
        from extractor.run import run_once
        from extractor.watermark import load_watermark

        conn = sqlite3.connect(fixture_db_path)
        chat_a = insert_chat(conn, "guid-a", "thread-a")
        r3 = insert_message(conn, chat_a, "g3", "https://open.spotify.com/track/aaa", 0, 1753000000)
        conn.close()

        fake_push, _ = _fake_push_recorder()
        run_once(fixture_db_path, state_path, "url", "key", push_share=fake_push)

        assert load_watermark(state_path) == r3


class TestRerunDedup:
    def test_rerun_with_no_new_messages_pushes_nothing(self, fixture_db_path, state_path):
        from extractor.run import run_once

        conn = sqlite3.connect(fixture_db_path)
        chat_a = insert_chat(conn, "guid-a", "thread-a")
        insert_message(conn, chat_a, "g1", "https://open.spotify.com/track/aaa", 0, 1753000000)
        conn.close()

        fake_push, calls = _fake_push_recorder()
        run_once(fixture_db_path, state_path, "url", "key", push_share=fake_push)
        assert len(calls) == 1

        calls.clear()
        stats2 = run_once(fixture_db_path, state_path, "url", "key", push_share=fake_push)
        assert stats2["scanned"] == 0
        assert stats2["shares_pushed"] == 0
        assert calls == []


class TestBackfillPickedUpByRowidNotDate:
    def test_new_rowid_older_date_is_still_scanned(self, fixture_db_path, state_path):
        """
        Simulates iCloud Messages-in-iCloud backfill: a message that
        actually happened LONG AGO gets inserted into chat.db LATER (once
        history syncs down), so it gets a fresh, HIGH ROWID despite an OLD
        `date`. A date-based watermark would never see it (its date is
        already "in the past" relative to what's been processed); an
        ROWID-based watermark picks it up automatically on the next scan.
        """
        from extractor.run import run_once

        conn = sqlite3.connect(fixture_db_path)
        chat_a = insert_chat(conn, "guid-a", "thread-a")
        insert_message(conn, chat_a, "recent", "https://open.spotify.com/track/recent", 0, 1753000000)
        conn.close()

        fake_push, calls = _fake_push_recorder()
        run_once(fixture_db_path, state_path, "url", "key", push_share=fake_push)
        assert {c["messageGuid"] for c in calls} == {"recent"}

        # "Backfill": a message with an OLD date (long before the one
        # already processed) arrives NOW, getting a new high ROWID.
        conn = sqlite3.connect(fixture_db_path)
        insert_message(conn, chat_a, "ancient-backfilled", "https://open.spotify.com/track/ancient", 0, 1_600_000_000)
        conn.close()

        calls.clear()
        run_once(fixture_db_path, state_path, "url", "key", push_share=fake_push)
        assert {c["messageGuid"] for c in calls} == {"ancient-backfilled"}


class TestPushFailureDoesNotSkipMessages:
    def test_failed_push_halts_watermark_at_last_success(self, fixture_db_path, state_path):
        from extractor.run import run_once
        from extractor.watermark import load_watermark

        conn = sqlite3.connect(fixture_db_path)
        chat_a = insert_chat(conn, "guid-a", "thread-a")
        r1 = insert_message(conn, chat_a, "g1", "https://open.spotify.com/track/aaa", 0, 1753000000)
        r2 = insert_message(conn, chat_a, "g2", "https://open.spotify.com/track/bbb", 0, 1753000100)
        conn.close()

        def flaky_push(share, ingest_url, bearer_key, **kwargs):
            return share["messageGuid"] != "g2"  # g2's push fails

        stats = run_once(fixture_db_path, state_path, "url", "key", push_share=flaky_push)

        assert stats["failed"] is True
        # Watermark stops at g1 (last fully-successful message), NOT g2 --
        # so a retry re-attempts g2 rather than silently losing it.
        assert load_watermark(state_path) == r1

    def test_retry_after_fix_picks_up_the_failed_message(self, fixture_db_path, state_path):
        from extractor.run import run_once

        conn = sqlite3.connect(fixture_db_path)
        chat_a = insert_chat(conn, "guid-a", "thread-a")
        insert_message(conn, chat_a, "g1", "https://open.spotify.com/track/aaa", 0, 1753000000)
        insert_message(conn, chat_a, "g2", "https://open.spotify.com/track/bbb", 0, 1753000100)
        conn.close()

        def flaky_push(share, ingest_url, bearer_key, **kwargs):
            return share["messageGuid"] != "g2"

        run_once(fixture_db_path, state_path, "url", "key", push_share=flaky_push)

        fake_push, calls = _fake_push_recorder()
        run_once(fixture_db_path, state_path, "url", "key", push_share=fake_push)

        assert {c["messageGuid"] for c in calls} == {"g2"}
