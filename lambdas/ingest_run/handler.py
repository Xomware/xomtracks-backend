"""
POST /ingest/run -- record a compact per-scan run summary (ingest-token authed).
================================================================================
The extractor POSTs this once after each scan cycle: how many messages it
scanned, how many new shares it ingested, its new watermark, and how long it
took. Authenticated by the SAME ingest bearer as /shares/ingest (dual-accept:
the legacy SSM key -> Dom, or a per-user token -> that owner), so the run is
attributed to the right owner.

Telemetry only -- backs the admin Extractor-status "recent runs" feed. Recording
is best-effort (never 500s on a telemetry-table hiccup); the response reports
whether it landed.
"""

from typing import Any

from pydantic import ValidationError as PydanticValidationError

from lambdas.common import ingest_runs, ssm_helpers
from lambdas.common.errors import ValidationError, handle_errors
from lambdas.common.logger import get_logger
from lambdas.common.models import IngestRunRequest
from lambdas.common.utility_helpers import parse_body, resolve_ingest_owner, success_response

log = get_logger(__file__)

HANDLER = "ingest_run"


@handle_errors(HANDLER)
def handler(event: dict, context: Any) -> dict:
    # 401 if the ingest bearer is missing/invalid. Resolves to the owner the
    # run is attributed to (per-token owner, or Dom for the legacy SSM key).
    owner_id = resolve_ingest_owner(event, ssm_helpers.INGEST_BEARER_KEY)

    body = parse_body(event)
    try:
        req = IngestRunRequest(**body)
    except PydanticValidationError as err:
        raise ValidationError(
            message=f"Invalid ingest-run payload: {err}",
            handler=HANDLER,
            function="handler",
        ) from err

    recorded = ingest_runs.record_run(
        owner_id,
        scanned=req.scanned,
        ingested=req.ingested,
        new_watermark=req.newWatermark,
        duration_ms=req.durationMs,
    )

    log.info(
        f"Ingest run owner={owner_id} scanned={req.scanned} ingested={req.ingested} recorded={recorded}"
    )

    return success_response({"recorded": recorded})
