"""
Read-only reader for ~/Library/Messages/chat.db.

Design requirements (PLAN.md Phase 2 + hard-won findings from host
verification):
- Opened strictly READ-ONLY (`mode=ro` URI) -- the extractor never writes
  to chat.db.
- Scans the WHOLE database, joined across `chat`/`chat_message_join` --
  every 1:1 and group conversation, no per-thread filter. Dom is a
  participant in every conversation, so his chat.db already contains every
  shared link from every contact/thread.
- Tracked by `message.ROWID` (insert order), NOT `message.date`. iCloud
  history backfill inserts old messages with NEW (higher) ROWIDs but OLD
  dates -- watermarking on date would silently swallow backfilled history
  forever; watermarking on ROWID picks it up automatically on the next
  scan, because ROWID always reflects "have we seen this row before",
  independent of what date the message actually happened on.
"""

import sqlite3

# Apple's epoch (Core Data / CFAbsoluteTime reference date) is
# 2001-01-01T00:00:00Z, which is 978307200 seconds after the Unix epoch.
_APPLE_EPOCH_OFFSET_SECONDS = 978307200

# Modern macOS (Sierra/10.12+) stores `message.date` as nanoseconds since
# the Apple epoch. Anything below this magnitude is almost certainly the
# older seconds-since-Apple-epoch format (pre-Sierra chat.db) -- 10**11 ns
# is ~1.7 days, seconds-since-2001 won't cross that for a very long time
# from now, so this heuristic safely distinguishes the two without needing
# to know the macOS version that wrote the row.
_NANOSECOND_THRESHOLD = 10**11


def apple_epoch_to_unix(date_value: int) -> int:
    """Convert a `message.date` value (Apple-epoch nanoseconds on modern
    macOS, seconds on very old chat.db files) to a Unix epoch integer."""
    if abs(date_value) >= _NANOSECOND_THRESHOLD:
        seconds = date_value / 1_000_000_000
    else:
        seconds = date_value
    return int(seconds) + _APPLE_EPOCH_OFFSET_SECONDS


def open_read_only_connection(db_path: str, immutable: bool = False) -> sqlite3.Connection:
    """
    Open chat.db strictly read-only via SQLite's URI mode.

    `immutable` is deliberately NOT the default: chat.db is actively
    written by Messages.app while the extractor runs, and `immutable=1`
    tells SQLite the file will never change for the connection's lifetime
    -- safe only because the extractor opens a fresh, short-lived
    connection per scan and never holds one open across polls. Left as an
    opt-in for callers who want the (small) perf benefit and accept that
    scoping.
    """
    uri = f"file:{db_path}?mode=ro"
    if immutable:
        uri += "&immutable=1"
    conn = sqlite3.connect(uri, uri=True)
    conn.row_factory = sqlite3.Row
    return conn


_FETCH_QUERY = """
    SELECT
        m.ROWID AS rowid,
        m.guid AS guid,
        m.text AS text,
        m.attributedBody AS attributed_body,
        m.is_from_me AS is_from_me,
        m.date AS date,
        h.id AS handle_identifier,
        c.chat_identifier AS chat_id
    FROM message m
    LEFT JOIN handle h ON m.handle_id = h.ROWID
    LEFT JOIN chat_message_join cmj ON cmj.message_id = m.ROWID
    LEFT JOIN chat c ON cmj.chat_id = c.ROWID
    WHERE m.ROWID > ?
    ORDER BY m.ROWID ASC
"""


def fetch_new_messages(conn: sqlite3.Connection, since_rowid: int) -> list[dict]:
    """
    Fetch every message row (across ALL conversations, 1:1 and group) with
    ROWID > since_rowid, in ascending ROWID order.

    A message can join to more than one chat row in real chat.db (rare
    edge cases); this query takes the first match via LEFT JOIN, which is
    sufficient for xomtracks' purposes (chatId is a "nice to have" field
    for the by-sharer/by-thread fast-follow, not load-bearing for
    dedup/matching).
    """
    cursor = conn.execute(_FETCH_QUERY, (since_rowid,))
    rows = cursor.fetchall()

    seen_rowids: set[int] = set()
    results: list[dict] = []
    for row in rows:
        rowid = row["rowid"]
        if rowid in seen_rowids:
            continue
        seen_rowids.add(rowid)
        results.append(dict(row))

    return results
