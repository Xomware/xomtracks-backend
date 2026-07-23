"""
XOMTRACKS Track Key Derivation
==============================
A rating belongs to a SONG, not to a single share instance. The same track can
arrive as many separate shares (different people, different days, different
source platforms), each a distinct row in xomtracks-shares. Ratings must
aggregate across all of them, so they are keyed by a normalized TRACK identity
-- `trackKey` -- rather than by shareId.

Rules (in priority order), from `derive_track_key`:
  1. `resolvedSpotifyId` present (matched / manual shares)  -> `spotify:<id>`.
     This is the strongest identity: once the matcher resolves ANY platform's
     share to a Spotify track, every share for that track collapses to one key.
  2. Raw Spotify source URL/URI (a share that came straight from Spotify, even
     before matching)                                       -> `spotify:<id>`.
     A Spotify-origin share's extracted id equals its eventual resolvedSpotifyId,
     so its key is stable across the pending -> matched transition.
  3. Anything else (SoundCloud / Apple / unmatched)         -> `url:<normalized>`.
     Normalized sourceUrl (host+path, lowercased, scheme/www/query/trailing-slash
     stripped) so the same link shared twice maps to one key.

KNOWN EDGE (documented, accepted at friend-group scale): a NON-Spotify share
rated while still `pending` lands under its `url:` key; once the matcher resolves
it to Spotify its key becomes `spotify:`, so a pre-match rating on a SoundCloud/
Apple link does not carry across that transition. In practice ratings happen in
the feed AFTER matching (the card shows album art / aggregate), so this is rare.
A backfill that re-keys url-rated tracks to their resolved spotify id is the
fast-follow if it ever matters.
"""

from urllib.parse import urlsplit

from lambdas.common.models import extract_spotify_track_id


def normalize_source_url(url: str | None) -> str:
    """
    Reduce a source URL to a comparable identity: lowercased host + path with
    the scheme, a leading `www.`, any query string / fragment, and a trailing
    slash all stripped. Deterministic for the same link in any casing/format.
    """
    if not url:
        return ""
    parts = urlsplit(url.strip())
    host = (parts.netloc or "").lower()
    if host.startswith("www."):
        host = host[4:]
    path = parts.path.rstrip("/")
    # No scheme (bare "soundcloud.com/..") -> urlsplit puts it all in `path`;
    # still deterministic, just lands under path with an empty host.
    return f"{host}{path}".lower()


def derive_track_key(share: dict) -> str:
    """
    Map a share dict to its normalized track key (see module docstring for the
    precedence rules). Never raises -- an empty/garbage share yields a stable
    `url:` key over the empty string rather than blowing up a feed render.
    """
    spotify_id = share.get("resolvedSpotifyId")
    if isinstance(spotify_id, str) and spotify_id.strip():
        return f"spotify:{spotify_id.strip()}"

    source_url = (share.get("sourceUrl") or "").strip()
    lowered = source_url.lower()
    if "spotify.com" in lowered or lowered.startswith("spotify:"):
        extracted = extract_spotify_track_id(source_url)
        # extract_spotify_track_id returns its input unchanged when no id is
        # found; a real extraction differs from the full URL/URI.
        if extracted and extracted != source_url:
            return f"spotify:{extracted}"

    return f"url:{normalize_source_url(source_url)}"
