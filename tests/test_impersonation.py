"""
RED-before-GREEN: ADMIN-ONLY impersonation override.

When the REAL authenticated caller is the admin (email == ADMIN_EMAIL) and the
`?impersonate=<email>` query param is present, the EFFECTIVE owner/caller-email
used for Shares data scoping becomes the impersonated user's email. For
NON-admins the param is IGNORED entirely (no privilege escalation). The admin
GATE always authorizes on the REAL caller, so impersonating a non-admin never
drops admin rights and /admin/* routes still gate on the true identity.

Param contract: `?impersonate=<email>` (normalized: trimmed + lowercased; must
contain "@" or it is treated as absent).
"""

import json

import boto3
import pytest
from moto import mock_aws

from conftest import make_xomify_token
from lambdas.common.constants import (
    ADMIN_EMAIL,
    INGEST_TOKENS_TABLE_NAME,
    LINK_REQUESTS_TABLE_NAME,
    SHARES_DIRECTION_INDEX,
    SHARES_OWNER_DIRECTION_INDEX,
    SHARES_SHARER_INDEX,
    SHARES_TABLE_NAME,
    USERS_TABLE_NAME,
)

ADMIN = ADMIN_EMAIL  # dominickj.giordano@gmail.com
FRIEND = "friend@example.com"
VICTIM = "victim@example.com"


def _authed(email: str, impersonate: str | None = None, **qs) -> dict:
    """API Gateway event with a valid xomify token for `email`, optionally
    carrying the `?impersonate=` override in the query string."""
    params = dict(qs)
    if impersonate is not None:
        params["impersonate"] = impersonate
    return {
        "httpMethod": "GET",
        "path": "/test",
        "headers": {"Authorization": f"Bearer {make_xomify_token(email)}"},
        "body": None,
        "isBase64Encoded": False,
        "queryStringParameters": params or None,
        "requestContext": {},
    }


# ============================================================================
# Unit: the resolution helpers in utility_helpers
# ============================================================================

class TestResolutionHelpers:
    def test_admin_impersonation_changes_effective_owner(self):
        from lambdas.common.utility_helpers import get_caller_owner

        event = _authed(ADMIN, impersonate=FRIEND)
        assert get_caller_owner(event) == FRIEND

    def test_real_caller_email_ignores_impersonation(self):
        from lambdas.common.utility_helpers import get_real_caller_email

        event = _authed(ADMIN, impersonate=FRIEND)
        assert get_real_caller_email(event) == ADMIN

    def test_non_admin_impersonation_is_ignored(self):
        from lambdas.common.utility_helpers import get_caller_owner

        # A non-admin trying to impersonate the admin gets their OWN identity.
        event = _authed(FRIEND, impersonate=ADMIN)
        assert get_caller_owner(event) == FRIEND

    def test_impersonate_is_normalized_lowercased(self):
        from lambdas.common.utility_helpers import get_caller_owner

        event = _authed(ADMIN, impersonate="  Friend@Example.COM  ")
        assert get_caller_owner(event) == FRIEND

    def test_malformed_impersonate_value_ignored(self):
        from lambdas.common.utility_helpers import get_caller_owner

        # No "@" -> treated as absent, falls back to the real caller.
        event = _authed(ADMIN, impersonate="not-an-email")
        assert get_caller_owner(event) == ADMIN

    def test_no_param_returns_real_admin(self):
        from lambdas.common.utility_helpers import get_caller_owner

        assert get_caller_owner(_authed(ADMIN)) == ADMIN

    def test_require_admin_gates_on_real_caller_while_impersonating(self):
        from lambdas.common.utility_helpers import require_admin

        # Admin impersonating a non-admin still passes the admin gate.
        assert require_admin(_authed(ADMIN, impersonate=FRIEND)) == ADMIN

    def test_require_admin_rejects_non_admin_even_impersonating_admin(self):
        from lambdas.common.errors import ForbiddenError
        from lambdas.common.utility_helpers import require_admin

        with pytest.raises(ForbiddenError):
            require_admin(_authed(FRIEND, impersonate=ADMIN))

    def test_impersonation_stashed_on_event_for_audit(self):
        from lambdas.common.request_log import (
            CALLER_EMAIL_EVENT_KEY,
            IMPERSONATED_EMAIL_EVENT_KEY,
        )
        from lambdas.common.utility_helpers import get_caller_owner

        event = _authed(ADMIN, impersonate=FRIEND)
        get_caller_owner(event)
        # Real admin recorded as the actor; impersonated target recorded too.
        assert event[CALLER_EMAIL_EVENT_KEY] == ADMIN
        assert event[IMPERSONATED_EMAIL_EVENT_KEY] == FRIEND

    def test_no_impersonation_leaves_audit_key_absent(self):
        from lambdas.common.request_log import IMPERSONATED_EMAIL_EVENT_KEY
        from lambdas.common.utility_helpers import get_caller_owner

        event = _authed(ADMIN)
        get_caller_owner(event)
        assert IMPERSONATED_EMAIL_EVENT_KEY not in event


# ============================================================================
# Integration: /shares/list
# ============================================================================

def _create_shares_table():
    ddb = boto3.resource("dynamodb", region_name="us-east-1")
    ddb.create_table(
        TableName=SHARES_TABLE_NAME,
        KeySchema=[{"AttributeName": "shareId", "KeyType": "HASH"}],
        AttributeDefinitions=[
            {"AttributeName": "shareId", "AttributeType": "S"},
            {"AttributeName": "direction", "AttributeType": "S"},
            {"AttributeName": "messageDate", "AttributeType": "N"},
            {"AttributeName": "sharerHandle", "AttributeType": "S"},
            {"AttributeName": "ownerDirection", "AttributeType": "S"},
        ],
        GlobalSecondaryIndexes=[
            {
                "IndexName": SHARES_DIRECTION_INDEX,
                "KeySchema": [
                    {"AttributeName": "direction", "KeyType": "HASH"},
                    {"AttributeName": "messageDate", "KeyType": "RANGE"},
                ],
                "Projection": {"ProjectionType": "ALL"},
                "ProvisionedThroughput": {"ReadCapacityUnits": 5, "WriteCapacityUnits": 5},
            },
            {
                "IndexName": SHARES_SHARER_INDEX,
                "KeySchema": [
                    {"AttributeName": "sharerHandle", "KeyType": "HASH"},
                    {"AttributeName": "messageDate", "KeyType": "RANGE"},
                ],
                "Projection": {"ProjectionType": "ALL"},
                "ProvisionedThroughput": {"ReadCapacityUnits": 5, "WriteCapacityUnits": 5},
            },
            {
                "IndexName": SHARES_OWNER_DIRECTION_INDEX,
                "KeySchema": [
                    {"AttributeName": "ownerDirection", "KeyType": "HASH"},
                    {"AttributeName": "messageDate", "KeyType": "RANGE"},
                ],
                "Projection": {"ProjectionType": "ALL"},
                "ProvisionedThroughput": {"ReadCapacityUnits": 5, "WriteCapacityUnits": 5},
            },
        ],
        ProvisionedThroughput={"ReadCapacityUnits": 5, "WriteCapacityUnits": 5},
    )
    return ddb.Table(SHARES_TABLE_NAME)


@pytest.fixture
def scoped_shares():
    """Friend + victim each own one in-direction share; NO DEFAULT_OWNER_ID
    (admin) baseline rows, so the admin's own feed is empty and impersonation
    is unambiguous."""
    import time

    with mock_aws():
        table = _create_shares_table()
        now = int(time.time())
        for share_id, owner in (("f1", FRIEND), ("v1", VICTIM)):
            table.put_item(Item={
                "shareId": share_id, "messageGuid": f"g-{share_id}", "direction": "in",
                "sharerHandle": f"+{share_id}", "ownerId": owner,
                "ownerDirection": f"{owner}#in", "chatId": "c1", "platform": "spotify",
                "sourceUrl": f"url-{share_id}", "messageDate": now - 60,
                "matchStatus": "matched", "createdAt": "x",
            })
        yield table


class TestSharesListImpersonation:
    def test_admin_impersonating_sees_impersonated_users_feed(self, scoped_shares, monkeypatch, mock_context):
        import lambdas.shares_list.handler as h

        monkeypatch.setattr(h, "OWNER_SCOPING_ENABLED", True)
        event = _authed(ADMIN, impersonate=VICTIM, direction="in", window="all")
        body = json.loads(h.handler(event, mock_context)["body"])
        ids = {s["shareId"] for s in body["data"]["shares"]}
        assert ids == {"v1"}

    def test_admin_without_impersonation_sees_own_empty_feed(self, scoped_shares, monkeypatch, mock_context):
        import lambdas.shares_list.handler as h

        monkeypatch.setattr(h, "OWNER_SCOPING_ENABLED", True)
        event = _authed(ADMIN, direction="in", window="all")
        body = json.loads(h.handler(event, mock_context)["body"])
        assert body["data"]["shares"] == []

    def test_non_admin_impersonation_ignored_no_escalation(self, scoped_shares, monkeypatch, mock_context):
        import lambdas.shares_list.handler as h

        monkeypatch.setattr(h, "OWNER_SCOPING_ENABLED", True)
        # Friend (non-admin) tries to impersonate victim -> param ignored,
        # friend sees only their OWN feed, NOT victim's share.
        event = _authed(FRIEND, impersonate=VICTIM, direction="in", window="all")
        body = json.loads(h.handler(event, mock_context)["body"])
        ids = {s["shareId"] for s in body["data"]["shares"]}
        assert ids == {"f1"}
        assert "v1" not in ids


# ============================================================================
# Integration: /me/get + /admin/users
# ============================================================================

def _create_me_tables():
    ddb = boto3.resource("dynamodb", region_name="us-east-1")
    ddb.create_table(
        TableName=USERS_TABLE_NAME,
        KeySchema=[{"AttributeName": "email", "KeyType": "HASH"}],
        AttributeDefinitions=[{"AttributeName": "email", "AttributeType": "S"}],
        BillingMode="PAY_PER_REQUEST",
    )
    ddb.create_table(
        TableName=LINK_REQUESTS_TABLE_NAME,
        KeySchema=[{"AttributeName": "requestId", "KeyType": "HASH"}],
        AttributeDefinitions=[{"AttributeName": "requestId", "AttributeType": "S"}],
        BillingMode="PAY_PER_REQUEST",
    )
    ddb.create_table(
        TableName=INGEST_TOKENS_TABLE_NAME,
        KeySchema=[{"AttributeName": "tokenHash", "KeyType": "HASH"}],
        AttributeDefinitions=[{"AttributeName": "tokenHash", "AttributeType": "S"}],
        BillingMode="PAY_PER_REQUEST",
    )
    _create_shares_table()
    return ddb


@pytest.fixture
def me_tables():
    with mock_aws():
        from lambdas.common import user_directory

        user_directory._last_written.clear()
        yield _create_me_tables()


class TestMeGetImpersonation:
    def test_impersonation_returns_impersonated_identity_non_admin(self, me_tables, mock_context):
        from lambdas.me_get.handler import handler

        # Admin steps through the app AS friend: /me/get reflects the friend,
        # isAdmin False so the frontend shows the setup card etc.
        event = _authed(ADMIN, impersonate=FRIEND)
        data = json.loads(handler(event, mock_context)["body"])["data"]
        assert data["email"] == FRIEND
        assert data["isAdmin"] is False

    def test_admin_without_impersonation_is_admin(self, me_tables, mock_context):
        from lambdas.me_get.handler import handler

        data = json.loads(handler(_authed(ADMIN), mock_context)["body"])["data"]
        assert data["email"] == ADMIN
        assert data["isAdmin"] is True

    def test_non_admin_cannot_forge_admin_via_impersonation(self, me_tables, mock_context):
        from lambdas.me_get.handler import handler

        event = _authed(FRIEND, impersonate=ADMIN)
        data = json.loads(handler(event, mock_context)["body"])["data"]
        assert data["email"] == FRIEND
        assert data["isAdmin"] is False


class TestAdminGateUnderImpersonation:
    def test_admin_route_works_while_impersonating(self, me_tables, mock_context):
        from lambdas.admin_users.handler import handler

        # Admin impersonating a non-admin still passes the /admin/* gate.
        event = _authed(ADMIN, impersonate=FRIEND)
        assert handler(event, mock_context)["statusCode"] == 200

    def test_non_admin_still_forbidden_when_impersonating_admin(self, me_tables, mock_context):
        from lambdas.admin_users.handler import handler

        event = _authed(FRIEND, impersonate=ADMIN)
        assert handler(event, mock_context)["statusCode"] == 403


# ============================================================================
# Audit: request log records the impersonation
# ============================================================================

class TestRequestLogAudit:
    def test_record_from_event_captures_impersonation(self, monkeypatch):
        from lambdas.common import request_log
        from lambdas.common.request_log import (
            CALLER_EMAIL_EVENT_KEY,
            IMPERSONATED_EMAIL_EVENT_KEY,
        )

        table_name = "xomtracks-request-log-test"
        with mock_aws():
            ddb = boto3.resource("dynamodb", region_name="us-east-1")
            ddb.create_table(
                TableName=table_name,
                KeySchema=[{"AttributeName": "id", "KeyType": "HASH"}],
                AttributeDefinitions=[{"AttributeName": "id", "AttributeType": "S"}],
                BillingMode="PAY_PER_REQUEST",
            )
            monkeypatch.setattr("lambdas.common.constants.REQUEST_LOG_TABLE_NAME", table_name)

            event = {
                "httpMethod": "GET",
                "path": "/shares/list",
                CALLER_EMAIL_EVENT_KEY: ADMIN,
                IMPERSONATED_EMAIL_EVENT_KEY: FRIEND,
            }
            request_log.record_from_event(event, {"statusCode": 200})

            it = ddb.Table(table_name).scan()["Items"][0]
            assert it["email"] == ADMIN
            assert it["impersonating"] == FRIEND
