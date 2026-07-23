"""
XOMTRACKS Phone Normalization
=============================
Reduce a phone number (any format) to a comparable key: its last-10 digits
(US/NANP) when it has at least that many, else all digits. Empty string for a
value with no digits (never a valid match key).

This MIRRORS extractor/contacts.py::_normalize_phone on purpose -- iMessage
handles are stored raw/E.164 ("+13364042196") while a user typing their number
into the link flow may use any format ("(336) 404-2196", "336.404.2196").
Reducing both sides to their last-10 digits makes them compare equal without a
libphonenumber dependency.

It is a SEPARATE copy (not an import) because extractor/ is a local-only,
Full-Disk-Access macOS job that is never packaged into the Lambda layer
(the deploy workflow only ships lambdas/common/). Keep the two in sync by hand
if the normalization rule ever changes.
"""

import re

_NON_DIGITS = re.compile(r"\D")


def normalize_phone(raw: str | None) -> str:
    """Last-10 digits (NANP) when the value has at least that many, else all
    digits. Empty string when there are no digits at all."""
    if not raw:
        return ""
    digits = _NON_DIGITS.sub("", raw)
    return digits[-10:] if len(digits) >= 10 else digits
