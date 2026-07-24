"""
POST /admin/revoke-token {tokenHash} -- admin override revoke (admin-gated).
========================================================================
Revokes ANY user's ingest token by hash -- an admin override of the owner-scoped
POST /ingest-tokens/revoke. After revoke, that token no longer authenticates
ingest (resolve_owner returns None), cutting the corresponding extractor off
immediately.

Gated in-handler to the admin (Dom). 404 if the hash doesn't exist. Only the
non-secret tokenHash is accepted (the admin never knows the plaintext). See
docs/features/xomtracks-xomify-merge/PLAN.md WS6.
"""

from typing import Any

from pydantic import ValidationError as PydanticValidationError

from lambdas.common.errors import ValidationError, handle_errors
from lambdas.common.ingest_tokens import revoke_token_admin
from lambdas.common.logger import get_logger
from lambdas.common.models import AdminRevokeTokenRequest
from lambdas.common.utility_helpers import parse_body, require_admin, success_response

log = get_logger(__file__)

HANDLER = "admin_revoketoken"


@handle_errors(HANDLER)
def handler(event: dict, context: Any) -> dict:
    # 401 if not signed in, 403 if signed in but not the admin.
    require_admin(event)

    body = parse_body(event)
    try:
        req = AdminRevokeTokenRequest(**body)
    except PydanticValidationError as err:
        raise ValidationError(
            message=f"Invalid revoke-token payload: {err}",
            handler=HANDLER,
            function="handler",
            field="tokenHash",
        ) from err

    # NotFoundError (404) when the hash doesn't exist.
    result = revoke_token_admin(req.tokenHash)

    log.info(f"Admin revoked ingest token tokenHash={req.tokenHash}")

    return success_response(result)
