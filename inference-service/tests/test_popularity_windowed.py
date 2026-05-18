import sys
from pathlib import Path
from unittest.mock import Mock

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

from personalization.legacy.feature_reader_legacy import LegacyFeatureReader
from personalization.popularity_windows import (
    active_bucket_keys,
    materialized_window_key,
)

# ---------------------------------------------------------------------------
# Cross-language contract: same timestamp → same key as Java PopularityKeyFormulaTest
#
# Fixed anchor: 1_700_000_000 Unix seconds (2023-11-14T22:13:20 UTC)
# Java equivalents in gateway-service/src/test/.../PopularityKeyFormulaTest.java:
#   popularityMaterializedKey(PREFIX, "24h", 1_700_000_000L, 3600L) == "popularity:materialized:24h:1699999200"
#   popularityMaterializedKey(PREFIX, "7d",  1_700_000_000L, 86400L) == "popularity:materialized:7d:1699920000"
#   popularityMaterializedKey(PREFIX, "1h",  1_700_000_000L, 300L)   == "popularity:materialized:1h:1699999800"
#
# If either side's expected string changes, the gateway reads a different ZSET than inference
# wrote and silently falls through to global:popular with no visible error.
# ---------------------------------------------------------------------------

_CROSS_LANG_TS = (
    1_700_000_000  # 2023-11-14T22:13:20 UTC — shared with Java PopularityKeyFormulaTest
)


def test_materialized_key_24h_cross_language_contract():
    """Java PopularityKeyFormulaTest.key_24h_knownTimestamp asserts the same expected string."""
    assert (
        materialized_window_key("24h", 3600, now_ts=_CROSS_LANG_TS)
        == "popularity:materialized:24h:1699999200"
    )


def test_materialized_key_7d_cross_language_contract():
    """Java PopularityKeyFormulaTest.key_7d_knownTimestamp asserts the same expected string."""
    assert (
        materialized_window_key("7d", 86400, now_ts=_CROSS_LANG_TS)
        == "popularity:materialized:7d:1699920000"
    )


def test_materialized_key_1h_cross_language_contract():
    """Java PopularityKeyFormulaTest.key_1h_knownTimestamp asserts the same expected string."""
    assert (
        materialized_window_key("1h", 300, now_ts=_CROSS_LANG_TS)
        == "popularity:materialized:1h:1699999800"
    )


def test_bucket_start_alignment_invariant():
    """bucketStart % bucket_seconds == 0 for all standard window sizes."""
    for window, bucket_secs in [("1h", 300), ("24h", 3600), ("7d", 86400)]:
        key = materialized_window_key(window, bucket_secs, now_ts=_CROSS_LANG_TS)
        bucket_start = int(key.rsplit(":", 1)[-1])
        assert (
            bucket_start % bucket_secs == 0
        ), f"bucketStart {bucket_start} is not a multiple of bucket_seconds {bucket_secs}"


def test_window_rollover_changes_materialized_key():
    before_boundary = materialized_window_key("24h", 3600, now_ts=3599.0)
    after_boundary = materialized_window_key("24h", 3600, now_ts=3600.0)

    assert before_boundary != after_boundary
    assert before_boundary.endswith(":0")
    assert after_boundary.endswith(":3600")


def test_active_bucket_keys_cover_latest_window():
    keys = active_bucket_keys("1h", 300, 12, now_ts=3600.0)

    assert len(keys) == 12
    assert keys[0].endswith(":3600")
    assert keys[-1].endswith(":300")


def test_missing_window_scores_fall_back_to_zero():
    redis_client = Mock()
    ttl_pipe = Mock()
    ttl_pipe.ttl.return_value = ttl_pipe
    ttl_pipe.execute.return_value = [60, 60, 60]
    zmscore_pipe = Mock()
    zmscore_pipe.zmscore.return_value = zmscore_pipe
    zmscore_pipe.execute.return_value = [
        [None, None],
        [None, None],
        [None, None],
    ]
    redis_client.pipeline.side_effect = [ttl_pipe, zmscore_pipe]

    reader = LegacyFeatureReader(redis_client)
    signals = reader.get_item_popularity_signals(["item_1", "item_2"])

    assert signals["item_1"] == {"1h": 0.0, "24h": 0.0, "7d": 0.0}
    assert signals["item_2"] == {"1h": 0.0, "24h": 0.0, "7d": 0.0}
