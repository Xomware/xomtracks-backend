"""
XOMTRACKS Utility Helpers
=========================
Common utilities for Lambda handlers. Ported from xomify-backend's
lambdas/common/utility_helpers.py via the xomforms-backend adaptation
(trimmed of Spotify-account/legacy-compat cruft; this is a fresh repo).
"""

import json
import decimal
from datetime import datetime, timezone
from typing import Any, Optional, Set

from lambdas.common.logger import get_logger

log = get_logger(__file__)


# ============================================
# JSON Encoding
# ============================================

class XomtracksJSONEncoder(json.JSONEncoder):
    """
    Custom JSON encoder that handles:
    - Decimal (from DynamoDB)
    - datetime objects
    - sets
    """

    def default(self, obj):
        if isinstance(obj, decimal.Decimal):
            if obj % 1 == 0:
                return int(obj)
            return float(obj)
        if isinstance(obj, datetime):
            return obj.isoformat()
        if isinstance(obj, set):
            return list(obj)
        return super().default(obj)


def json_dumps(obj: Any) -> str:
    """Serialize object to JSON string with custom encoder."""
    return json.dumps(obj, cls=XomtracksJSONEncoder)


# ============================================
# Request Parsing
# ============================================

def is_api_request(event: dict) -> bool:
    """Check if the event is from API Gateway."""
    return isinstance(event.get('body'), str)


def parse_body(event: dict) -> dict:
    """
    Parse the request body from an event.
    Handles both API Gateway (string) and direct invocation (dict).
    """
    body = event.get('body')

    if body is None:
        return {}

    if isinstance(body, str):
        try:
            return json.loads(body)
        except json.JSONDecodeError:
            log.warning("Failed to parse body as JSON")
            return {}

    return body if isinstance(body, dict) else {}


def get_query_params(event: dict) -> dict:
    """Get query string parameters from event."""
    return event.get('queryStringParameters') or {}


def get_path_params(event: dict) -> dict:
    """Get path parameters from event."""
    return event.get('pathParameters') or {}


def get_header(event: dict, name: str) -> Optional[str]:
    """
    Case-insensitive header lookup (API Gateway lower-cases some but not
    all headers depending on integration type -- normalize defensively).
    """
    headers = event.get('headers') or {}
    lname = name.lower()
    for key, value in headers.items():
        if key.lower() == lname:
            return value
    return None


def get_bearer_token(event: dict) -> Optional[str]:
    """Extract a bearer token from the Authorization header, if present."""
    auth = get_header(event, 'Authorization')
    if not auth or not auth.strip():
        return None
    parts = auth.strip().split(' ', 1)
    if len(parts) == 2 and parts[0].lower() == 'bearer':
        return parts[1].strip()
    return None


# ============================================
# Response Building
# ============================================

CORS_HEADERS = {
    "Access-Control-Allow-Origin": "*",
    "Access-Control-Allow-Headers": "Content-Type,Authorization,X-Amz-Date,X-Api-Key,X-Amz-Security-Token",
    "Access-Control-Allow-Methods": "GET,POST,PUT,DELETE,OPTIONS",
    "Content-Type": "application/json",
}


def success_response(body: Any, status_code: int = 200, is_api: bool = True) -> dict:
    """Build a successful Lambda response. Follows the {data, error, meta} shape."""
    envelope = {"data": body, "error": None, "meta": {}}
    return {
        "statusCode": status_code,
        "headers": CORS_HEADERS,
        "body": json_dumps(envelope) if is_api else envelope,
        "isBase64Encoded": False,
    }


def error_response(
    message: str,
    status_code: int = 500,
    is_api: bool = True,
    details: Optional[dict] = None,
) -> dict:
    """Build an error Lambda response. Follows the {data, error, meta} shape."""
    envelope = {
        "data": None,
        "error": {
            "message": message,
            "status": status_code,
            **(details or {}),
        },
        "meta": {},
    }

    return {
        "statusCode": status_code,
        "headers": CORS_HEADERS,
        "body": json_dumps(envelope) if is_api else envelope,
        "isBase64Encoded": False,
    }


# ============================================
# Input Validation
# ============================================

def validate_input(
    data: Optional[dict],
    required_fields: Set[str] = None,
    optional_fields: Set[str] = None,
) -> tuple[bool, Optional[str]]:
    """Validate input data has required fields and no extra fields."""
    required_fields = required_fields or set()
    optional_fields = optional_fields or set()

    if data is None:
        if required_fields:
            return False, f"Missing required fields: {required_fields}"
        return True, None

    if not isinstance(data, dict):
        return False, "Input must be a dictionary"

    data_keys = set(data.keys())
    allowed_keys = required_fields | optional_fields

    missing = required_fields - data_keys
    if missing:
        return False, f"Missing required fields: {missing}"

    if optional_fields:
        extra = data_keys - allowed_keys
        if extra:
            return False, f"Unexpected fields: {extra}"

    return True, None


def require_fields(data: dict, *fields: str) -> None:
    """
    Raise ValidationError if any required fields are missing.

    Usage:
        require_fields(body, 'sourceUrl', 'direction')
    """
    from lambdas.common.errors import ValidationError

    missing = [f for f in fields if f not in data or data[f] is None]
    if missing:
        raise ValidationError(
            message=f"Missing required fields: {', '.join(missing)}",
            field=missing[0],
        )


# ============================================
# Caller Identity Resolution (WS-AUTH: xomify HS256 token, in-handler)
# ============================================
# xomify is the sole frontend and signs a homegrown HS256 JWT (claims `email`
# + `userId`) with the secret at SSM `/xomify/api/API_SECRET_KEY`. There is NO
# Cognito authorizer any more -- the authed API Gateway routes are `NONE` and
# each handler validates the caller's Bearer token IN-HANDLER via
# xomify_auth.verify_xomify_token (mirroring how POST /shares/ingest already
# validates its SSM bearer key with the route set to NONE).
#
# Identity is keyed on the NORMALIZED (lowercased) email EVERYWHERE -- it is
# the ownerId shares are stamped/scoped by, the raterEmail on ratings/heard,
# and the admin check. `userId` (Spotify id) is available on the verified
# payload but is NOT the owner key. The extractor ingest route uses a
# *different* mechanism entirely (a scoped SSM bearer key / per-user ingest
# token, see resolve_ingest_owner below) -- it never carries a caller JWT.

def get_caller_owner(event: dict) -> str:
    """
    Resolve the caller's ownerId -- the NORMALIZED (lowercased) email from the
    verified xomify token. This is the single identity every authed handler
    keys on (owner stamping/scoping, ratings/heard raterEmail, admin check).

    Raises AuthorizationError (HTTP 401) on any token failure (missing,
    malformed, bad signature, expired, or missing email/userId claim).
    """
    from lambdas.common.xomify_auth import verify_xomify_token

    return verify_xomify_token(event)["email"]


# Back-compat alias: handlers historically read `get_caller_email` for the
# raterEmail / admin identity. Under WS-AUTH the caller email IS the ownerId
# (normalized), so this returns the same value as get_caller_owner.
def get_caller_email(event: dict) -> str:
    """Alias of get_caller_owner -- the caller's normalized email (== ownerId)."""
    return get_caller_owner(event)


def require_admin(event: dict) -> str:
    """
    Resolve the caller's verified email AND assert it is the configured admin
    (Dom). Gates the /admin/* routes on top of the in-handler xomify-token
    check -- any xomify user passes token verification, but only the admin may
    list/approve/deny link requests.

    Returns the admin email on success. Raises AuthorizationError (401) if
    there is no valid caller token, or ForbiddenError (403) if the caller is
    authenticated but is not the admin.
    """
    from lambdas.common.constants import ADMIN_EMAIL
    from lambdas.common.errors import ForbiddenError

    email = get_caller_email(event)
    if not ADMIN_EMAIL or email.strip().lower() != ADMIN_EMAIL.strip().lower():
        raise ForbiddenError(
            message="Admin access required",
            handler="utility_helpers",
            function="require_admin",
            reason="not_admin",
        )
    return email


def require_ingest_bearer_key(event: dict, expected_key: str) -> None:
    """
    Validate the extractor's scoped bearer key on POST /shares/ingest.

    Raises AuthorizationError (401) if missing or mismatched. Deliberately
    NOT the same code path as get_caller_email -- the extractor has no
    user identity, just a shared secret scoped to this one route.

    LEGACY (Phase 3): superseded by resolve_ingest_owner, which additionally
    resolves the OWNER of the ingest. Kept for back-compat / rollback; the
    ingest handler now calls resolve_ingest_owner. Retired at the Phase 4
    contract step.
    """
    from lambdas.common.errors import AuthorizationError

    token = get_bearer_token(event)
    if not token or not expected_key or token != expected_key:
        raise AuthorizationError(
            message="Missing or invalid ingest bearer key",
            handler="shares_ingest",
            function="require_ingest_bearer_key",
        )


def resolve_ingest_owner(event: dict, legacy_key: str) -> str:
    """
    Resolve the OWNER (Cognito sub) that a POST /shares/ingest request
    authenticates as -- the Phase 3 replacement for require_ingest_bearer_key.

    Dual-accept, checked in this order:
      1. LEGACY SSM bearer key -> DEFAULT_OWNER_ID (Dom). Checked FIRST (a
         constant-time compare, no DB read) so Dom's running extractor keeps
         working UNCHANGED and is immune to a tokens-table outage.
      2. Per-user ingest token -> its ownerId (hash the presented bearer, look
         it up; revoked/unknown -> no match).

    Raises AuthorizationError (401) only if NEITHER matches. The stamped owner
    flows straight into the share's ownerId / ownerDirection, closing the
    multi-tenant loop (Phase 1 stamped DEFAULT_OWNER_ID unconditionally).
    """
    import hmac

    from lambdas.common import ingest_tokens
    from lambdas.common.constants import DEFAULT_OWNER_ID
    from lambdas.common.errors import AuthorizationError

    token = get_bearer_token(event)
    if not token:
        raise AuthorizationError(
            message="Missing ingest bearer token",
            handler="shares_ingest",
            function="resolve_ingest_owner",
        )

    # 1. Legacy single SSM key -> Dom. Constant-time compare, no DB dependency.
    if legacy_key and hmac.compare_digest(token, legacy_key):
        return DEFAULT_OWNER_ID

    # 2. Per-user token -> its owner (None if unknown/revoked/lookup-failed).
    owner_id = ingest_tokens.resolve_owner(token)
    if owner_id:
        return owner_id

    raise AuthorizationError(
        message="Invalid or revoked ingest token",
        handler="shares_ingest",
        function="resolve_ingest_owner",
    )


# ============================================
# Date/Time Utilities
# ============================================

def get_timestamp() -> str:
    """Get current UTC timestamp in standard format."""
    return datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M:%S')


def get_iso_timestamp() -> str:
    """Get current UTC timestamp in ISO 8601 format (with Z suffix)."""
    return datetime.now(timezone.utc).isoformat().replace('+00:00', 'Z')
