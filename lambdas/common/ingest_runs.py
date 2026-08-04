"""
XOMTRACKS Ingest Runs
=====================
Compact per-scan telemetry the extractor POSTs after each cycle -- so the admin
Extractor-status view can show a recent-runs feed ("is it running, is it finding
things") rather than only the single last-scan timestamp.

Table: xomtracks-ingest-runs (constants.INGEST_RUNS_TABLE_NAME)
  PK = ownerId   (normalized email -- WS-AUTH owner)
  SK = runAt     (epoch seconds; query ScanIndexForward=False for recent-first)
  attrs: scanned, ingested, newWatermark (int|None), durationMs (int|None),
         expiresAt (epoch TTL = runAt + INGEST_RUNS_TTL_DAYS days).

Recording is ALWAYS best-effort: a telemetry write must never fail the
extractor's run report (let alone its scan). NO-OP when the table env is unset.
"""

import time

import boto3
from boto3.dynamodb.conditions import Key

from lambdas.common.constants import INGEST_RUNS_TABLE_NAME, INGEST_RUNS_TTL_DAYS
from lambdas.common.logger import get_logger

log = get_logger(__file__)

_dynamodb = None


def _get_dynamodb():
    global _dynamodb
    if _dynamodb is None:
        _dynamodb = boto3.resource("dynamodb")
    return _dynamodb


def _table():
    return _get_dynamodb().Table(INGEST_RUNS_TABLE_NAME)


def record_run(
    owner_id: str,
    scanned: int,
    ingested: int,
    new_watermark: int | None = None,
    duration_ms: int | None = None,
) -> bool:
    """
    Persist one run summary for `owner_id`. Best-effort: returns False (never
    raises) when the table isn't configured or the write fails -- the caller's
    run report/response must not depend on telemetry landing.
    """
    if not INGEST_RUNS_TABLE_NAME or not owner_id:
        return False
    now = int(time.time())
    item = {
        "ownerId": owner_id,
        "runAt": now,
        "scanned": int(scanned or 0),
        "ingested": int(ingested or 0),
        "expiresAt": now + INGEST_RUNS_TTL_DAYS * 86400,
    }
    if new_watermark is not None:
        item["newWatermark"] = int(new_watermark)
    if duration_ms is not None:
        item["durationMs"] = int(duration_ms)
    try:
        _table().put_item(Item=item)
        return True
    except Exception as err:  # noqa: BLE001 -- telemetry is never fatal
        log.warning(f"record_run failed for owner={owner_id}: {err}")
        return False


def list_runs_for_owner(owner_id: str, limit: int = 20) -> list[dict]:
    """
    The owner's most-recent runs (recent-first), each:
      {runAt, scanned, ingested, newWatermark?, durationMs?}
    Query on the PK with ScanIndexForward=False -- O(limit), scales with runs
    per owner, not the whole table. Fails CLOSED to [] on any error.
    """
    if not INGEST_RUNS_TABLE_NAME or not owner_id:
        return []
    try:
        res = _table().query(
            KeyConditionExpression=Key("ownerId").eq(owner_id),
            ScanIndexForward=False,
            Limit=max(1, limit),
        )
    except Exception as err:  # noqa: BLE001 -- non-critical, degrade to []
        log.warning(f"list_runs_for_owner failed for owner={owner_id}: {err}")
        return []

    runs: list[dict] = []
    for r in res.get("Items", []):
        runs.append(
            {
                "runAt": int(r["runAt"]),
                "scanned": int(r.get("scanned", 0)),
                "ingested": int(r.get("ingested", 0)),
                "newWatermark": int(r["newWatermark"]) if r.get("newWatermark") is not None else None,
                "durationMs": int(r["durationMs"]) if r.get("durationMs") is not None else None,
            }
        )
    return runs
