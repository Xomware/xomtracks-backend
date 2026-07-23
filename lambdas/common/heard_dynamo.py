"""
XOMTRACKS Heard (Listen-Tracking) DynamoDB Helpers
==================================================
Per-(track, user) LISTEN state -- the sibling of ratings_dynamo. Any logged-in
Cognito user marks a song heard/unheard; the flag follows the SONG across all
of its share instances because it is keyed by the normalized trackKey.

Table: xomtracks-heard (additive -- a NEW table, sibling to xomtracks-ratings).
  PK = trackKey    (normalized SONG identity, see track_key.derive_track_key)
  SK = raterEmail  (the Cognito email of the listener)
  attrs: trackKey, raterEmail, heard (bool), heardAt (epoch -- "when heard",
         set only while heard is True), updatedAt (epoch).

One item per (track, user) => a user has exactly ONE heard row per song, and
toggling is a plain upsert (PutItem overwrite). Unlike ratings there is no
aggregate: heard is a PER-CALLER boolean, so the feed enrichment reads only the
caller's own row for each of a page's tracks (default False when absent).

WHY per-key GetItem (mirrors ratings' per-key Query): at friend-group scale a
feed page holds a handful of distinct songs, so a GetItem per unique trackKey is
cheap, always correct, and needs no GSI. If a page's fan-out ever costs, a
BatchGetItem (up to 100 keys/call) is the drop-in fast-follow.
"""

import time

import boto3

from lambdas.common.constants import AWS_DEFAULT_REGION, HEARD_TABLE_NAME
from lambdas.common.errors import DynamoDBError, ValidationError
from lambdas.common.logger import get_logger
from lambdas.common.track_key import derive_track_key

log = get_logger(__file__)

_dynamodb = None


def _get_dynamodb():
    """
    Lazily create (and cache) the DynamoDB resource on FIRST USE rather than at
    import time. Deferring construction until a function actually runs keeps
    import order from resolving/leaking AWS credentials -- tests import this
    module freely and only bind to (mocked) AWS when they call a helper. Behavior
    is identical to a module-level resource for real Lambda invocations.
    """
    global _dynamodb
    if _dynamodb is None:
        _dynamodb = boto3.resource("dynamodb", region_name=AWS_DEFAULT_REGION)
    return _dynamodb


def set_heard(track_key: str, rater_email: str, heard: bool, heard_at: int | None = None) -> dict:
    """
    Upsert the caller's heard flag for a track. Returns
    {"trackKey", "heard", "heardAt"}. `heardAt` (epoch "when heard") is stored
    only while heard is True -- an unheard row carries no heardAt. `heard_at`
    lets the auto-heard cron persist the actual Spotify `played_at` time; it
    defaults to now for the interactive POST /heard/set path.
    """
    if not track_key:
        raise ValidationError(
            message="trackKey must not be empty",
            handler="heard_dynamo",
            function="set_heard",
            field="trackKey",
        )
    if not rater_email:
        raise ValidationError(
            message="rater identity is required",
            handler="heard_dynamo",
            function="set_heard",
            field="raterEmail",
        )

    now = int(time.time())
    heard_bool = bool(heard)
    item = {
        "trackKey": track_key,
        "raterEmail": rater_email,
        "heard": heard_bool,
        "updatedAt": now,
    }
    stored_heard_at = None
    if heard_bool:
        stored_heard_at = int(heard_at) if heard_at is not None else now
        item["heardAt"] = stored_heard_at

    try:
        table = _get_dynamodb().Table(HEARD_TABLE_NAME)
        table.put_item(Item=item)
    except Exception as err:
        log.error(f"Set heard failed: {err}")
        raise DynamoDBError(message=str(err), function="set_heard", table=HEARD_TABLE_NAME)

    return {"trackKey": track_key, "heard": heard_bool, "heardAt": stored_heard_at}


def _get_heard_item(track_key: str, rater_email: str) -> dict | None:
    """The caller's single heard row for one track, or None if never set."""
    try:
        table = _get_dynamodb().Table(HEARD_TABLE_NAME)
        res = table.get_item(Key={"trackKey": track_key, "raterEmail": rater_email})
        return res.get("Item")
    except Exception as err:
        log.error(f"Get heard item failed: {err}")
        raise DynamoDBError(message=str(err), function="_get_heard_item", table=HEARD_TABLE_NAME)


def caller_heard_map(track_keys: set[str], caller_email: str | None) -> dict[str, bool]:
    """
    Map trackKey -> True for every track the CALLER has heard, out of the given
    keys. Keys the caller has NOT heard (row absent or heard False) are OMITTED,
    so the enrichment/caller substitutes False. One GetItem per unique key.
    """
    result: dict[str, bool] = {}
    if not caller_email:
        return result
    for track_key in {k for k in track_keys if k}:
        item = _get_heard_item(track_key, caller_email)
        if item and item.get("heard"):
            result[track_key] = True
    return result


def enrich_shares_with_heard(shares: list[dict], caller_email: str | None) -> list[dict]:
    """
    Attach `heard` (bool = the CALLER's per-song heard state, default False) to
    every share in a page IN PLACE, so the feed can offer an "unheard" filter
    with no extra client round trip. Reuses each share's `trackKey` if the
    ratings enrichment already set it, else derives it. Returns the same list.

    Heard state is an ENHANCEMENT of the feed, never load-bearing: if the heard
    table read fails, degrade to all-unheard and let the feed render rather than
    500 the page. The error is logged for repair.
    """
    if not shares:
        return shares

    for share in shares:
        if not share.get("trackKey"):
            share["trackKey"] = derive_track_key(share)

    track_keys = {share["trackKey"] for share in shares}
    try:
        heard_map = caller_heard_map(track_keys, caller_email)
    except Exception as err:
        log.error(f"Heard enrichment failed (feed degraded to all-unheard): {err}")
        heard_map = {}

    for share in shares:
        share["heard"] = bool(heard_map.get(share["trackKey"], False))
    return shares
