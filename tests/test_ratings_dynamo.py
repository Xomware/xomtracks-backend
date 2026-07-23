"""
RED-before-GREEN: ratings_dynamo -- whole-group song ratings keyed per
(trackKey, raterEmail). One rating per user per song (upsert), aggregate
{avg, count, myRating} computed by querying the trackKey partition.
"""

import boto3
import pytest
from moto import mock_aws

from lambdas.common.constants import RATINGS_TABLE_NAME


def _create_ratings_table():
    ddb = boto3.resource("dynamodb", region_name="us-east-1")
    ddb.create_table(
        TableName=RATINGS_TABLE_NAME,
        KeySchema=[
            {"AttributeName": "trackKey", "KeyType": "HASH"},
            {"AttributeName": "raterEmail", "KeyType": "RANGE"},
        ],
        AttributeDefinitions=[
            {"AttributeName": "trackKey", "AttributeType": "S"},
            {"AttributeName": "raterEmail", "AttributeType": "S"},
        ],
        ProvisionedThroughput={"ReadCapacityUnits": 5, "WriteCapacityUnits": 5},
    )
    return ddb


@pytest.fixture
def ratings_table():
    with mock_aws():
        yield _create_ratings_table()


class TestSetRating:
    def test_first_rating_returns_aggregate(self, ratings_table):
        from lambdas.common.ratings_dynamo import set_rating

        agg = set_rating("spotify:abc", "dom@example.com", 5)
        assert agg == {"avg": 5, "count": 1, "myRating": 5}

    def test_multiple_raters_average(self, ratings_table):
        from lambdas.common.ratings_dynamo import set_rating

        set_rating("spotify:abc", "dom@example.com", 5)
        set_rating("spotify:abc", "sam@example.com", 2)
        agg = set_rating("spotify:abc", "jo@example.com", 2)  # (5+2+2)/3 = 3.0

        assert agg["count"] == 3
        assert agg["avg"] == 3.0
        assert agg["myRating"] == 2  # jo's own

    def test_rerate_overwrites_not_appends(self, ratings_table):
        from lambdas.common.ratings_dynamo import set_rating

        set_rating("spotify:abc", "dom@example.com", 1)
        agg = set_rating("spotify:abc", "dom@example.com", 4)

        assert agg["count"] == 1  # still one row for dom
        assert agg["avg"] == 4
        assert agg["myRating"] == 4

    def test_rating_out_of_range_raises(self, ratings_table):
        from lambdas.common.errors import ValidationError
        from lambdas.common.ratings_dynamo import set_rating

        with pytest.raises(ValidationError):
            set_rating("spotify:abc", "dom@example.com", 6)
        with pytest.raises(ValidationError):
            set_rating("spotify:abc", "dom@example.com", 0)

    def test_empty_track_key_raises(self, ratings_table):
        from lambdas.common.errors import ValidationError
        from lambdas.common.ratings_dynamo import set_rating

        with pytest.raises(ValidationError):
            set_rating("", "dom@example.com", 3)


class TestBatchAndAggregate:
    def test_batch_omits_unrated_keys(self, ratings_table):
        from lambdas.common.ratings_dynamo import batch_ratings_for_track_keys, set_rating

        set_rating("spotify:abc", "dom@example.com", 4)
        result = batch_ratings_for_track_keys({"spotify:abc", "url:nope"}, "dom@example.com")

        assert set(result) == {"spotify:abc"}
        assert result["spotify:abc"]["myRating"] == 4

    def test_my_rating_none_for_non_rater(self, ratings_table):
        from lambdas.common.ratings_dynamo import batch_ratings_for_track_keys, set_rating

        set_rating("spotify:abc", "dom@example.com", 4)
        result = batch_ratings_for_track_keys({"spotify:abc"}, "stranger@example.com")

        assert result["spotify:abc"]["count"] == 1
        assert result["spotify:abc"]["myRating"] is None


class TestEnrichShares:
    def test_attaches_track_key_and_rating(self, ratings_table):
        from lambdas.common.ratings_dynamo import enrich_shares_with_ratings, set_rating

        set_rating("spotify:abc", "dom@example.com", 5)
        shares = [
            {"shareId": "s1", "resolvedSpotifyId": "abc", "sourceUrl": "https://open.spotify.com/track/abc"},
            {"shareId": "s2", "sourceUrl": "https://soundcloud.com/x/y"},
        ]
        enrich_shares_with_ratings(shares, "dom@example.com")

        assert shares[0]["trackKey"] == "spotify:abc"
        assert shares[0]["rating"] == {"avg": 5, "count": 1, "myRating": 5}
        # unrated share gets the empty aggregate, not a missing key
        assert shares[1]["rating"] == {"avg": 0, "count": 0, "myRating": None}

    def test_two_shares_same_song_share_aggregate(self, ratings_table):
        from lambdas.common.ratings_dynamo import enrich_shares_with_ratings, set_rating

        set_rating("spotify:abc", "dom@example.com", 4)
        shares = [
            {"shareId": "s1", "resolvedSpotifyId": "abc", "sourceUrl": "u1"},
            {"shareId": "s2", "resolvedSpotifyId": "abc", "sourceUrl": "u2"},
        ]
        enrich_shares_with_ratings(shares, "dom@example.com")
        assert shares[0]["rating"] == shares[1]["rating"] == {"avg": 4, "count": 1, "myRating": 4}
