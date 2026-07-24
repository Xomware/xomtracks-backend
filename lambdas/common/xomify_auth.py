"""
XOMTRACKS <- XOMIFY token verification (WS-AUTH)
================================================
xomify is the sole frontend and authenticates via a homegrown HS256 JWT --
claims `email` (the caller's Spotify email) + `userId` (their Spotify id),
signed HS256 with the secret at SSM `/xomify/api/API_SECRET_KEY`. There is NO
Cognito. This module lets xomtracks' authed handlers validate that token
IN-HANDLER (the same pattern POST /shares/ingest already uses for its SSM
bearer key), so the API Gateway routes can drop the Cognito authorizer to
`NONE` without any custom-authorizer or shared-module work.

Identity is keyed on the NORMALIZED (lowercased) email everywhere downstream --
it is the one stable id common to both apps (see PLAN.md WS-AUTH). The token
and the signing key are never logged.
"""

import jwt

from lambdas.common import ssm_helpers
from lambdas.common.errors import AuthorizationError
from lambdas.common.logger import get_logger
from lambdas.common.utility_helpers import get_bearer_token

log = get_logger(__file__)

HANDLER = "xomify_auth"

JWT_ALGORITHM = "HS256"


def verify_xomify_token(event: dict) -> dict:
    """
    Validate the caller's xomify HS256 JWT from the `Authorization: Bearer
    <jwt>` header and return the verified identity.

    Returns:
        {"email": <lowercased>, "userId": <str>} -- the normalized email is the
        ownerId every downstream consumer keys by.

    Raises:
        AuthorizationError (HTTP 401) on ANY failure -- missing/malformed
        header, bad signature, expired/absent `exp`, or a missing/empty
        `email`/`userId` claim. The failure reason is logged but never leaked
        to the caller.
    """
    token = get_bearer_token(event)
    if not token:
        raise AuthorizationError(
            message="Missing bearer token",
            handler=HANDLER,
            function="verify_xomify_token",
        )

    try:
        payload = jwt.decode(
            token,
            key=ssm_helpers.XOMIFY_API_SECRET_KEY,
            algorithms=[JWT_ALGORITHM],
            # Enforce that an expiry is PRESENT (PyJWT verifies it once present;
            # `require` additionally rejects a token that omits it outright).
            options={"require": ["exp"], "verify_exp": True},
        )
    except jwt.PyJWTError as err:
        # ExpiredSignatureError / InvalidSignatureError / DecodeError /
        # MissingRequiredClaimError all land here -- collapse to a single 401.
        log.warning("xomify token rejected: %s", err.__class__.__name__)
        raise AuthorizationError(
            message="Invalid or expired token",
            handler=HANDLER,
            function="verify_xomify_token",
        ) from err

    email = payload.get("email")
    user_id = payload.get("userId")
    if not isinstance(email, str) or not email.strip():
        raise AuthorizationError(
            message="Token missing required claim: email",
            handler=HANDLER,
            function="verify_xomify_token",
        )
    if not user_id or not str(user_id).strip():
        raise AuthorizationError(
            message="Token missing required claim: userId",
            handler=HANDLER,
            function="verify_xomify_token",
        )

    return {"email": email.strip().lower(), "userId": str(user_id)}
