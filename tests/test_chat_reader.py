"""
RED-before-GREEN: extractor/chat_reader.py -- the read-only chat.db reader.

Covers: read-only DB open (write attempts fail), scanning ALL conversations
(no thread filter), Apple-epoch -> unix conversion, and the raw row shape
fetch_new_messages() hands to share_builder.py.
"""

import os
import sqlite3

import pytest

from tests.chat_db_fixture import (
    create_fixture_db,
    insert_chat,
    insert_handle,
    insert_message,
)


@pytest.fixture
def fixture_db_path(tmp_path):
    path = str(tmp_path / "chat.db")
    conn = create_fixture_db(path)
    conn.close()
    return path


class TestApplyEpochToUnix:
    def test_nanosecond_epoch_roundtrip(self):
        from extractor.chat_reader import apple_epoch_to_unix
        from tests.chat_db_fixture import apple_ns_from_unix

        unix_time = 1753000000
        apple_ns = apple_ns_from_unix(unix_time)
        assert apple_epoch_to_unix(apple_ns) == unix_time

    def test_known_reference_point(self):
        # Apple epoch zero == 2001-01-01T00:00:00Z == unix 978307200
        from extractor.chat_reader import apple_epoch_to_unix

        assert apple_epoch_to_unix(0) == 978307200


class TestOpenReadOnlyConnection:
    def test_can_read(self, fixture_db_path):
        from extractor.chat_reader import open_read_only_connection

        conn = open_read_only_connection(fixture_db_path)
        cur = conn.execute("SELECT COUNT(*) FROM message")
        assert cur.fetchone()[0] == 0
        conn.close()

    def test_cannot_write(self, fixture_db_path):
        from extractor.chat_reader import open_read_only_connection

        conn = open_read_only_connection(fixture_db_path)
        with pytest.raises(sqlite3.OperationalError):
            conn.execute("INSERT INTO message (guid, text) VALUES ('x', 'y')")
        conn.close()

    def test_missing_file_raises(self, tmp_path):
        from extractor.chat_reader import open_read_only_connection

        missing = str(tmp_path / "does-not-exist.db")
        with pytest.raises(sqlite3.OperationalError):
            conn = open_read_only_connection(missing)
            conn.execute("SELECT 1")


class TestFetchNewMessages:
    def test_scans_across_multiple_conversations(self, fixture_db_path):
        from extractor.chat_reader import open_read_only_connection, fetch_new_messages

        conn = sqlite3.connect(fixture_db_path)
        chat_a = insert_chat(conn, "chat-guid-a", "+13364042196")
        chat_b = insert_chat(conn, "chat-guid-b", "group-chat-id")
        h1 = insert_handle(conn, "+13364042196")

        insert_message(conn, chat_a, "guid-1", "check https://open.spotify.com/track/aaa", 0, 1753000000, h1)
        insert_message(conn, chat_b, "guid-2", "https://soundcloud.com/artist/bbb", 1, 1753000100, None)
        conn.close()

        ro_conn = open_read_only_connection(fixture_db_path)
        rows = fetch_new_messages(ro_conn, since_rowid=0)
        ro_conn.close()

        assert len(rows) == 2
        chat_ids = {r["chat_id"] for r in rows}
        assert chat_ids == {"+13364042196", "group-chat-id"}

    def test_since_rowid_excludes_already_seen(self, fixture_db_path):
        from extractor.chat_reader import open_read_only_connection, fetch_new_messages

        conn = sqlite3.connect(fixture_db_path)
        chat_a = insert_chat(conn, "chat-guid-a", "thread-1")
        r1 = insert_message(conn, chat_a, "guid-1", "https://open.spotify.com/track/aaa", 0, 1753000000)
        r2 = insert_message(conn, chat_a, "guid-2", "https://open.spotify.com/track/bbb", 0, 1753000100)
        conn.close()

        ro_conn = open_read_only_connection(fixture_db_path)
        rows = fetch_new_messages(ro_conn, since_rowid=r1)
        ro_conn.close()

        assert [r["rowid"] for r in rows] == [r2]

    def test_row_shape_includes_expected_fields(self, fixture_db_path):
        from extractor.chat_reader import open_read_only_connection, fetch_new_messages

        conn = sqlite3.connect(fixture_db_path)
        chat_a = insert_chat(conn, "chat-guid-a", "thread-1")
        h1 = insert_handle(conn, "+15551234567")
        insert_message(conn, chat_a, "guid-1", "text here", 1, 1753000000, h1)
        conn.close()

        ro_conn = open_read_only_connection(fixture_db_path)
        rows = fetch_new_messages(ro_conn, since_rowid=0)
        ro_conn.close()

        row = rows[0]
        for field in ("rowid", "guid", "text", "attributed_body", "is_from_me", "date", "handle_identifier", "chat_id"):
            assert field in row

    def test_out_message_has_no_handle_identifier(self, fixture_db_path):
        from extractor.chat_reader import open_read_only_connection, fetch_new_messages

        conn = sqlite3.connect(fixture_db_path)
        chat_a = insert_chat(conn, "chat-guid-a", "thread-1")
        insert_message(conn, chat_a, "guid-1", "https://open.spotify.com/track/aaa", 1, 1753000000, handle_rowid=None)
        conn.close()

        ro_conn = open_read_only_connection(fixture_db_path)
        rows = fetch_new_messages(ro_conn, since_rowid=0)
        ro_conn.close()

        assert rows[0]["handle_identifier"] is None
        assert rows[0]["is_from_me"] == 1

    def test_empty_result_when_nothing_new(self, fixture_db_path):
        from extractor.chat_reader import open_read_only_connection, fetch_new_messages

        ro_conn = open_read_only_connection(fixture_db_path)
        rows = fetch_new_messages(ro_conn, since_rowid=0)
        ro_conn.close()

        assert rows == []
