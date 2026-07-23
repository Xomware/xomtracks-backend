"""
Builds a throwaway SQLite file that mimics the real ~/Library/Messages/chat.db
schema (trimmed to the columns the extractor actually reads) -- multiple
conversations (1:1 + group), mixed is_from_me, plain-text URLs, and
attributedBody-only link-preview URLs (both a bplist-ish NSKeyedArchiver
shape and a raw-bytes "typedstream" fallback shape).

Real chat.db has many more columns/tables; this fixture only needs what
chat_reader.py's query touches.
"""

import plistlib
import sqlite3


def create_fixture_db(path: str) -> sqlite3.Connection:
    conn = sqlite3.connect(path)
    conn.execute(
        """
        CREATE TABLE message (
            ROWID INTEGER PRIMARY KEY AUTOINCREMENT,
            guid TEXT,
            text TEXT,
            attributedBody BLOB,
            is_from_me INTEGER,
            date INTEGER,
            handle_id INTEGER
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE handle (
            ROWID INTEGER PRIMARY KEY AUTOINCREMENT,
            id TEXT
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE chat (
            ROWID INTEGER PRIMARY KEY AUTOINCREMENT,
            guid TEXT,
            chat_identifier TEXT
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE chat_message_join (
            chat_id INTEGER,
            message_id INTEGER
        )
        """
    )
    conn.commit()
    return conn


def apple_ns_from_unix(unix_epoch: int) -> int:
    """Inverse of chat_reader.apple_epoch_to_unix -- nanoseconds since 2001-01-01."""
    return (unix_epoch - 978307200) * 1_000_000_000


def bplist_attributed_body(url: str) -> bytes:
    """
    A simplified stand-in for a real NSKeyedArchiver-encoded attributedBody
    plist: a binary plist whose $objects array contains the shared link
    URL as one of the NSString entries, mirroring where the real payload
    lives (LPLinkMetadata's URL string, buried among other objects). Real
    attributedBody plists are far more elaborate; this is enough to
    exercise the bplist-parsing branch of extract_urls_from_attributed_body.
    """
    payload = {
        "$version": 100000,
        "$archiver": "NSKeyedArchiver",
        "$objects": ["$null", "NSAttributedString", "Hey check this out", url, {"NS.relative": url}],
        "$top": {"root": plistlib.UID(1)},
    }
    return plistlib.dumps(payload, fmt=plistlib.FMT_BINARY)


def typedstream_attributed_body(url: str, glue_url_tail: bool = True) -> bytes:
    """
    A stand-in for the legacy NeXT/Apple "typedstream" attributedBody
    format (starts with a streamtyped header, not `bplist00`). We don't
    implement a real typedstream encoder -- just enough raw bytes,
    including some binary noise around a fully-intact ASCII URL substring,
    to exercise the regex-fallback branch (this is genuinely how these
    tools recover URLs from typedstream blobs in practice: the URL's ASCII
    bytes survive as a contiguous run even though the rest isn't valid
    UTF-8/plist).

    Load-bearing realism (verified against Dom's live xomtracks-shares
    table): typedstream link blobs do NOT place a null byte right after the
    URL string. The archived link-attribute class token follows immediately,
    and its leading bytes are ASCII (`WHttpURL/`) -- all URL-legal
    characters. A greedy URL regex therefore runs straight past the real end
    of the URL and swallows `WHttpURL/`, producing a second, corrupted copy
    of the same link. That is exactly what doubled every link-preview share
    in production. `glue_url_tail=True` reproduces that artifact so the
    extractor's tail-stripping is tested against the real failure mode; set
    it False for a blob whose URL happens to be followed by binary noise.
    """
    noise_before = bytes([0x04, 0x0B, 0x73, 0x74, 0x72, 0x65, 0x61, 0x6D, 0x9F, 0x01, 0x00, 0x84, 0x01])
    url_tail = b"WHttpURL/" if glue_url_tail else b""
    noise_after = bytes([0x00, 0x86, 0x84, 0x02, 0x69])
    return noise_before + url.encode("utf-8") + url_tail + noise_after


def insert_chat(conn: sqlite3.Connection, guid: str, chat_identifier: str) -> int:
    cur = conn.execute("INSERT INTO chat (guid, chat_identifier) VALUES (?, ?)", (guid, chat_identifier))
    conn.commit()
    return cur.lastrowid


def insert_handle(conn: sqlite3.Connection, identifier: str) -> int:
    cur = conn.execute("INSERT INTO handle (id) VALUES (?)", (identifier,))
    conn.commit()
    return cur.lastrowid


def insert_message(
    conn: sqlite3.Connection,
    chat_rowid: int,
    guid: str,
    text: str | None,
    is_from_me: int,
    unix_date: int,
    handle_rowid: int | None = None,
    attributed_body: bytes | None = None,
) -> int:
    cur = conn.execute(
        "INSERT INTO message (guid, text, attributedBody, is_from_me, date, handle_id) VALUES (?, ?, ?, ?, ?, ?)",
        (guid, text, attributed_body, is_from_me, apple_ns_from_unix(unix_date), handle_rowid),
    )
    message_rowid = cur.lastrowid
    conn.execute("INSERT INTO chat_message_join (chat_id, message_id) VALUES (?, ?)", (chat_rowid, message_rowid))
    conn.commit()
    return message_rowid
