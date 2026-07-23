"""
RED-before-GREEN: lambdas/common/phone.py -- last-10-digit handle
normalization.

Mirrors the extractor's contacts._normalize_phone approach (last-10 digits
for NANP, all-digits fallback) so a phone the user types in the link flow
compares equal to the raw E.164 sharerHandle stored on shares -- WITHOUT
importing the extractor package (which is local-only and never packaged into
the Lambda layer).
"""


class TestNormalizePhone:
    def test_e164_reduces_to_last_ten(self):
        from lambdas.common.phone import normalize_phone

        assert normalize_phone("+13364042196") == "3364042196"

    def test_formatted_forms_match_e164(self):
        from lambdas.common.phone import normalize_phone

        norm = normalize_phone("+13364042196")
        assert normalize_phone("(336) 404-2196") == norm
        assert normalize_phone("336-404-2196") == norm
        assert normalize_phone("336.404.2196") == norm
        assert normalize_phone("1 336 404 2196") == norm

    def test_short_number_keeps_all_digits(self):
        from lambdas.common.phone import normalize_phone

        assert normalize_phone("12345") == "12345"

    def test_blank_and_none_are_empty(self):
        from lambdas.common.phone import normalize_phone

        assert normalize_phone("") == ""
        assert normalize_phone(None) == ""
        assert normalize_phone("no digits here") == ""
