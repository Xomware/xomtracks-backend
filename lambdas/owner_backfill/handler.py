"""
XOMTRACKS Owner-Id Backfill  (self-serve foundation Phase 1B)
============================================================
One-shot, IDEMPOTENT backfill that stamps `ownerId` + `ownerDirection` onto
EVERY historical xomtracks-shares row that predates the multi-tenant re-key.
Before this runs the ~325 live rows have no owner; after it, every row is
owned by DEFAULT_OWNER_ID (Dom) and thus appears on the owner-scoped GSI-3
feed -- making the Phase 1C read cutover a no-op for Dom.

Idempotency + reversibility (the whole point of the expand->backfill->cutover
sequence on LIVE data):
  * Each write is a conditional UpdateItem `SET ownerId, ownerDirection` guarded
    by `attribute_not_exists(ownerId)`. A row already owned is left UNTOUCHED
    (ConditionalCheckFailed -> counted as skipped, not an error), so the script
    is safe to re-run any number of times.
  * `ownerDirection = <ownerId>#<direction>` -- the exact key
    shares_dynamo.compute_owner_direction derives on write, so backfilled rows
    are indistinguishable from freshly-ingested owned rows.
  * Rollback = restore the pre-backfill on-demand backup / PITR, or a
    remove-attributes Scan. The additive stamp loses no existing data.

The pure iteration core (`backfill`) is DynamoDB-boto3-only (no network, no
Spotify) and unit-tested against a moto table with mixed owned/unowned rows.
`run_backfill()` wires it to the real table; `python -m
lambdas.owner_backfill.handler` runs it against prod (migration-runner step).
"""

from typing import Any

from lambdas.common.logger import get_logger
from lambdas.common.shares_dynamo import compute_owner_direction

log = get_logger(__file__)

HANDLER = "owner_backfill"


def backfill(table, owner_id: str, *, dry_run: bool = False) -> dict:
    """
    Paginated Scan of `table`; conditionally stamp ownerId + ownerDirection on
    every row that has no ownerId yet.

    Args:
        table: a boto3 DynamoDB Table resource for xomtracks-shares.
        owner_id: the owner to stamp (DEFAULT_OWNER_ID / Dom's Cognito sub).
        dry_run: when True, only counts what WOULD be stamped -- no writes.

    Returns:
        {scanned, stamped, skipped, missing_direction} counts.
    """
    if not owner_id:
        raise ValueError("owner_id is required for the owner backfill")

    from botocore.exceptions import ClientError

    scanned = 0
    stamped = 0
    skipped = 0
    missing_direction = 0

    scan_kwargs: dict = {}
    while True:
        res = table.scan(**scan_kwargs)
        for item in res.get("Items", []):
            scanned += 1

            # Already owned -> nothing to do (keeps the re-run a no-op even
            # without hitting the conditional write).
            if item.get("ownerId"):
                skipped += 1
                continue

            direction = item.get("direction")
            owner_direction = compute_owner_direction(owner_id, direction)
            if owner_direction is None:
                # A row with no direction can't get a valid GSI-3 key -- record
                # it and skip rather than writing a malformed owner key.
                missing_direction += 1
                log.warning(f"Share {item.get('shareId')} has no direction; skipping owner stamp")
                continue

            if dry_run:
                stamped += 1
                continue

            try:
                table.update_item(
                    Key={"shareId": item["shareId"]},
                    UpdateExpression="SET ownerId = :o, ownerDirection = :od",
                    ConditionExpression="attribute_not_exists(ownerId)",
                    ExpressionAttributeValues={":o": owner_id, ":od": owner_direction},
                )
                stamped += 1
            except ClientError as err:
                if err.response.get("Error", {}).get("Code") == "ConditionalCheckFailedException":
                    # Raced/re-run -- row became owned between scan and write.
                    skipped += 1
                    continue
                log.error(f"Owner backfill update failed for {item.get('shareId')}: {err}")
                raise

        last_key = res.get("LastEvaluatedKey")
        if not last_key:
            break
        scan_kwargs["ExclusiveStartKey"] = last_key

    summary = {
        "scanned": scanned,
        "stamped": stamped,
        "skipped": skipped,
        "missing_direction": missing_direction,
    }
    log.info(f"Owner backfill complete: {summary}")
    return summary


def run_backfill(dry_run: bool = False) -> dict:
    """Wire `backfill` to the real xomtracks-shares table + DEFAULT_OWNER_ID."""
    import boto3

    from lambdas.common.constants import SHARES_TABLE_NAME, DEFAULT_OWNER_ID

    table = boto3.resource("dynamodb", region_name="us-east-1").Table(SHARES_TABLE_NAME)
    log.info(
        f"Owner backfill starting (table={SHARES_TABLE_NAME}, owner={DEFAULT_OWNER_ID}, "
        f"dry_run={dry_run})"
    )
    return backfill(table, DEFAULT_OWNER_ID, dry_run=dry_run)


def handler(event: dict, context: Any) -> dict:
    """Optional Lambda entry point (also runnable via `python -m`)."""
    dry_run = bool((event or {}).get("dry_run"))
    return run_backfill(dry_run=dry_run)


if __name__ == "__main__":
    import json
    import sys

    dry = "--dry-run" in sys.argv
    print(json.dumps(run_backfill(dry_run=dry), indent=2))
