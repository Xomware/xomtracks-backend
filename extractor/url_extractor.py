"""
URL extraction + platform detection for the Xomtracks extractor.

Load-bearing finding (verified against Dom's real chat.db before this repo
existed): matching `text` alone found ZERO music links; matching
`text` + `attributedBody` found 13. iMessage's link-preview bubbles store
the actual URL in `attributedBody` (a binary blob), not in the plain `text`
column -- so extraction MUST cover both, or the extractor misses nearly
every real share.

`attributedBody` comes in two shapes depending on macOS/message age:
  1. Modern (`bplist00` header): an NSKeyedArchiver-encoded binary plist.
     We parse it with `plistlib` and walk every string value looking for
     URLs -- this recovers the URL cleanly, without stray bytes attached.
  2. Legacy ("typedstream", NeXT/Apple's older archive format -- header is
     NOT `bplist00`): plistlib can't parse this. We fall back to a raw
     regex scan of the decoded bytes. This works in practice because the
     URL's ASCII bytes survive as one contiguous run inside the archive
     even though the surrounding bytes aren't valid UTF-8/plist -- the
     same trick most third-party iMessage-export tools use rather than
     writing a full typedstream decoder.
"""

import plistlib
import re

PLATFORM_PATTERNS: dict[str, re.Pattern] = {
    "spotify": re.compile(r"^https?://open\.spotify\.com/", re.IGNORECASE),
    "soundcloud": re.compile(r"^https?://(?:www\.)?soundcloud\.com/", re.IGNORECASE),
    "apple": re.compile(r"^https?://music\.apple\.com/", re.IGNORECASE),
}

# Generic URL matcher -- restricted to RFC 3986 unreserved + reserved URL
# characters. Deliberately NOT a permissive "anything but whitespace"
# class: attributedBody's raw-bytes fallback path regexes across binary
# noise (control bytes, stray multi-byte junk) surrounding the URL, and a
# permissive class would swallow that noise into the "URL". Restricting to
# real URL characters cleanly stops at the first non-URL byte instead.
_URL_PATTERN = re.compile(r"https?://[A-Za-z0-9\-._~:/?#\[\]@!$&'()*+,;=%]+", re.IGNORECASE)

# Trailing characters to strip off a matched URL -- prose punctuation and
# closing brackets/quotes that the greedy URL regex can't distinguish from
# real URL characters (e.g. a `)` closing "(check this out: URL)").
_TRAILING_JUNK = ").,;:!?]}’”"

# Legacy typedstream archives store the link-preview URL as a
# length-prefixed NSString and immediately follow it -- with NO null
# separator -- by the archived link-attribute class token, whose leading
# bytes are the ASCII run `WHttpURL/`. Every one of those characters is
# URL-legal, so the raw-byte fallback regex (which has no length info to
# stop at) runs straight past the real end of the URL and swallows the
# token. Left unstripped, the recovered URL differs from the byte-identical
# copy in the message's `text` column, so the SAME link produced TWO shares
# with two different deterministic shareIds -- this is what doubled every
# link-preview share in production. We strip the marker only on the
# raw-byte typedstream path (the bplist and plain-text paths recover clean
# URLs and never see it). Anchored at end-of-string and requiring the full
# `Http(s)URL` token keeps this from ever touching a genuine URL.
_TYPEDSTREAM_URL_TAIL = re.compile(r"W?Https?URL/?$")


def detect_platform(url: str) -> str | None:
    """Return 'spotify' | 'soundcloud' | 'apple', or None if unsupported."""
    for platform, pattern in PLATFORM_PATTERNS.items():
        if pattern.match(url):
            return platform
    return None


def _clean_and_filter(raw_urls: list[str]) -> list[str]:
    """Strip trailing junk, then keep only supported-platform URLs, de-duped
    in first-seen order."""
    seen: set[str] = set()
    cleaned: list[str] = []
    for raw in raw_urls:
        url = raw.rstrip(_TRAILING_JUNK)
        if not url or detect_platform(url) is None:
            continue
        if url in seen:
            continue
        seen.add(url)
        cleaned.append(url)
    return cleaned


def extract_urls_from_text(text: str | None) -> list[str]:
    """Extract supported-platform music URLs from the plain `text` column."""
    if not text:
        return []
    return _clean_and_filter(_URL_PATTERN.findall(text))


def _walk_plist_strings(node, out: list[str]) -> None:
    """Recursively collect every string value in a parsed plist structure."""
    if isinstance(node, str):
        out.append(node)
    elif isinstance(node, dict):
        for value in node.values():
            _walk_plist_strings(value, out)
    elif isinstance(node, (list, tuple)):
        for value in node:
            _walk_plist_strings(value, out)


def _extract_urls_from_bplist(blob: bytes) -> list[str] | None:
    """Try parsing as a binary plist (NSKeyedArchiver). Returns None if the
    blob isn't a valid bplist at all (caller falls back to raw-byte regex)."""
    try:
        parsed = plistlib.loads(blob, fmt=plistlib.FMT_BINARY)
    except Exception:
        return None

    strings: list[str] = []
    _walk_plist_strings(parsed, strings)

    urls: list[str] = []
    for s in strings:
        urls.extend(_URL_PATTERN.findall(s))
    return urls


def _extract_urls_from_raw_bytes(blob: bytes) -> list[str]:
    """Fallback for legacy typedstream blobs: decode permissively and regex
    the whole thing. The URL's ASCII run survives even amid binary noise --
    but the archived class token glued onto its tail (see
    _TYPEDSTREAM_URL_TAIL) must be stripped so the recovered URL matches the
    plain-text copy byte-for-byte and doesn't dedup into a second share."""
    text = blob.decode("utf-8", errors="ignore")
    return [_TYPEDSTREAM_URL_TAIL.sub("", url) for url in _URL_PATTERN.findall(text)]


def extract_urls_from_attributed_body(blob: bytes | None) -> list[str]:
    """Extract supported-platform music URLs from the `attributedBody` blob."""
    if not blob:
        return []

    bplist_urls = _extract_urls_from_bplist(blob)
    if bplist_urls is not None:
        raw_urls = bplist_urls
    else:
        raw_urls = _extract_urls_from_raw_bytes(blob)

    return _clean_and_filter(raw_urls)
