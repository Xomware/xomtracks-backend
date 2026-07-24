"""
POST /shares/ingest - the extractor's push endpoint.

Auth: a scoped SSM bearer key (INGEST_BEARER_KEY), NOT the per-user JWT --
the extractor has no user identity, just a shared secret scoped to this one
route. Idempotent: shareId is derived from (messageGuid, sourceUrl), so a
conditional put makes re-ingesting the same share a no-op ("already
exists"), not a duplicate row. Every fresh ingest starts at
matchStatus=pending -- the async matcher (Phase 3) picks it up from there.
"""

from datetime import datetime, timezone
from typing import Any

from pydantic import ValidationError as PydanticValidationError

from lambdas.common import ssm_helpers
from lambdas.common.constants import DEFAULT_OWNER_ID
from lambdas.common.errors import ValidationError, handle_errors
from lambdas.common.logger import get_logger
from lambdas.common.models import ShareIngestRequest
from lambdas.common.shares_dynamo import (
    compute_owner_direction,
    derive_share_id,
    put_share_idempotent,
)
from lambdas.common.utility_helpers import parse_body, require_ingest_bearer_key, success_response

log = get_logger(__file__)

HANDLER = "shares_ingest"


@handle_errors(HANDLER)
def handler(event: dict, context: Any) -> dict:
    require_ingest_bearer_key(event, ssm_helpers.INGEST_BEARER_KEY)

    body = parse_body(event)
    try:
        req = ShareIngestRequest(**body)
    except PydanticValidationError as err:
        raise ValidationError(
            message=f"Invalid share payload: {err}",
            handler=HANDLER,
            function="handler",
        ) from err

    share_id = derive_share_id(req.messageGuid, req.sourceUrl)
    # Multi-tenant Phase 1: every NEW write is owner-stamped. Phase 1 resolves
    # every ingest to DEFAULT_OWNER_ID (Dom -- the extractor still uses the
    # single SSM bearer key); Phase 3 swaps this for the per-token owner.
    # ownerDirection is derived server-side so GSI-3's key can never drift.
    owner_id = DEFAULT_OWNER_ID
    share = {
        "shareId": share_id,
        "messageGuid": req.messageGuid,
        "direction": req.direction,
        "ownerId": owner_id,
        "ownerDirection": compute_owner_direction(owner_id, req.direction),
        "sharerHandle": req.sharerHandle,
        "sharerName": req.sharerName,
        "chatId": req.chatId,
        "platform": req.platform,
        "sourceUrl": req.sourceUrl,
        "messageDate": req.messageDate,
        "trackTitle": None,
        "trackArtist": None,
        "resolvedSpotifyId": None,
        "resolvedSpotifyUri": None,
        "matchStatus": "pending",
        "matchConfidence": None,
        "createdAt": datetime.now(timezone.utc).isoformat(),
    }

    item, created = put_share_idempotent(share)

    log.info(f"Ingest {'created' if created else 'already existed'}: shareId={share_id}")

    return success_response({**item, "created": created})
