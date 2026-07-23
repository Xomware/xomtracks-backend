"""
Resolve iMessage handles (phone numbers / email addresses) to macOS
Contacts display names.

Why this lives in the extractor (and only runs on Dom's Mac): the Contacts
data is in the local AddressBook SQLite DBs under
`~/Library/Application Support/AddressBook`, gated by macOS Full Disk
Access. The extractor's Python already has FDA (it reads chat.db the same
way), so it -- and nothing in the cloud -- can turn a bare "+13364042196"
sharer into "Jordan Reyes". Opened strictly READ-ONLY, same discipline as
chat_reader.py.

Phone matching is digit-normalized. Contacts stores numbers however the
user typed them ("(336) 404-2196", "+1 336-404-2196", "336.404.2196")
while iMessage handles are E.164 ("+13364042196"). Reducing both to their
last-10 digits (US/NANP) makes the two forms compare equal without a full
libphonenumber dependency. Non-US numbers fall back to all-digits matching,
which still works when both sides carry the same country code. Emails match
case-insensitively.

There can be more than one AddressBook DB (a top-level file plus one per
`Sources/<uuid>/` account). We read every readable one and merge; the first
name found for a given handle wins (accounts are scanned in path order).
"""

import glob
import os
import re
import sqlite3
from typing import Callable

from extractor.logging_setup import get_logger

log = get_logger(__name__)

_ADDRESSBOOK_ROOT = os.path.expanduser("~/Library/Application Support/AddressBook")
_ADDRESSBOOK_DB_NAME = "AddressBook-v22.abcddb"

_NON_DIGITS = re.compile(r"\D")


def default_addressbook_paths() -> list[str]:
    """Every AddressBook DB on this host: the top-level file plus one per
    per-account `Sources/<uuid>/` directory. Returned in a stable order so
    merge precedence is deterministic."""
    paths = [os.path.join(_ADDRESSBOOK_ROOT, _ADDRESSBOOK_DB_NAME)]
    paths.extend(sorted(glob.glob(os.path.join(_ADDRESSBOOK_ROOT, "Sources", "*", _ADDRESSBOOK_DB_NAME))))
    return [p for p in paths if os.path.exists(p)]


def _normalize_phone(raw: str | None) -> str:
    """Reduce a phone number (either format) to a comparable key: its
    last-10 digits (NANP) when it has at least that many, else all digits.
    Empty string for values with no digits at all (never a match key)."""
    if not raw:
        return ""
    digits = _NON_DIGITS.sub("", raw)
    return digits[-10:] if len(digits) >= 10 else digits


def _display_name(first: str | None, last: str | None, nickname: str | None, org: str | None) -> str | None:
    """Best human label for a contact: full name, then nickname, then
    organization. None when the record carries no usable label at all."""
    full = " ".join(part for part in (first, last) if part and part.strip()).strip()
    if full:
        return full
    for fallback in (nickname, org):
        if fallback and fallback.strip():
            return fallback.strip()
    return None


class ContactIndex:
    """Immutable lookup built once per extractor run / backfill. Maps
    normalized phone keys and lowercased emails to display names."""

    def __init__(self, phone_map: dict[str, str], email_map: dict[str, str]) -> None:
        self._phone_map = phone_map
        self._email_map = email_map

    def __len__(self) -> int:
        return len(self._phone_map) + len(self._email_map)

    def resolve(self, handle: str | None) -> str | None:
        """Resolve a single iMessage handle to a contact display name, or
        None if the handle is blank or not in Contacts."""
        if not handle or not handle.strip():
            return None
        handle = handle.strip()
        if "@" in handle:
            return self._email_map.get(handle.lower())
        key = _normalize_phone(handle)
        return self._phone_map.get(key) if key else None


_PHONE_QUERY = """
    SELECT p.ZFULLNUMBER AS number,
           r.ZFIRSTNAME AS first, r.ZLASTNAME AS last,
           r.ZNICKNAME AS nickname, r.ZORGANIZATION AS org
    FROM ZABCDPHONENUMBER p
    JOIN ZABCDRECORD r ON p.ZOWNER = r.Z_PK
    WHERE p.ZFULLNUMBER IS NOT NULL
"""

_EMAIL_QUERY = """
    SELECT e.ZADDRESS AS address,
           r.ZFIRSTNAME AS first, r.ZLASTNAME AS last,
           r.ZNICKNAME AS nickname, r.ZORGANIZATION AS org
    FROM ZABCDEMAILADDRESS e
    JOIN ZABCDRECORD r ON e.ZOWNER = r.Z_PK
    WHERE e.ZADDRESS IS NOT NULL
"""


def _load_one_db(db_path: str, phone_map: dict[str, str], email_map: dict[str, str]) -> None:
    """Merge one AddressBook DB into the maps. Tolerant: a missing, locked,
    or schema-variant DB is logged and skipped, never fatal."""
    try:
        conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    except sqlite3.Error as err:
        log.warning(f"Could not open Contacts DB {db_path}: {err}")
        return
    conn.row_factory = sqlite3.Row
    try:
        for row in conn.execute(_PHONE_QUERY):
            name = _display_name(row["first"], row["last"], row["nickname"], row["org"])
            key = _normalize_phone(row["number"])
            if name and key:
                phone_map.setdefault(key, name)
        for row in conn.execute(_EMAIL_QUERY):
            name = _display_name(row["first"], row["last"], row["nickname"], row["org"])
            key = (row["address"] or "").strip().lower()
            if name and key:
                email_map.setdefault(key, name)
    except sqlite3.Error as err:
        log.warning(f"Could not read Contacts DB {db_path}: {err}")
    finally:
        conn.close()


def load_contact_index(db_paths: list[str] | None = None) -> ContactIndex:
    """Build a ContactIndex from the given AddressBook DBs (default: this
    host's real ones). Never raises for missing/unreadable DBs -- an empty
    index simply resolves nothing, so the extractor keeps working off-host."""
    if db_paths is None:
        db_paths = default_addressbook_paths()

    phone_map: dict[str, str] = {}
    email_map: dict[str, str] = {}
    for db_path in db_paths:
        _load_one_db(db_path, phone_map, email_map)

    log.info(f"Loaded contacts: {len(phone_map)} phone(s), {len(email_map)} email(s)")
    return ContactIndex(phone_map, email_map)


def build_resolver(db_paths: list[str] | None = None) -> Callable[[str | None], str | None]:
    """Convenience: load the index once and hand back its bound `resolve`
    callable -- the shape share_builder / backfill_names expect."""
    return load_contact_index(db_paths).resolve
