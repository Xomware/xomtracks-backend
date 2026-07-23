"""
RED-before-GREEN: extractor/contacts.py -- resolve iMessage handles
(phone/email) to macOS Contacts display names.

The load-bearing case is phone normalization: Contacts stores numbers in
human format ("(336) 404-2196", "+1 336-404-2196") while iMessage handles
are E.164 ("+13364042196"). Both must reduce to the same key so a sharer's
raw number renders as their real name in the feed.
"""

import os

from tests.addressbook_fixture import add_contact, create_addressbook_db


def _build_db(tmp_path) -> str:
    path = os.path.join(str(tmp_path), "AddressBook-v22.abcddb")
    conn = create_addressbook_db(path)
    add_contact(conn, 1, first="Jordan", last="Reyes", phones=["(336) 404-2196"])
    add_contact(conn, 2, first="Sam", last="Okoye", phones=["+1 (704) 408-4344"])
    add_contact(conn, 3, first="Casey", last="Lin", emails=["Casey.Lin@Example.com"])
    add_contact(conn, 4, organization="Vinyl Shop", phones=["9195551234"])
    add_contact(conn, 5, nickname="DJ Nix", phones=["+447911123456"])
    conn.close()
    return path


class TestResolveName:
    def test_resolves_e164_handle_to_contact_name(self, tmp_path):
        from extractor.contacts import build_resolver

        resolve = build_resolver([_build_db(tmp_path)])
        assert resolve("+13364042196") == "Jordan Reyes"

    def test_resolves_across_formatting_differences(self, tmp_path):
        from extractor.contacts import build_resolver

        resolve = build_resolver([_build_db(tmp_path)])
        assert resolve("+17044084344") == "Sam Okoye"

    def test_resolves_email_case_insensitively(self, tmp_path):
        from extractor.contacts import build_resolver

        resolve = build_resolver([_build_db(tmp_path)])
        assert resolve("casey.lin@example.com") == "Casey Lin"

    def test_falls_back_to_organization_then_nickname(self, tmp_path):
        from extractor.contacts import build_resolver

        resolve = build_resolver([_build_db(tmp_path)])
        assert resolve("+19195551234") == "Vinyl Shop"
        assert resolve("+447911123456") == "DJ Nix"

    def test_unknown_handle_returns_none(self, tmp_path):
        from extractor.contacts import build_resolver

        resolve = build_resolver([_build_db(tmp_path)])
        assert resolve("+15550009999") is None

    def test_none_or_blank_handle_returns_none(self, tmp_path):
        from extractor.contacts import build_resolver

        resolve = build_resolver([_build_db(tmp_path)])
        assert resolve(None) is None
        assert resolve("") is None


class TestMissingDatabase:
    def test_no_readable_db_yields_noop_resolver(self, tmp_path):
        # Off-host / CI: no AddressBook DB present -> resolver never raises,
        # just resolves nothing (extractor keeps working without names).
        from extractor.contacts import build_resolver

        resolve = build_resolver([os.path.join(str(tmp_path), "does-not-exist.abcddb")])
        assert resolve("+13364042196") is None
