"""
Shared pytest fixtures for xomtracks-backend lambda tests.
"""

import pytest
import os
import sys
from unittest.mock import MagicMock

# Add repo root to path so `lambdas.*` / `extractor.*` imports resolve
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

# Set required env vars before any lambda modules are imported
_TEST_ENV_VARS = {
    "AWS_DEFAULT_REGION": "us-east-1",
    "DYNAMODB_KMS_ALIAS": "alias/xomtracks-kms-test",
    "SHARES_TABLE_NAME": "xomtracks-shares-test",
    "SHARES_DIRECTION_INDEX": "direction-messageDate-index",
    "SHARES_SHARER_INDEX": "sharerHandle-messageDate-index",
    "USERS_TABLE_NAME": "xomtracks-users-test",
    "APP_SERVICE_USER_EMAIL": "app@xomtracks.xomware.com",
    "RATINGS_TABLE_NAME": "xomtracks-ratings-test",
}
for key, value in _TEST_ENV_VARS.items():
    os.environ.setdefault(key, value)

# Pre-seed the lazy SSM parameter cache so constructing a Spotify() client
# in tests never hits real AWS -- see lambdas/common/spotify.py's docstring
# on why the module-object import pattern (not `from ssm_helpers import
# NAME`) matters for testability here.
from lambdas.common import ssm_helpers as _ssm_helpers  # noqa: E402

_ssm_helpers._ssm_cache.update({
    '/xomtracks/spotify/CLIENT_ID': 'test-spotify-client-id',
    '/xomtracks/spotify/CLIENT_SECRET': 'test-spotify-client-secret',
    '/xomtracks/api/API_SECRET_KEY': 'test-api-secret-key',
    '/xomtracks/ingest/BEARER_KEY': 'test-ingest-key',
    '/xomtracks/soundcloud/CLIENT_ID': 'test-soundcloud-client-id',
})


@pytest.fixture
def mock_context():
    """Mock AWS Lambda context."""
    context = MagicMock()
    context.function_name = "test-function"
    context.memory_limit_in_mb = 128
    context.invoked_function_arn = "arn:aws:lambda:us-east-1:123456789012:function:test-function"
    context.aws_request_id = "test-request-id"
    return context


def _base_api_gateway_event() -> dict:
    """Internal helper -- returns a fresh base event dict (avoids fixture-state sharing)."""
    return {
        "httpMethod": "GET",
        "path": "/test",
        "queryStringParameters": {},
        "headers": {"Content-Type": "application/json"},
        "body": None,
        "isBase64Encoded": False,
    }


@pytest.fixture
def api_gateway_event():
    """Base API Gateway event structure."""
    return _base_api_gateway_event()


@pytest.fixture
def authorized_event():
    """
    Build an API Gateway event WITH the Cognito authorizer context populated,
    mirroring what the native COGNITO_USER_POOLS authorizer places into
    requestContext.authorizer.claims after validating the caller's Cognito
    ID token.
    """

    def _make(email: str = "dom@example.com", **overrides) -> dict:
        event = _base_api_gateway_event()
        event["requestContext"] = {"authorizer": {"claims": {"email": email}}}
        event.update(overrides)
        return event

    return _make


@pytest.fixture
def ingest_event():
    """
    Build an API Gateway event for POST /shares/ingest -- carries the
    extractor's scoped bearer key in the Authorization header, NOT a user
    JWT / authorizer context.
    """

    def _make(bearer_key: str = "test-ingest-key", **overrides) -> dict:
        event = _base_api_gateway_event()
        event["httpMethod"] = "POST"
        event["headers"] = {"Content-Type": "application/json", "Authorization": f"Bearer {bearer_key}"}
        event["requestContext"] = {}
        event.update(overrides)
        return event

    return _make


@pytest.fixture
def public_event():
    """Build an API Gateway event for unauthenticated public routes -- no headers, no authorizer context."""

    def _make(**overrides) -> dict:
        event = _base_api_gateway_event()
        event["requestContext"] = {}
        event.update(overrides)
        return event

    return _make


@pytest.fixture
def sample_share():
    """A minimal, valid Share dict for reuse across tests."""
    return {
        "shareId": "share-1",
        "messageGuid": "guid-1",
        "direction": "in",
        "sharerHandle": "+13364042196",
        "sharerName": None,
        "chatId": "chat-1",
        "platform": "spotify",
        "sourceUrl": "https://open.spotify.com/track/4uLU6hMCjMI75M1A2tKUQC",
        "messageDate": 1753000000,
        "trackTitle": None,
        "trackArtist": None,
        "resolvedSpotifyId": None,
        "resolvedSpotifyUri": None,
        "matchStatus": "pending",
        "matchConfidence": None,
        "createdAt": "2026-07-20T00:00:00Z",
    }
