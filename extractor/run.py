"""
End-to-end extractor orchestration: read-only chat.db scan -> build shares
-> push to /shares/ingest -> persist watermark.

`run_once()` is a pure(ish) function (all I/O is either explicit args or
injectable) so it's testable end-to-end against a real fixture SQLite file
with a fake push_share -- no real network, no real chat.db needed to prove
correctness. `main()` is the thin CLI wrapper that wires real config
(env vars / CLI args) and is what the launchd LaunchAgent will invoke
later (NOT installed as a persistent job yet -- see PLAN.md Phase 2.6,
explicitly out of scope for this pass).

Failure semantics: shares within one message are pushed in order; if any
push for a message fails, the whole run stops there (does not skip ahead
to later messages) and the watermark is saved at the LAST fully-successful
message's ROWID. This guarantees no message is silently lost -- a failed
push (host asleep, network blip, backend down) is retried in full on the
next scan rather than skipped.
"""

import argparse
import os
import sys

from extractor.chat_reader import fetch_new_messages, open_read_only_connection
from extractor.contacts import build_resolver
from extractor.ingest_client import push_share as default_push_share
from extractor.logging_setup import get_logger
from extractor.share_builder import build_shares_from_message
from extractor.watermark import DEFAULT_STATE_PATH, load_watermark, save_watermark

log = get_logger(__name__)

DEFAULT_CHAT_DB_PATH = os.path.expanduser("~/Library/Messages/chat.db")


def run_once(
    db_path: str,
    state_path: str,
    ingest_url: str,
    bearer_key: str,
    immutable: bool = False,
    push_share=default_push_share,
    resolve_name=None,
) -> dict:
    """
    Run a single extractor scan: since-last-watermark -> ingest push ->
    new watermark. Never writes to chat.db (opens strictly read-only).

    `resolve_name` is an optional `(handle) -> name | None` callable
    (extractor.contacts.build_resolver at the real edge); when provided,
    incoming shares carry the sharer's resolved contact name. Injectable so
    tests exercise scans without touching the host's Contacts DB.

    Returns a stats dict: scanned, shares_found, shares_pushed, failed,
    new_watermark.
    """
    since_rowid = load_watermark(state_path)

    conn = open_read_only_connection(db_path, immutable=immutable)
    try:
        rows = fetch_new_messages(conn, since_rowid)
    finally:
        conn.close()

    log.info(f"Scanning from ROWID {since_rowid}: {len(rows)} new message(s) found")

    scanned = 0
    shares_found = 0
    shares_pushed = 0
    failed = False
    last_good_rowid = since_rowid

    for row in rows:
        shares = build_shares_from_message(row, resolve_name=resolve_name)
        shares_found += len(shares)

        message_fully_pushed = True
        for share in shares:
            ok = push_share(share, ingest_url, bearer_key)
            if ok:
                shares_pushed += 1
            else:
                message_fully_pushed = False
                failed = True
                log.warning(
                    f"Push failed for messageGuid={share.get('messageGuid')} "
                    f"sourceUrl={share.get('sourceUrl')} -- halting scan, "
                    f"will retry from ROWID {last_good_rowid} next run"
                )
                break

        if not message_fully_pushed:
            # Stop here -- don't advance past a partially-processed
            # message, and don't process later (newer) rows out of order.
            break

        scanned += 1
        last_good_rowid = row["rowid"]

    save_watermark(state_path, last_good_rowid)

    stats = {
        "scanned": scanned,
        "shares_found": shares_found,
        "shares_pushed": shares_pushed,
        "failed": failed,
        "new_watermark": last_good_rowid,
    }
    log.info(f"Scan complete: {stats}")
    return stats


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Xomtracks iMessage music-share extractor (read-only, one-shot scan)")
    parser.add_argument("--db-path", default=DEFAULT_CHAT_DB_PATH, help="Path to chat.db")
    parser.add_argument("--state-path", default=DEFAULT_STATE_PATH, help="Path to the watermark state file")
    parser.add_argument("--ingest-url", default=os.environ.get("XOMTRACKS_INGEST_URL", ""), help="POST /shares/ingest URL")
    parser.add_argument("--bearer-key", default=os.environ.get("XOMTRACKS_INGEST_BEARER_KEY", ""), help="Scoped ingest bearer key")
    parser.add_argument("--immutable", action="store_true", help="Open chat.db with immutable=1 (see chat_reader.py docstring)")
    args = parser.parse_args(argv)

    if not args.ingest_url or not args.bearer_key:
        log.error("Missing --ingest-url/--bearer-key (or XOMTRACKS_INGEST_URL/XOMTRACKS_INGEST_BEARER_KEY env vars)")
        return 2

    # Resolve sharer phone/email handles to contact names from the host's
    # local macOS Contacts (Full Disk Access, same as chat.db). Best-effort:
    # build_resolver never raises -- off-host it just resolves nothing.
    resolve_name = build_resolver()

    stats = run_once(
        args.db_path,
        args.state_path,
        args.ingest_url,
        args.bearer_key,
        immutable=args.immutable,
        resolve_name=resolve_name,
    )
    return 1 if stats["failed"] else 0


if __name__ == "__main__":
    sys.exit(main())
