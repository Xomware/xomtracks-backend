"""
POST /ingest-tokens/revoke -- revoke one of the caller's ingest tokens (authed).
================================================================================
Self-serve foundation Phase 3. A xomify-authed caller revokes an ingest token
they own -- identified EITHER by `tokenHash` (the non-secret id returned at mint)
OR by presenting the plaintext `token`. Revocation is SCOPED to the caller's
ownerId (their normalized email -- WS-AUTH): a user can only revoke their own
token. Revoking a token
that is missing or owned by someone else returns 404 (indistinguishable, so a
caller can't probe for other users' token hashes) and leaves it live.

After revoke, the token no longer authenticates ingest (resolve_owner returns
None), so the corresponding extractor is cut off immediately.
"""

from typing import Any

from pydantic import ValidationError as PydanticValidationError

from lambdas.common import ingest_tokens
from lambdas.common.errors import ValidationError, handle_errors
from lambdas.common.logger import get_logger
from lambdas.common.models import RevokeIngestTokenRequest
from lambdas.common.utility_helpers import get_caller_owner, parse_body, success_response

log = get_logger(__file__)

HANDLER = "ingesttokens_revoke"


@handle_errors(HANDLER)
def handler(event: dict, context: Any) -> dict:
    # Authed route -- 401 if the caller's xomify token is missing/invalid. The
    # verified email is the ownerId revocation is scoped to.
    owner_id = get_caller_owner(event)

    body = parse_body(event)
    try:
        req = RevokeIngestTokenRequest(**body)
    except PydanticValidationError as err:
        raise ValidationError(
            message=f"Invalid revoke-ingest-token payload: {err}",
            handler=HANDLER,
            function="handler",
        ) from err

    token_hash = req.resolve_token_hash()
    if not token_hash:
        raise ValidationError(
            message="Provide either tokenHash or token to revoke.",
            handler=HANDLER,
            function="handler",
            field="tokenHash",
        )

    # NotFoundError (404) when the token is missing or not owned by the caller.
    result = ingest_tokens.revoke_token(owner_id, token_hash)

    log.info(f"Revoked ingest token owner={owner_id} tokenHash={token_hash}")

    return success_response(result)
