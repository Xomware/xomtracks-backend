"""
WS-AUTH ownerId migration: Cognito sub -> normalized email.
===========================================================
Re-stamps every xomtracks-shares row owned by Dom's OLD ownerId (his Cognito
`sub`, from before WS-AUTH) onto his NEW ownerId (his normalized email), and
rewrites the GSI-3 `ownerDirection` attribute (`<ownerId>#<direction>`) to
match -- PRESERVING each row's direction. WS-AUTH re-based identity from the
Cognito authorizer onto xomify's HS256 token, whose stable key is email, so
ownerId is now the email everywhere (see docs/features/xomtracks-xomify-merge/
PLAN.md WS-AUTH). There are ~325 shares, all one owner (Dom).

DO NOT RUN casually. Sequence (human-driven): apply infra -> merge backend ->
run this with --apply -> verify. Dry-run is the DEFAULT.

Safety / idempotency / reversibility
------------------------------------
  * DRY-RUN BY DEFAULT. Nothing is written unless you pass --apply.
  * Each write is a conditional UpdateItem guarded by
    `ownerId = :from` -- a row already re-stamped to the email is skipped
    (ConditionalCheckFailed -> counted, not an error), so re-running is a no-op.
  * `ownerDirection` is recomputed with the SAME rule the write path uses
    (shares_dynamo.compute_owner_direction), so migrated rows are
    indistinguishable from freshly-ingested owned rows. Direction is preserved.
  * Every changed shareId is logged.
  * Before/after owner-distribution counts are printed.
  * REVERSIBLE two ways:
      1. `--reverse` swaps FROM/TO (email -> sub), the exact inverse mapping.
      2. Restore the pre-migration on-demand backup / PITR (both exist) -- the
         re-stamp changes only ownerId + ownerDirection and loses no other data.

Usage (from the xomtracks-backend repo root)
--------------------------------------------
    python scripts/migrate_ownerid_to_email.py                 # dry-run (default)
    python scripts/migrate_ownerid_to_email.py --apply         # write sub -> email
    python scripts/migrate_ownerid_to_email.py --reverse --apply  # email -> sub (rollback)
    python scripts/migrate_ownerid_to_email.py --from <id> --to <id>  # override mapping

The FROM (old sub) default is the value pinned in config both here and in
xomtracks-infrastructure (`default_owner_id` was the Cognito sub before this
migration): f4e80448-2061-7059-0c26-d0fd91863568. The TO (new email) default is
the post-WS-AUTH constants.DEFAULT_OWNER_ID (Dom's normalized email). The script
first SCANS and prints the live owner distribution so you can CONFIRM the FROM
value actually exists in the data before applying.
"""

import argparse
import json
import os
import sys
from collections import Counter

# Repo root on the path so `lambdas.*` imports resolve when run as a script.
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from lambdas.common.logger import get_logger  # noqa: E402
from lambdas.common.shares_dynamo import compute_owner_direction  # noqa: E402

log = get_logger(__file__)

# Dom's OLD ownerId -- his Cognito sub, the value stamped on every legacy row
# and the pre-WS-AUTH `default_owner_id` in both constants.py and
# xomtracks-infrastructure/terraform/variables.tf. Confirmed from config, and
# re-confirmed against live data by the pre-flight scan below.
DEFAULT_FROM_SUB = "f4e80448-2061-7059-0c26-d0fd91863568"


def _owner_distribution(table) -> Counter:
    """Paginated scan -> Counter of ownerId (missing/empty -> '<unowned>')."""
    dist: Counter = Counter()
    scan_kwargs: dict = {"ProjectionExpression": "ownerId"}
    while True:
        res = table.scan(**scan_kwargs)
        for item in res.get("Items", []):
            dist[item.get("ownerId") or "<unowned>"] += 1
        last_key = res.get("LastEvaluatedKey")
        if not last_key:
            break
        scan_kwargs["ExclusiveStartKey"] = last_key
    return dist


def migrate(table, from_owner: str, to_owner: str, *, apply: bool) -> dict:
    """
    Re-stamp every row with ownerId == from_owner onto to_owner, rewriting
    ownerDirection to `<to_owner>#<direction>`.

    Args:
        table: a boto3 DynamoDB Table resource for xomtracks-shares.
        from_owner: the ownerId to migrate FROM (old Cognito sub by default).
        to_owner: the ownerId to migrate TO (normalized email by default).
        apply: when False (the DEFAULT), only counts what WOULD change -- no writes.

    Returns:
        {scanned, matched, migrated, skipped, missing_direction, apply} counts.
    """
    if not from_owner or not to_owner:
        raise ValueError("both --from and --to owner ids are required")
    if from_owner == to_owner:
        raise ValueError("--from and --to are identical; nothing to migrate")

    from botocore.exceptions import ClientError

    scanned = matched = migrated = skipped = missing_direction = 0

    scan_kwargs: dict = {}
    while True:
        res = table.scan(**scan_kwargs)
        for item in res.get("Items", []):
            scanned += 1
            if item.get("ownerId") != from_owner:
                continue
            matched += 1

            share_id = item.get("shareId")
            direction = item.get("direction")
            new_owner_direction = compute_owner_direction(to_owner, direction)
            if new_owner_direction is None:
                # No direction -> can't build a valid GSI-3 key. Record + skip
                # rather than write a malformed owner key.
                missing_direction += 1
                log.warning(f"Share {share_id} has no direction; skipping owner re-stamp")
                continue

            if not apply:
                migrated += 1
                log.info(f"[DRY-RUN] would re-stamp {share_id}: {from_owner} -> {to_owner} "
                         f"(ownerDirection -> {new_owner_direction})")
                continue

            try:
                table.update_item(
                    Key={"shareId": share_id},
                    UpdateExpression="SET ownerId = :to, ownerDirection = :od",
                    ConditionExpression="ownerId = :from",
                    ExpressionAttributeValues={
                        ":to": to_owner,
                        ":od": new_owner_direction,
                        ":from": from_owner,
                    },
                )
                migrated += 1
                log.info(f"Re-stamped {share_id}: {from_owner} -> {to_owner} "
                         f"(ownerDirection -> {new_owner_direction})")
            except ClientError as err:
                if err.response.get("Error", {}).get("Code") == "ConditionalCheckFailedException":
                    # Raced / already migrated between scan and write.
                    skipped += 1
                    continue
                log.error(f"Migration update failed for {share_id}: {err}")
                raise

        last_key = res.get("LastEvaluatedKey")
        if not last_key:
            break
        scan_kwargs["ExclusiveStartKey"] = last_key

    summary = {
        "scanned": scanned,
        "matched": matched,
        "migrated": migrated,
        "skipped": skipped,
        "missing_direction": missing_direction,
        "apply": apply,
    }
    log.info(f"ownerId migration complete: {summary}")
    return summary


def run(from_owner: str, to_owner: str, *, apply: bool) -> dict:
    """Wire migrate() to the real xomtracks-shares table + print before/after."""
    import boto3

    from lambdas.common.constants import SHARES_TABLE_NAME

    if not SHARES_TABLE_NAME:
        raise RuntimeError("SHARES_TABLE_NAME is empty -- set the env before running against prod")

    table = boto3.resource("dynamodb", region_name="us-east-1").Table(SHARES_TABLE_NAME)

    print(f"Table: {SHARES_TABLE_NAME}")
    print(f"Mapping: {from_owner!r} -> {to_owner!r}")
    print(f"Mode: {'APPLY (writing)' if apply else 'DRY-RUN (no writes)'}\n")

    before = _owner_distribution(table)
    print("Owner distribution BEFORE:")
    print(json.dumps(dict(before), indent=2))
    if before.get(from_owner, 0) == 0:
        print(f"\nWARNING: no rows currently owned by --from {from_owner!r}. "
              f"Nothing will migrate -- double-check the FROM value.\n")

    summary = migrate(table, from_owner, to_owner, apply=apply)

    after = _owner_distribution(table)
    print("\nOwner distribution AFTER:")
    print(json.dumps(dict(after), indent=2))

    print("\nSummary:")
    print(json.dumps(summary, indent=2))
    return summary


def main() -> None:
    parser = argparse.ArgumentParser(description="Migrate xomtracks-shares ownerId (Cognito sub -> email).")
    parser.add_argument("--apply", action="store_true",
                        help="Actually write the changes. Omit for a dry-run (the default).")
    parser.add_argument("--reverse", action="store_true",
                        help="Reverse the mapping (email -> sub) for rollback.")
    parser.add_argument("--from", dest="from_owner", default=None,
                        help=f"ownerId to migrate FROM (default: {DEFAULT_FROM_SUB}).")
    parser.add_argument("--to", dest="to_owner", default=None,
                        help="ownerId to migrate TO (default: constants.DEFAULT_OWNER_ID, Dom's email).")
    args = parser.parse_args()

    from lambdas.common.constants import DEFAULT_OWNER_ID

    from_owner = args.from_owner or DEFAULT_FROM_SUB
    to_owner = args.to_owner or DEFAULT_OWNER_ID

    if args.reverse:
        from_owner, to_owner = to_owner, from_owner

    run(from_owner, to_owner, apply=args.apply)


if __name__ == "__main__":
    main()
