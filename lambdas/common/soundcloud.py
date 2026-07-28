"""
XOMTRACKS SoundCloud client_id refresh
======================================
SoundCloud's public `api-v2` (the endpoint the cross-platform matcher's
SoundCloud resolver calls -- see matching.default_soundcloud_resolver) needs
a `client_id` that SoundCloud no longer issues through any developer portal.
The established approach -- the same one that populates the value
xomcloud-backend reads at /xomcloud/soundcloud/CLIENT_ID -- is to scrape it
from the public web player's JS bundles, where it appears inline as
`client_id:"<32 base62 chars>"`.

SoundCloud rotates that id periodically, which silently breaks the resolver
(every SoundCloud share degrades to `unmatched` with no title). This module
refreshes it: it scrapes a fresh id from the web player and writes it to
xomtracks' OWN SSM parameter (/xomtracks/soundcloud/CLIENT_ID) via
ssm_helpers.put_ssm_param.

The parse helpers (extract_script_urls / find_client_id / scrape_client_id)
are network-free and unit-tested; only refresh_client_id() touches the
network and SSM. Run it locally when SoundCloud recovery starts failing:
    python -m lambdas.common.soundcloud
"""

import re

import requests

from lambdas.common.constants import PRODUCT
from lambdas.common.logger import get_logger

log = get_logger(__file__)

SOUNDCLOUD_HOMEPAGE = "https://soundcloud.com/"
SOUNDCLOUD_CLIENT_ID_PARAM = f"/{PRODUCT}/soundcloud/CLIENT_ID"

# A known-good public SoundCloud track. A candidate client_id is only accepted
# after it resolves this URL with HTTP 200 -- proof the scraped 32-char token is
# actually a live api-v2 credential and not some unrelated 32-char string that
# happened to sit next to `client_id` in a bundle.
SOUNDCLOUD_RESOLVE_URL = "https://api-v2.soundcloud.com/resolve"
SOUNDCLOUD_VALIDATION_TRACK = "https://soundcloud.com/griz/ffrv1"

# <script ... src="https://a-v2.sndcdn.com/assets/....js"> tags on the
# web-player HTML. crossorigin/defer ordering varies, so match src= loosely.
_SCRIPT_SRC = re.compile(r'<script[^>]+src="([^"]+)"', re.IGNORECASE)

# The client_id appears in the bundle as `client_id:"XXXX"` (object literal)
# or `?client_id=XXXX` (inlined URL). SoundCloud ids are 32 base62 chars.
_CLIENT_ID = re.compile(r'client_id[:=]\\?["\']?([0-9A-Za-z]{32})')

_REQUEST_TIMEOUT = 10


def extract_script_urls(html: str) -> list[str]:
    """Every `<script src="...">` URL on the page, in document order."""
    return _SCRIPT_SRC.findall(html or "")


def find_client_id(js: str) -> str | None:
    """The first 32-char SoundCloud client_id in a JS blob, or None."""
    match = _CLIENT_ID.search(js or "")
    return match.group(1) if match else None


def find_client_ids(js: str) -> list[str]:
    """
    Every distinct 32-char client_id candidate in a JS blob, in first-seen
    order. A single bundle can inline more than one -- only the live one
    resolves, which is why refresh_client_id validates each candidate.
    """
    seen: set[str] = set()
    out: list[str] = []
    for candidate in _CLIENT_ID.findall(js or ""):
        if candidate not in seen:
            seen.add(candidate)
            out.append(candidate)
    return out


def _default_fetch(url: str) -> str:
    """Fetch a URL's text body. Isolated so tests inject a fake fetcher."""
    resp = requests.get(url, timeout=_REQUEST_TIMEOUT)
    if resp.status_code != 200:
        log.warning(f"SoundCloud scrape fetch failed ({resp.status_code}) for {url}")
        return ""
    return resp.text


def scrape_client_id(fetch=_default_fetch) -> str | None:
    """
    Scrape a fresh SoundCloud client_id from the public web player.

    Fetches the homepage, then walks its <script> bundles (LAST first -- the
    client_id lives in one of the trailing app bundles, so scanning from the
    end finds it in the fewest fetches) and returns the first id found. None
    if the layout changed and no id could be located (caller logs + aborts,
    never writes a bad value).

    `fetch(url) -> str` is injected so the whole scrape path is unit-testable
    with zero network.
    """
    homepage = fetch(SOUNDCLOUD_HOMEPAGE)
    if not homepage:
        log.error("SoundCloud homepage returned no HTML -- cannot scrape client_id")
        return None

    script_urls = extract_script_urls(homepage)
    for url in reversed(script_urls):
        client_id = find_client_id(fetch(url))
        if client_id:
            log.info(f"Scraped SoundCloud client_id from {url}")
            return client_id

    log.error(f"No client_id found across {len(script_urls)} SoundCloud script bundle(s)")
    return None


def scrape_client_id_candidates(fetch=_default_fetch) -> list[str]:
    """
    Every client_id candidate scraped from the public web player, ordered so
    the most likely valid ones come first: bundles are walked LAST-first (the
    id lives in a trailing app bundle) and candidates within a bundle keep
    document order. Deduped across bundles. Empty when the layout changed and
    nothing matched.

    `fetch(url) -> str` is injected so the scrape path is unit-testable with
    zero network.
    """
    homepage = fetch(SOUNDCLOUD_HOMEPAGE)
    if not homepage:
        log.error("SoundCloud homepage returned no HTML -- cannot scrape client_id")
        return []

    seen: set[str] = set()
    candidates: list[str] = []
    for url in reversed(extract_script_urls(homepage)):
        for candidate in find_client_ids(fetch(url)):
            if candidate not in seen:
                seen.add(candidate)
                candidates.append(candidate)
    return candidates


def _default_validate(client_id: str) -> bool:
    """
    True if `client_id` is a live api-v2 credential -- i.e. it resolves a
    known-good public track with HTTP 200. Network errors count as invalid
    (never accept a candidate we couldn't prove works).
    """
    try:
        resp = requests.get(
            SOUNDCLOUD_RESOLVE_URL,
            params={"url": SOUNDCLOUD_VALIDATION_TRACK, "client_id": client_id},
            timeout=_REQUEST_TIMEOUT,
        )
    except Exception as err:  # noqa: BLE001 -- a probe failure just means "unproven"
        log.warning(f"SoundCloud client_id validation request failed: {err}")
        return False
    return resp.status_code == 200


def refresh_client_id(fetch=_default_fetch, validate=_default_validate) -> str | None:
    """
    Scrape fresh SoundCloud client_id candidates, keep the FIRST one that
    validates against the public resolve endpoint, and persist it to SSM at
    /xomtracks/soundcloud/CLIENT_ID (also refreshing the in-process cache).

    Returns the new id, or None (and writes NOTHING) when nothing could be
    scraped or no candidate validated -- the stale id is left in place rather
    than clobbered with a value that doesn't work. This is the self-heal the
    matcher calls when a resolve returns 401/403.
    """
    from lambdas.common import ssm_helpers

    candidates = scrape_client_id_candidates(fetch)
    if not candidates:
        log.error("SoundCloud client_id scrape found no candidates -- SSM left unchanged")
        return None

    for candidate in candidates:
        if validate(candidate):
            ssm_helpers.put_ssm_param(SOUNDCLOUD_CLIENT_ID_PARAM, candidate, secure=True)
            log.info(f"Updated SSM {SOUNDCLOUD_CLIENT_ID_PARAM} with a validated SoundCloud client_id")
            return candidate

    log.error(
        f"None of {len(candidates)} scraped SoundCloud client_id candidate(s) validated -- SSM left unchanged"
    )
    return None


if __name__ == "__main__":
    new_id = refresh_client_id()
    if new_id:
        print(f"Refreshed SoundCloud client_id -> {SOUNDCLOUD_CLIENT_ID_PARAM}")
    else:
        raise SystemExit("Failed to scrape a SoundCloud client_id -- SSM left unchanged")
