"""
Builds a throwaway SQLite file that mimics the macOS Contacts
(AddressBook-v22.abcddb) schema -- trimmed to only the tables/columns
extractor/contacts.py actually reads. Real AddressBook DBs have dozens of
Z* tables; this fixture needs just enough to exercise phone/email ->
display-name resolution without touching Dom's real, Full-Disk-Access-gated
Contacts DB.
"""

import sqlite3


def create_addressbook_db(path: str) -> sqlite3.Connection:
    conn = sqlite3.connect(path)
    conn.execute(
        """
        CREATE TABLE ZABCDRECORD (
            Z_PK INTEGER PRIMARY KEY,
            ZFIRSTNAME TEXT,
            ZLASTNAME TEXT,
            ZNICKNAME TEXT,
            ZORGANIZATION TEXT
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE ZABCDPHONENUMBER (
            Z_PK INTEGER PRIMARY KEY,
            ZOWNER INTEGER,
            ZFULLNUMBER TEXT
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE ZABCDEMAILADDRESS (
            Z_PK INTEGER PRIMARY KEY,
            ZOWNER INTEGER,
            ZADDRESS TEXT
        )
        """
    )
    conn.commit()
    return conn


def add_contact(
    conn: sqlite3.Connection,
    pk: int,
    first: str | None = None,
    last: str | None = None,
    nickname: str | None = None,
    organization: str | None = None,
    phones: list[str] | None = None,
    emails: list[str] | None = None,
) -> None:
    conn.execute(
        "INSERT INTO ZABCDRECORD (Z_PK, ZFIRSTNAME, ZLASTNAME, ZNICKNAME, ZORGANIZATION) VALUES (?, ?, ?, ?, ?)",
        (pk, first, last, nickname, organization),
    )
    for num in phones or []:
        conn.execute("INSERT INTO ZABCDPHONENUMBER (ZOWNER, ZFULLNUMBER) VALUES (?, ?)", (pk, num))
    for addr in emails or []:
        conn.execute("INSERT INTO ZABCDEMAILADDRESS (ZOWNER, ZADDRESS) VALUES (?, ?)", (pk, addr))
    conn.commit()
