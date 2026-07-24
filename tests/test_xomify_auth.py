"""
RED-before-GREEN: xomify_auth.verify_xomify_token + utility_helpers.get_caller_owner.

WS-AUTH re-bases xomtracks identity onto xomify's homegrown HS256 JWT (claims
`email` + `userId`, signed with the secret at SSM /xomify/api/API_SECRET_KEY).
verify_xomify_token validates that token IN-HANDLER and returns the normalized
(lowercased) email + userId; get_caller_owner returns the email as the ownerId.
Every failure mode maps to a 401 (AuthorizationError, status 401).
"""

import time

import jwt
import pytest

from conftest import XOMIFY_TEST_SECRET, make_xomify_token
from lambdas.common.errors import AuthorizationError
from lambdas.common.utility_helpers import get_caller_owner
from lambdas.common.xomify_auth import verify_xomify_token


def _event(token: str | None) -> dict:
    headers = {"Content-Type": "application/json"}
    if token is not None:
        headers["Authorization"] = f"Bearer {token}"
    return {"headers": headers, "requestContext": {}}


class TestVerifyXomifyToken:
    def test_valid_token_returns_normalized_identity(self):
        event = _event(make_xomify_token(email="Dom@Example.COM", user_id="spot-123"))
        identity = verify_xomify_token(event)
        assert identity == {"email": "dom@example.com", "userId": "spot-123"}

    def test_get_caller_owner_returns_lowercased_email(self):
        event = _event(make_xomify_token(email="MiXeD@Case.io"))
        assert get_caller_owner(event) == "mixed@case.io"

    def test_expired_token_is_401(self):
        event = _event(make_xomify_token(exp_offset=-60))  # already expired
        with pytest.raises(AuthorizationError) as exc:
            verify_xomify_token(event)
        assert exc.value.status == 401

    def test_bad_signature_is_401(self):
        # Signed with a DIFFERENT secret than the one verify uses.
        bad = make_xomify_token(secret="not-the-real-secret")
        with pytest.raises(AuthorizationError) as exc:
            verify_xomify_token(_event(bad))
        assert exc.value.status == 401

    def test_missing_exp_is_401(self):
        # No exp claim at all -> options={"require": ["exp"]} rejects it.
        token = jwt.encode({"email": "a@b.com", "userId": "x"}, XOMIFY_TEST_SECRET, algorithm="HS256")
        token = token.decode("utf-8") if isinstance(token, bytes) else token
        with pytest.raises(AuthorizationError) as exc:
            verify_xomify_token(_event(token))
        assert exc.value.status == 401

    def test_missing_email_claim_is_401(self):
        token = jwt.encode(
            {"userId": "x", "exp": int(time.time()) + 3600},
            XOMIFY_TEST_SECRET, algorithm="HS256",
        )
        token = token.decode("utf-8") if isinstance(token, bytes) else token
        with pytest.raises(AuthorizationError) as exc:
            verify_xomify_token(_event(token))
        assert exc.value.status == 401

    def test_empty_email_claim_is_401(self):
        token = jwt.encode(
            {"email": "  ", "userId": "x", "exp": int(time.time()) + 3600},
            XOMIFY_TEST_SECRET, algorithm="HS256",
        )
        token = token.decode("utf-8") if isinstance(token, bytes) else token
        with pytest.raises(AuthorizationError) as exc:
            verify_xomify_token(_event(token))
        assert exc.value.status == 401

    def test_missing_userid_claim_is_401(self):
        token = jwt.encode(
            {"email": "a@b.com", "exp": int(time.time()) + 3600},
            XOMIFY_TEST_SECRET, algorithm="HS256",
        )
        token = token.decode("utf-8") if isinstance(token, bytes) else token
        with pytest.raises(AuthorizationError) as exc:
            verify_xomify_token(_event(token))
        assert exc.value.status == 401

    def test_missing_bearer_is_401(self):
        with pytest.raises(AuthorizationError) as exc:
            verify_xomify_token(_event(None))
        assert exc.value.status == 401

    def test_malformed_token_is_401(self):
        with pytest.raises(AuthorizationError) as exc:
            verify_xomify_token(_event("not-a-jwt"))
        assert exc.value.status == 401
