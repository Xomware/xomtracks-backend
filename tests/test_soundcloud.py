"""
RED-before-GREEN: lambdas/common/soundcloud.py -- the network-free parse
helpers behind the SoundCloud client_id refresh (scraped from the public web
player the same way xomcloud-backend's stored id is obtained).

Only the parse + scrape-orchestration logic is tested here; the fetch edge is
injected so there is zero real network. refresh_client_id (SSM write) is not
exercised against live AWS.
"""

from lambdas.common.soundcloud import (
    SOUNDCLOUD_CLIENT_ID_PARAM,
    extract_script_urls,
    find_client_id,
    scrape_client_id,
)

_ID = "abcdef0123456789ABCDEF0123456789"  # 32 base62 chars


class TestExtractScriptUrls:
    def test_finds_all_script_srcs_in_order(self):
        html = (
            '<script crossorigin src="https://a-v2.sndcdn.com/assets/0-first.js"></script>'
            '<script crossorigin src="https://a-v2.sndcdn.com/assets/49-last.js"></script>'
        )
        assert extract_script_urls(html) == [
            "https://a-v2.sndcdn.com/assets/0-first.js",
            "https://a-v2.sndcdn.com/assets/49-last.js",
        ]

    def test_empty_html_returns_empty(self):
        assert extract_script_urls("") == []


class TestFindClientId:
    def test_finds_object_literal_form(self):
        assert find_client_id(f'e.exports={{client_id:"{_ID}",host:"x"}}') == _ID

    def test_finds_url_query_form(self):
        assert find_client_id(f"https://api-v2.soundcloud.com/x?client_id={_ID}&y=1") == _ID

    def test_none_when_absent(self):
        assert find_client_id("no id here") is None

    def test_ignores_wrong_length_tokens(self):
        assert find_client_id('client_id:"tooshort"') is None


class TestScrapeClientId:
    def test_scans_bundles_last_first_and_returns_id(self):
        homepage = (
            '<script src="https://a-v2.sndcdn.com/assets/app.js"></script>'
            '<script src="https://a-v2.sndcdn.com/assets/vendor.js"></script>'
        )
        pages = {
            "https://soundcloud.com/": homepage,
            "https://a-v2.sndcdn.com/assets/app.js": "no id in this one",
            "https://a-v2.sndcdn.com/assets/vendor.js": f'client_id:"{_ID}"',
        }
        fetched = []

        def fake_fetch(url):
            fetched.append(url)
            return pages[url]

        assert scrape_client_id(fetch=fake_fetch) == _ID
        # LAST bundle scanned first -> vendor.js hit before app.js.
        assert fetched[1] == "https://a-v2.sndcdn.com/assets/vendor.js"

    def test_none_when_homepage_empty(self):
        assert scrape_client_id(fetch=lambda url: "") is None

    def test_none_when_no_bundle_has_id(self):
        homepage = '<script src="https://a-v2.sndcdn.com/assets/app.js"></script>'

        def fake_fetch(url):
            return homepage if url.endswith("/") else "nothing useful"

        assert scrape_client_id(fetch=fake_fetch) is None


def test_ssm_param_path_is_xomtracks_scoped():
    assert SOUNDCLOUD_CLIENT_ID_PARAM == "/xomtracks/soundcloud/CLIENT_ID"


_GOOD = "GOODcandidate0123456789abcdefABC"  # 32 base62 chars
_BAD = "BADcandidate00000000000000000000"   # 32 base62 chars


class TestRefreshClientIdValidation:
    """
    refresh_client_id scrapes candidates, keeps the FIRST that validates
    against the resolve endpoint, and persists it. When nothing validates (or
    nothing scrapes) it writes NOTHING -- the stale id is never clobbered.
    Both the fetch and validate edges are injected; SSM writes are captured.
    """

    def _homepage(self):
        return '<script src="https://a-v2.sndcdn.com/assets/app.js"></script>'

    def test_picks_first_validating_candidate_and_writes_ssm(self, monkeypatch):
        from lambdas.common import soundcloud

        pages = {
            "https://soundcloud.com/": self._homepage(),
            # Bundle inlines a bad candidate first, then the live one.
            "https://a-v2.sndcdn.com/assets/app.js": f'client_id:"{_BAD}",x client_id:"{_GOOD}"',
        }
        from lambdas.common import ssm_helpers

        writes = []
        monkeypatch.setattr(
            ssm_helpers, "put_ssm_param", lambda name, value, secure=True: writes.append((name, value))
        )

        result = soundcloud.refresh_client_id(
            fetch=lambda url: pages[url],
            validate=lambda cid: cid == _GOOD,
        )

        assert result == _GOOD
        assert writes == [(soundcloud.SOUNDCLOUD_CLIENT_ID_PARAM, _GOOD)]

    def test_no_candidate_validates_writes_nothing(self, monkeypatch):
        from lambdas.common import soundcloud, ssm_helpers

        pages = {
            "https://soundcloud.com/": self._homepage(),
            "https://a-v2.sndcdn.com/assets/app.js": f'client_id:"{_BAD}"',
        }
        writes = []
        monkeypatch.setattr(ssm_helpers, "put_ssm_param", lambda *a, **k: writes.append(a))

        result = soundcloud.refresh_client_id(fetch=lambda url: pages[url], validate=lambda cid: False)

        assert result is None
        assert writes == []

    def test_empty_scrape_writes_nothing(self, monkeypatch):
        from lambdas.common import soundcloud, ssm_helpers

        writes = []
        monkeypatch.setattr(ssm_helpers, "put_ssm_param", lambda *a, **k: writes.append(a))

        result = soundcloud.refresh_client_id(fetch=lambda url: "", validate=lambda cid: True)

        assert result is None
        assert writes == []


class TestScrapeCandidates:
    def test_collects_all_candidates_last_bundle_first(self):
        from lambdas.common.soundcloud import scrape_client_id_candidates

        homepage = (
            '<script src="https://a-v2.sndcdn.com/assets/app.js"></script>'
            '<script src="https://a-v2.sndcdn.com/assets/vendor.js"></script>'
        )
        a, b = "aaaa0123456789abcdef0123456789AB", "bbbb0123456789abcdef0123456789CD"
        pages = {
            "https://soundcloud.com/": homepage,
            "https://a-v2.sndcdn.com/assets/app.js": f'client_id:"{a}"',
            "https://a-v2.sndcdn.com/assets/vendor.js": f'client_id:"{b}"',
        }
        # vendor.js (LAST) scanned first -> b before a.
        assert scrape_client_id_candidates(fetch=lambda url: pages[url]) == [b, a]
