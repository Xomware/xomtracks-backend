"""
Backfill `sharerName` onto EXISTING xomtracks-shares rows from the local
macOS Contacts DB.

    python -m extractor.backfill_names

Runs on Dom's Mac ONLY. The Contacts (AddressBook) DBs are local and gated
by macOS Full Disk Access, which the extractor's Python has but a cloud
Lambda / remote agent does not -- so this can't run in AWS. It reads the
host's Contacts, then writes resolved names straight back to the
xomtracks-shares DynamoDB table via boto3 (needs AWS credentials on the
host, scoped to that one table).

Why direct DynamoDB and not the /shares/ingest endpoint: ingest is a
conditional put (attribute_not_exists(shareId)) -- re-posting an existing
share is an idempotent no-op and would NOT add a name to a row that's
already there. A backfill is an explicit UpdateItem on rows that exist.

Safe by default: only fills rows that have a `sharerHandle` and no
`sharerName` yet (outgoing/Dom-sent shares have no handle and are skipped).
`--dry-run` reports what would change without writing; `--force` overwrites
names already present.
"""

import argparse
import os
import sys

import boto3

from extractor.contacts import build_resolver
from extractor.logging_setup import get_logger

log = get_logger(__name__)

DEFAULT_TABLE_NAME = os.environ.get("SHARES_TABLE_NAME", "xomtracks-shares")
DEFAULT_REGION = os.environ.get("AWS_DEFAULT_REGION", "us-east-1")


def _scan_all(table) -> list[dict]:
    """Every item in the table, across all Scan pages."""
    items: list[dict] = []
    kwargs: dict = {}
    while True:
        res = table.scan(**kwargs)
        items.extend(res.get("Items", []))
        last_key = res.get("LastEvaluatedKey")
        if not last_key:
            break
        kwargs["ExclusiveStartKey"] = last_key
    return items


def backfill(table, resolve_name, *, dry_run: bool = False, force: bool = False) -> dict:
    """
    Resolve and persist `sharerName` for existing shares.

    Args:
        table: a boto3 DynamoDB Table resource for xomtracks-shares.
        resolve_name: `(handle) -> name | None` (extractor.contacts).
        dry_run: compute + report only, write nothing.
        force: overwrite an already-present sharerName.

    Returns a summary dict of counts.
    """
    items = _scan_all(table)

    scanned = len(items)
    skipped_no_handle = 0
    skipped_existing = 0
    unresolved = 0
    updated = 0
    would_update = 0

    for item in items:
        handle = item.get("sharerHandle")
        if not handle:
            skipped_no_handle += 1
            continue
        if item.get("sharerName") and not force:
            skipped_existing += 1
            continue

        name = resolve_name(handle)
        if not name:
            unresolved += 1
            continue

        if dry_run:
            would_update += 1
            log.info(f"[dry-run] would set sharerName={name!r} on shareId={item['shareId']}")
            continue

        table.update_item(
            Key={"shareId": item["shareId"]},
            UpdateExpression="SET sharerName = :n",
            ExpressionAttributeValues={":n": name},
        )
        updated += 1
        log.info(f"Set sharerName={name!r} on shareId={item['shareId']}")

    summary = {
        "scanned": scanned,
        "skipped_no_handle": skipped_no_handle,
        "skipped_existing": skipped_existing,
        "unresolved": unresolved,
        "updated": updated,
        "would_update": would_update,
        "dry_run": dry_run,
    }
    log.info(f"Backfill complete: {summary}")
    return summary


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Backfill sharerName on xomtracks-shares from local macOS Contacts")
    parser.add_argument("--table", default=DEFAULT_TABLE_NAME, help="DynamoDB table name")
    parser.add_argument("--region", default=DEFAULT_REGION, help="AWS region")
    parser.add_argument("--dry-run", action="store_true", help="Report what would change without writing")
    parser.add_argument("--force", action="store_true", help="Overwrite sharerName even if already set")
    parser.add_argument(
        "--db-path",
        action="append",
        default=None,
        help="Override AddressBook DB path (repeatable); defaults to this host's Contacts DBs",
    )
    args = parser.parse_args(argv)

    resolve_name = build_resolver(args.db_path)
    table = boto3.resource("dynamodb", region_name=args.region).Table(args.table)

    summary = backfill(table, resolve_name, dry_run=args.dry_run, force=args.force)
    print(
        f"\nBackfill summary ({'DRY RUN' if args.dry_run else 'LIVE'}):\n"
        f"  scanned            {summary['scanned']}\n"
        f"  updated            {summary['updated']}\n"
        f"  would_update       {summary['would_update']}\n"
        f"  unresolved         {summary['unresolved']}\n"
        f"  skipped_existing   {summary['skipped_existing']}\n"
        f"  skipped_no_handle  {summary['skipped_no_handle']}"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
