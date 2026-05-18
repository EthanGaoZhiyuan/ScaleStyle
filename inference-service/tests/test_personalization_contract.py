"""
Reader-side contract: FeatureReader reads canonical item IDs from Redis.

Uses fakeredis to populate Redis with the key schema the (fixed) writer
produces, then calls load_personalization_snapshot() to verify that
recent_clicks and category_affinity are visible in the returned snapshot.

This test FAILS if:
  - recent_clicks LIST is populated with non-canonical IDs and the reader
    does not recover them (it does, via canonical_article_id on LRANGE result)
  - category_affinity HASH is missing or the decay formula produces zero
  - popularity ZSET members are non-canonical and ZMSCORE returns None for
    all candidates (popularity_signals stays all-zero)
"""

import os
import sys
import math
import time
from pathlib import Path
from unittest.mock import patch

import pytest

# Ensure src/ is on the path so absolute imports like "from src.config import ..."
# work when running tests from the inference-service root.
sys.path.insert(0, str(Path(__file__).parent.parent))

try:
    import fakeredis
    HAS_FAKEREDIS = True
except ImportError:
    HAS_FAKEREDIS = False

pytestmark = pytest.mark.skipif(not HAS_FAKEREDIS, reason="fakeredis not installed")

from src.personalization.feature_reader import FeatureReader
from src.utils.redis_metadata import canonical_article_id


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_reader(fake_redis_client):
    return FeatureReader(redis_client=fake_redis_client)


def _now():
    return time.time()


# ---------------------------------------------------------------------------
# Stage-1 contract: recent_clicks and category_affinity
# ---------------------------------------------------------------------------


class TestRecentClicksAndAffinityContract:
    """
    Simulate a fixed writer that stores canonical IDs and verify the reader
    returns them correctly in the PersonalizationSnapshot.
    """

    def test_recent_clicks_are_visible_in_snapshot(self):
        """
        Writer contract: LPUSH canonical item_id into user:{uid}:recent_clicks.
        Reader contract: LRANGE returns the same canonical IDs.
        """
        r = fakeredis.FakeRedis(decode_responses=True)
        canonical_id = canonical_article_id("108775015")  # "0108775015"
        r.lpush("user:test-user:recent_clicks", canonical_id)

        reader = _make_reader(r)
        # Patch out materialized window resolution to skip popularity stage.
        with patch.object(
            reader,
            "_resolve_materialized_popularity_windows",
            return_value=({}, 0),
        ):
            snapshot = reader.load_personalization_snapshot(
                user_id="test-user",
                candidate_item_ids=[canonical_id],
                max_recent_clicks=20,
            )

        assert canonical_id in snapshot.recent_clicks, (
            f"Expected {canonical_id!r} in recent_clicks, got {snapshot.recent_clicks}"
        )

    def test_category_affinity_is_visible_in_snapshot(self):
        """
        Writer contract: HSET user:{uid}:category_affinity category score.
        Reader contract: HGETALL returns the hash; score decayed from stored ts.
        """
        r = fakeredis.FakeRedis(decode_responses=True)
        now = _now()
        r.hset("user:test-user:category_affinity", "dress", "2.5")
        # Store last_ts = now so elapsed≈0 and decay≈1 (score stays near 2.5)
        r.hset("user:test-user:category_affinity:last_ts", "dress", str(now))

        reader = _make_reader(r)
        with patch.object(
            reader,
            "_resolve_materialized_popularity_windows",
            return_value=({}, 0),
        ):
            snapshot = reader.load_personalization_snapshot(
                user_id="test-user",
                candidate_item_ids=[],
                max_recent_clicks=20,
            )

        assert "dress" in snapshot.category_affinity, (
            "Expected 'dress' in category_affinity"
        )
        assert snapshot.category_affinity["dress"] > 0, (
            "Expected positive affinity score for 'dress'"
        )

    def test_non_canonical_id_in_list_is_canonicalized_on_read(self):
        """
        Reader tolerates pre-fix data: if the LIST contains a non-canonical ID
        the reader normalizes it via canonical_article_id before returning.
        This verifies the reader's defensive canonicalization still works.
        """
        r = fakeredis.FakeRedis(decode_responses=True)
        # Simulate the old (broken) writer that stored raw IDs
        r.lpush("user:test-user:recent_clicks", "108775015")

        reader = _make_reader(r)
        with patch.object(
            reader,
            "_resolve_materialized_popularity_windows",
            return_value=({}, 0),
        ):
            snapshot = reader.load_personalization_snapshot(
                user_id="test-user",
                candidate_item_ids=["0108775015"],
                max_recent_clicks=20,
            )

        assert "0108775015" in snapshot.recent_clicks, (
            "Reader should canonicalize raw '108775015' → '0108775015' on LRANGE"
        )


# ---------------------------------------------------------------------------
# Stage-3 contract: popularity ZMSCORE with canonical members
# ---------------------------------------------------------------------------


class TestPopularityZmscoreContract:
    """
    ZMSCORE returns a score only when the ZSET member string matches exactly.

    The (fixed) writer stores canonical_article_id(raw_id) as the ZSET member.
    The reader queries ZMSCORE with canonical_candidate_ids.
    They must be equal strings for the score lookup to succeed.
    """

    def test_canonical_zset_member_is_found_by_zmscore(self):
        """
        If the ZSET has "0108775015" as member, ZMSCORE(key, ["0108775015"])
        returns a non-None score.
        """
        r = fakeredis.FakeRedis(decode_responses=True)
        materialized_key = "popularity:materialized:24h:test"
        canonical_id = canonical_article_id("108775015")  # "0108775015"
        r.zadd(materialized_key, {canonical_id: 3.0})

        scores = r.zmscore(materialized_key, [canonical_id])
        assert scores[0] is not None, (
            f"ZMSCORE with canonical ID should return a score; got {scores}"
        )
        assert math.isclose(float(scores[0]), 3.0, rel_tol=1e-6), (
            f"ZMSCORE score should be 3.0; got {scores[0]}"
        )

    def test_non_canonical_zset_member_is_not_found_by_canonical_query(self):
        """
        Demonstrates the pre-fix breakage: if the ZSET member is the raw
        (non-padded) ID and the query uses the canonical ID, ZMSCORE returns None.
        This is the mismatch H-2 fixes.
        """
        r = fakeredis.FakeRedis(decode_responses=True)
        materialized_key = "popularity:materialized:24h:test"
        r.zadd(materialized_key, {"108775015": 3.0})  # raw, non-canonical member

        canonical_id = canonical_article_id("108775015")  # "0108775015"
        scores = r.zmscore(materialized_key, [canonical_id])
        assert scores[0] is None, (
            "ZMSCORE with canonical ID against non-canonical member must return None "
            "(this is the bug H-2 fixes)"
        )

    def test_popularity_signals_non_zero_with_canonical_members(self):
        """
        Full Stage-3 path: pre-populate a materialized window with canonical IDs,
        inject it via _resolve_materialized_popularity_windows, then verify
        the snapshot's popularity_signals are non-zero.
        """
        r = fakeredis.FakeRedis(decode_responses=True)
        canonical_id = canonical_article_id("108775015")  # "0108775015"
        materialized_key = "popularity:materialized:24h:fixed"
        r.zadd(materialized_key, {canonical_id: 5.0})
        r.expire(materialized_key, 300)

        reader = _make_reader(r)
        resolved_keys = {"1h": materialized_key, "24h": materialized_key, "7d": materialized_key}
        with patch.object(
            reader,
            "_resolve_materialized_popularity_windows",
            return_value=(resolved_keys, 0),
        ):
            snapshot = reader.load_personalization_snapshot(
                user_id=None,
                candidate_item_ids=["108775015"],  # raw; reader canonicalizes
                max_recent_clicks=20,
            )

        signal_24h = snapshot.popularity_signals.get(canonical_id, {}).get("24h", 0.0)
        assert signal_24h > 0, (
            f"Expected non-zero 24h popularity for {canonical_id!r}; got {signal_24h}. "
            "ZMSCORE likely failed to match canonical member."
        )
