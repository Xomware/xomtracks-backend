"""
RED-before-GREEN: lambdas/common/user_links.py -- the additive Cognito
identity <-> phone-handle mapping stored on the xomtracks-users table.

Keyed by the caller's Cognito email (a distinct row from the single Spotify
service-account row). linkedHandles is a DynamoDB String Set updated via ADD
so re-linking is idempotent and linking a second number appends.
"""

import boto3
import pytest
from moto import mock_aws

from lambdas.common.constants import USERS_TABLE_NAME


def _create_users_table():
    ddb = boto3.resource("dynamodb", region_name="us-east-1")
    ddb.create_table(
        TableName=USERS_TABLE_NAME,
        KeySchema=[{"AttributeName": "email", "KeyType": "HASH"}],
        AttributeDefinitions=[{"AttributeName": "email", "AttributeType": "S"}],
        ProvisionedThroughput={"ReadCapacityUnits": 5, "WriteCapacityUnits": 5},
    )
    return boto3.resource("dynamodb", region_name="us-east-1").Table(USERS_TABLE_NAME)


@pytest.fixture
def users_table():
    with mock_aws():
        yield _create_users_table()


class TestLinkPhone:
    def test_first_link_creates_row_with_handle(self, users_table):
        from lambdas.common.user_links import link_phone

        handles = link_phone("member@example.com", "3364042196", sub="cognito-sub-1")
        assert handles == {"3364042196"}

        item = users_table.get_item(Key={"email": "member@example.com"})["Item"]
        assert set(item["linkedHandles"]) == {"3364042196"}
        assert item["sub"] == "cognito-sub-1"
        assert item["recordType"] == "userLink"

    def test_relinking_same_handle_is_idempotent(self, users_table):
        from lambdas.common.user_links import link_phone

        link_phone("member@example.com", "3364042196")
        handles = link_phone("member@example.com", "3364042196")
        assert handles == {"3364042196"}

    def test_second_number_appends(self, users_table):
        from lambdas.common.user_links import link_phone

        link_phone("member@example.com", "3364042196")
        handles = link_phone("member@example.com", "9195551234")
        assert handles == {"3364042196", "9195551234"}

    def test_empty_handle_rejected(self, users_table):
        from lambdas.common.user_links import link_phone
        from lambdas.common.errors import XomtracksError

        with pytest.raises(XomtracksError):
            link_phone("member@example.com", "")


class TestGetLinkedHandles:
    def test_unknown_user_is_empty(self, users_table):
        from lambdas.common.user_links import get_linked_handles

        assert get_linked_handles("nobody@example.com") == set()

    def test_returns_linked_handles(self, users_table):
        from lambdas.common.user_links import link_phone, get_linked_handles

        link_phone("member@example.com", "3364042196")
        link_phone("member@example.com", "9195551234")
        assert get_linked_handles("member@example.com") == {"3364042196", "9195551234"}
