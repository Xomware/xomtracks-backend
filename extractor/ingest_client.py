"""
Pushes a single share dict to POST /shares/ingest, authed via the scoped
SSM bearer key (NOT the per-user JWT -- the extractor has no user
identity, see lambdas/shares_ingest/handler.py).

READ-ONLY / one-way: this module only ever POSTs share records. It never
reads anything back from the API beyond the HTTP status of its own push.
"""

import requests

from extractor.logging_setup import get_logger

log = get_logger(__name__)

DEFAULT_TIMEOUT_SECONDS = 10


def push_share(
    share: dict,
    ingest_url: str,
    bearer_key: str,
    http_post=requests.post,
    timeout: int = DEFAULT_TIMEOUT_SECONDS,
) -> bool:
    """
    POST one share to the ingest endpoint.

    Returns:
        True on any 2xx response (including the idempotent "already
        exists" case -- that's a successful push, not a failure). False on
        any non-2xx response OR transport-level exception (host asleep,
        DNS failure, timeout, etc.) -- the caller (run.py) is expected to
        NOT advance the watermark past a failed push, so it retries on the
        next scan.
    """
    headers = {"Authorization": f"Bearer {bearer_key}", "Content-Type": "application/json"}
    try:
        response = http_post(ingest_url, json=share, headers=headers, timeout=timeout)
    except Exception as err:
        log.warning(f"Ingest push failed (transport error) for guid={share.get('messageGuid')}: {err}")
        return False

    if 200 <= response.status_code < 300:
        return True

    log.warning(
        f"Ingest push failed ({response.status_code}) for guid={share.get('messageGuid')}: {response.json()}"
    )
    return False


def report_run(
    run_url: str,
    bearer_key: str,
    summary: dict,
    http_post=requests.post,
    timeout: int = DEFAULT_TIMEOUT_SECONDS,
) -> bool:
    """
    POST a compact per-scan summary to /ingest/run (telemetry only). Body:
    {scanned, ingested, newWatermark?, durationMs?}. STRICTLY best-effort --
    returns False (never raises) on any error; a telemetry miss must never
    affect the scan or its watermark. No-ops when `run_url` is falsy.
    """
    if not run_url:
        return False
    headers = {"Authorization": f"Bearer {bearer_key}", "Content-Type": "application/json"}
    try:
        response = http_post(run_url, json=summary, headers=headers, timeout=timeout)
    except Exception as err:  # noqa: BLE001 -- telemetry is never fatal
        log.warning(f"Run report failed (transport error): {err}")
        return False
    if 200 <= response.status_code < 300:
        return True
    log.warning(f"Run report failed ({response.status_code})")
    return False
