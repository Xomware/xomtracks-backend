"""
RED-before-GREEN: per-user ingest tokens (self-serve foundation Phase 3).

The xomtracks-ingest-tokens table stores a SHA-256 HASH of an opaque random
token -- the plaintext is returned to the owner exactly once (at mint) and never
persisted. Auth hashes the presented bearer and looks the hash up to resolve the
owner. Tokens are revocable (a flag flip), and revoke is scoped to the owner so
one user can never revoke another's token.
"""

import boto3
import pytest
from moto import mock_aws

from lambdas.common.constants import INGEST_TOKENS_TABLE_NAME

OWNER_A = "f4e80448-2061-7059-0c26-d0fd91863568"  # Dom
OWNER_B = "aaaabbbb-1111-2222-3333-444455556666"  # a second user


def _create_table():
    ddb = boto3.resource("dynamodb", region_name="us-east-1")
    ddb.create_table(
        TableName=INGEST_TOKENS_TABLE_NAME,
        KeySchema=[{"AttributeName": "tokenHash", "KeyType": "HASH"}],
        AttributeDefinitions=[{"AttributeName": "tokenHash", "AttributeType": "S"}],
        BillingMode="PAY_PER_REQUEST",
    )


@pytest.fixture
def tokens_table():
    with mock_aws():
        _create_table()
        yield


class TestHashing:
    def test_hash_is_deterministic_sha256_hex(self):
        from lambdas.common import ingest_tokens

        h1 = ingest_tokens.hash_token("some-token")
        h2 = ingest_tokens.hash_token("some-token")
        assert h1 == h2
        # SHA-256 hex digest is 64 lowercase hex chars.
        assert len(h1) == 64
        assert all(c in "0123456789abcdef" for c in h1)

    def test_generated_tokens_are_unique_and_opaque(self):
        from lambdas.common import ingest_tokens

        tokens = {ingest_tokens.generate_token() for _ in range(50)}
        assert len(tokens) == 50
        # Long enough to be unguessable.
        assert all(len(t) >= 32 for t in tokens)


class TestMint:
    def test_mint_returns_plaintext_and_stores_only_the_hash(self, tokens_table):
        from lambdas.common import ingest_tokens

        result = ingest_tokens.mint_token(OWNER_A)
        plaintext = result["token"]
        token_hash = result["tokenHash"]

        assert plaintext
        assert token_hash == ingest_tokens.hash_token(plaintext)
        assert result["ownerId"] == OWNER_A

        # The stored row holds the HASH as its key and NEVER the plaintext.
        table = boto3.resource("dynamodb", region_name="us-east-1").Table(INGEST_TOKENS_TABLE_NAME)
        row = table.get_item(Key={"tokenHash": token_hash})["Item"]
        assert row["ownerId"] == OWNER_A
        assert row["revoked"] is False
        assert "createdAt" in row
        # Plaintext appears NOWHERE in the persisted row.
        assert plaintext not in str(row)


class TestResolve:
    def test_resolve_returns_owner_for_a_live_token(self, tokens_table):
        from lambdas.common import ingest_tokens

        plaintext = ingest_tokens.mint_token(OWNER_B)["token"]
        assert ingest_tokens.resolve_owner(plaintext) == OWNER_B

    def test_resolve_unknown_token_is_none(self, tokens_table):
        from lambdas.common import ingest_tokens

        assert ingest_tokens.resolve_owner("never-minted") is None

    def test_resolve_revoked_token_is_none(self, tokens_table):
        from lambdas.common import ingest_tokens

        minted = ingest_tokens.mint_token(OWNER_A)
        ingest_tokens.revoke_token(OWNER_A, minted["tokenHash"])
        assert ingest_tokens.resolve_owner(minted["token"]) is None


class TestRevoke:
    def test_owner_can_revoke_their_own_token(self, tokens_table):
        from lambdas.common import ingest_tokens

        minted = ingest_tokens.mint_token(OWNER_A)
        out = ingest_tokens.revoke_token(OWNER_A, minted["tokenHash"])
        assert out["revoked"] is True

        table = boto3.resource("dynamodb", region_name="us-east-1").Table(INGEST_TOKENS_TABLE_NAME)
        row = table.get_item(Key={"tokenHash": minted["tokenHash"]})["Item"]
        assert row["revoked"] is True

    def test_cannot_revoke_another_owners_token(self, tokens_table):
        from lambdas.common import ingest_tokens
        from lambdas.common.errors import NotFoundError

        minted = ingest_tokens.mint_token(OWNER_A)
        # OWNER_B tries to revoke OWNER_A's token -- must be refused, and the
        # token must remain live.
        with pytest.raises(NotFoundError):
            ingest_tokens.revoke_token(OWNER_B, minted["tokenHash"])
        assert ingest_tokens.resolve_owner(minted["token"]) == OWNER_A

    def test_revoke_missing_token_raises_not_found(self, tokens_table):
        from lambdas.common import ingest_tokens
        from lambdas.common.errors import NotFoundError

        with pytest.raises(NotFoundError):
            ingest_tokens.revoke_token(OWNER_A, "deadbeef")
