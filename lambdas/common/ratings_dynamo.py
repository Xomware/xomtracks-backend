"""
XOMTRACKS Ratings DynamoDB Helpers
==================================
Whole-group song ratings. Any logged-in Cognito user may rate a song 1-5; a
song shows its aggregate ({avg, count}) plus the caller's own rating.

Table: xomtracks-ratings (additive -- a NEW table, not a change to shares).
  PK = trackKey    (normalized SONG identity, see track_key.derive_track_key)
  SK = raterEmail  (the Cognito email of the rater)
  attrs: trackKey, raterEmail, rating (int 1-5), updatedAt (epoch)

One item per (track, user) => a user has exactly ONE rating per song, and
re-rating is a plain upsert (PutItem overwrite). Because every rater row for a
track lives in the SAME partition (PK = trackKey), a single Query(PK=trackKey)
returns BOTH the full aggregate AND the caller's own row -- no second read, no
denormalized counter to drift. Aggregation is computed on read from the
partition rows.

WHY partition-query over denormalized counters: at friend-group scale a track
partition holds a handful of rows, so Query-and-fold is cheap, always correct,
and keeps the table to one item type (trivially testable). If the feed's
per-track query fan-out ever becomes a cost concern, a denormalized
`#AGG` (sum/count) row updated with atomic ADD on write is the drop-in
fast-follow -- see PLAN/brainstorm Option A.
"""

import time
from decimal import Decimal

import boto3
from boto3.dynamodb.conditions import Attr, Key

from lambdas.common.constants import AWS_DEFAULT_REGION, RATINGS_TABLE_NAME
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


RATING_MIN = 1
RATING_MAX = 5

# What an unrated track reports. avg 0 + count 0 is the "no ratings yet"
# sentinel (a real rating is always >= 1, so avg is never legitimately 0);
# the frontend should gate any "rated" UI on `count > 0`, not on avg.
EMPTY_AGGREGATE = {"avg": 0, "count": 0, "myRating": None}


def _validate_rating(rating: int) -> int:
    if not isinstance(rating, int) or isinstance(rating, bool):
        raise ValidationError(
            message="rating must be an integer 1-5",
            handler="ratings_dynamo",
            function="_validate_rating",
            field="rating",
        )
    if rating < RATING_MIN or rating > RATING_MAX:
        raise ValidationError(
            message=f"rating must be between {RATING_MIN} and {RATING_MAX}",
            handler="ratings_dynamo",
            function="_validate_rating",
            field="rating",
        )
    return rating


def get_track_rating_rows(track_key: str) -> list[dict]:
    """Every rater row for one track (its whole PK partition). Empty if unrated."""
    if not track_key:
        return []
    try:
        table = _get_dynamodb().Table(RATINGS_TABLE_NAME)
        items: list[dict] = []
        kwargs = {"KeyConditionExpression": Key("trackKey").eq(track_key)}
        while True:
            res = table.query(**kwargs)
            items.extend(res.get("Items", []))
            last_key = res.get("LastEvaluatedKey")
            if not last_key:
                break
            kwargs["ExclusiveStartKey"] = last_key
        return items
    except Exception as err:
        log.error(f"Get track rating rows failed: {err}")
        raise DynamoDBError(message=str(err), function="get_track_rating_rows", table=RATINGS_TABLE_NAME)


def aggregate_rows(rows: list[dict], caller_email: str | None) -> dict:
    """
    Fold a track's rater rows into {avg, count, myRating}. `avg` is rounded to
    two decimals; `myRating` is the caller's own int rating (or None). Pure --
    no I/O -- so the same fold serves both the single-set path and the batch
    feed-enrichment path off one Query.
    """
    count = len(rows)
    if count == 0:
        return dict(EMPTY_AGGREGATE)

    total = 0
    my_rating = None
    for row in rows:
        value = int(row.get("rating", 0))
        total += value
        if caller_email is not None and row.get("raterEmail") == caller_email:
            my_rating = value

    return {"avg": round(total / count, 2), "count": count, "myRating": my_rating}


def set_rating(track_key: str, rater_email: str, rating: int) -> dict:
    """
    Upsert the caller's rating for a track, then return the fresh aggregate
    {avg, count, myRating} for that track (a Query of the partition after the
    write -- so the returned counts already include this rating).
    """
    if not track_key:
        raise ValidationError(
            message="trackKey must not be empty",
            handler="ratings_dynamo",
            function="set_rating",
            field="trackKey",
        )
    if not rater_email:
        raise ValidationError(
            message="rater identity is required",
            handler="ratings_dynamo",
            function="set_rating",
            field="raterEmail",
        )
    _validate_rating(rating)

    now = int(time.time())
    try:
        table = _get_dynamodb().Table(RATINGS_TABLE_NAME)
        table.put_item(
            Item={
                "trackKey": track_key,
                "raterEmail": rater_email,
                "rating": Decimal(int(rating)),
                "updatedAt": now,
            }
        )
    except Exception as err:
        log.error(f"Set rating failed: {err}")
        raise DynamoDBError(message=str(err), function="set_rating", table=RATINGS_TABLE_NAME)

    rows = get_track_rating_rows(track_key)
    return aggregate_rows(rows, rater_email)


def batch_ratings_for_track_keys(track_keys: set[str], caller_email: str | None) -> dict[str, dict]:
    """
    Aggregate ratings for a whole page of tracks at once. Each UNIQUE trackKey
    is queried exactly once (dedup up front) and its rows serve both the
    aggregate and the caller's myRating in a single read. Returns a map
    trackKey -> {avg, count, myRating}; keys with no ratings are omitted (the
    caller substitutes EMPTY_AGGREGATE).
    """
    wanted = {k for k in track_keys if k}
    result: dict[str, dict] = {}
    for track_key in wanted:
        rows = get_track_rating_rows(track_key)
        if rows:
            result[track_key] = aggregate_rows(rows, caller_email)
    return result


def list_ratings_for_rater(rater_email: str) -> list[dict]:
    """
    Every rating row the given caller has made, across ALL tracks/directions.
    Each row is {trackKey, raterEmail, rating, updatedAt}.

    There is no GSI on raterEmail (the table is keyed for the per-track
    aggregate read); a member's own ratings are a low-frequency personal query
    ("My Rated" screen), so a filtered Scan is the right tool -- same rationale
    as scan_shares_by_match_status / scan_shares_by_normalized_handles. Returns
    every matching row across all Scan pages.
    """
    if not rater_email:
        return []
    try:
        table = _get_dynamodb().Table(RATINGS_TABLE_NAME)
        items: list[dict] = []
        kwargs = {"FilterExpression": Attr("raterEmail").eq(rater_email)}
        while True:
            res = table.scan(**kwargs)
            items.extend(res.get("Items", []))
            last_key = res.get("LastEvaluatedKey")
            if not last_key:
                break
            kwargs["ExclusiveStartKey"] = last_key
        return items
    except Exception as err:
        log.error(f"List ratings for rater failed: {err}")
        raise DynamoDBError(message=str(err), function="list_ratings_for_rater", table=RATINGS_TABLE_NAME)


def enrich_shares_with_ratings(shares: list[dict], caller_email: str | None) -> list[dict]:
    """
    Attach `trackKey` + `rating` ({avg, count, myRating}) to every share in a
    page IN PLACE, so the feed renders ratings with no extra client round trip.
    Batch-loads ratings for the page's unique trackKeys (one query per distinct
    song, not per share). Returns the same list for chaining.
    """
    if not shares:
        return shares

    for share in shares:
        share["trackKey"] = derive_track_key(share)

    track_keys = {share["trackKey"] for share in shares}
    # Ratings are an ENHANCEMENT of the feed, never load-bearing for it: if the
    # ratings table read fails, degrade to empty aggregates and let the feed
    # render rather than 500 the whole page. The error is logged for repair.
    try:
        aggregates = batch_ratings_for_track_keys(track_keys, caller_email)
    except Exception as err:
        log.error(f"Rating enrichment failed (feed degraded to unrated): {err}")
        aggregates = {}

    for share in shares:
        share["rating"] = aggregates.get(share["trackKey"], dict(EMPTY_AGGREGATE))
    return shares
