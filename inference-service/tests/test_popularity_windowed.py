import sys
from pathlib import Path
from unittest.mock import Mock

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

from personalization.feature_reader_legacy import LegacyFeatureReader
from personalization.popularity_windows import active_bucket_keys, materialized_window_key


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
