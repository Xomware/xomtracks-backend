"""
POST /ingest-tokens/create -- mint a per-user extractor ingest token (authed).
==============================================================================
Self-serve foundation Phase 3. A xomify-authed caller mints an opaque ingest
token bound to their ownerId (their normalized email, from the verified xomify
token -- WS-AUTH). Only the token's SHA-256 HASH is stored; the PLAINTEXT is
returned in this response EXACTLY ONCE and is never recoverable afterwards --
the user copies it into their macOS Keychain (see extractor/README.md) so their
extractor authenticates as them and their ingested shares are stamped with
their ownerId.

A caller without a valid xomify token is refused (401), because we can't
attribute the token to an owner.
"""

from typing import Any

from pydantic import ValidationError as PydanticValidationError

from lambdas.common import ingest_tokens
from lambdas.common.errors import ValidationError, handle_errors
from lambdas.common.logger import get_logger
from lambdas.common.models import CreateIngestTokenRequest
from lambdas.common.utility_helpers import get_caller_owner, parse_body, success_response

log = get_logger(__file__)

HANDLER = "ingesttokens_create"


@handle_errors(HANDLER)
def handler(event: dict, context: Any) -> dict:
    # Authed route -- 401 if the caller's xomify token is missing/invalid. The
    # verified email IS the ownerId the minted token is bound to.
    owner_id = get_caller_owner(event)

    body = parse_body(event)
    try:
        req = CreateIngestTokenRequest(**body)
    except PydanticValidationError as err:
        raise ValidationError(
            message=f"Invalid create-ingest-token payload: {err}",
            handler=HANDLER,
            function="handler",
        ) from err

    minted = ingest_tokens.mint_token(owner_id, label=req.label)

    # The plaintext token is returned ONCE here and never logged.
    log.info(f"Minted ingest token for owner={owner_id} tokenHash={minted['tokenHash']}")

    return success_response(
        {
            "token": minted["token"],
            "tokenHash": minted["tokenHash"],
            "ownerId": minted["ownerId"],
            "createdAt": minted["createdAt"],
            "label": minted["label"],
        }
    )
