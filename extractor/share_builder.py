"""
Turns a chat_reader.py row into zero or more share-ingest dicts -- the
exact body shape POST /shares/ingest expects (see
lambdas/common/models.ShareIngestRequest).

Direction mapping (PLAN.md): `direction=in` when `is_from_me=0`
(sharerHandle = the sender's handle); `direction=out` when `is_from_me=1`
(Dom is the sender -- no sharerHandle, per shares_dynamo.py's
None-attribute-stripping for the sparse GSI-2).

One text message can carry more than one music link (rare, but real) --
each becomes its own share dict, all sharing the same messageGuid. The
same URL appearing in BOTH `text` and `attributedBody` (e.g. plain link +
its own rich-link preview) collapses to one share, not two.
"""

from typing import Callable

from extractor.chat_reader import apple_epoch_to_unix
from extractor.url_extractor import (
    detect_platform,
    extract_urls_from_attributed_body,
    extract_urls_from_text,
)

# A handle -> contact-name resolver: `(handle) -> display name | None`.
# Normally extractor.contacts.build_resolver(); injectable + optional so the
# builder stays pure and testable, and so an off-host run (no Contacts DB)
# still produces shares, just without names.
ResolveName = Callable[[str | None], str | None]


def build_shares_from_message(row: dict, resolve_name: ResolveName | None = None) -> list[dict]:
    """Build zero or more share-ingest dicts from a single chat_reader row.

    When `resolve_name` is provided, incoming shares (direction=in) get a
    `sharerName` resolved from their `sharerHandle` -- the raw phone/email is
    always kept alongside. Outgoing shares (Dom is the sender) have no
    handle, so no name lookup is attempted.
    """
    text_urls = extract_urls_from_text(row.get("text"))
    body_urls = extract_urls_from_attributed_body(row.get("attributed_body"))

    seen: set[str] = set()
    urls: list[str] = []
    for url in text_urls + body_urls:
        if url in seen:
            continue
        seen.add(url)
        urls.append(url)

    if not urls:
        return []

    is_from_me = bool(row.get("is_from_me"))
    direction = "out" if is_from_me else "in"
    sharer_handle = None if is_from_me else row.get("handle_identifier")
    sharer_name = resolve_name(sharer_handle) if (sharer_handle and resolve_name) else None
    message_date = apple_epoch_to_unix(row["date"])

    shares = []
    for url in urls:
        shares.append({
            "messageGuid": row["guid"],
            "direction": direction,
            "sharerHandle": sharer_handle,
            "sharerName": sharer_name,
            "chatId": row.get("chat_id"),
            "platform": detect_platform(url),
            "sourceUrl": url,
            "messageDate": message_date,
        })
    return shares


def build_shares_from_messages(rows: list[dict], resolve_name: ResolveName | None = None) -> list[dict]:
    """Flat-map build_shares_from_message across many rows, in row order."""
    shares: list[dict] = []
    for row in rows:
        shares.extend(build_shares_from_message(row, resolve_name=resolve_name))
    return shares
