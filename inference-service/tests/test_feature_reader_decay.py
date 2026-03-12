import sys
from pathlib import Path
from unittest.mock import Mock, patch

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))
sys.path.insert(0, str(Path(__file__).parent.parent.parent / "inference-service" / "src"))

from personalization.feature_reader_legacy import LegacyFeatureReader


def test_category_affinity_read_applies_lazy_decay():
    redis_client = Mock()
    redis_client.pipeline.return_value = redis_client
    redis_client.hgetall.side_effect = [
        {"dress": "2.0"},
        {"dress": "1000.0"},
    ]
    redis_client.execute.return_value = [
        {"dress": "2.0"},
        {"dress": "1000.0"},
    ]

    reader = LegacyFeatureReader(redis_client)

    # Seven-day half-life is the default. One half-life after update, score halves.
    half_life_seconds = 7 * 86400
    now = 1000.0 + half_life_seconds
    with patch("personalization.feature_reader_legacy.time.time", return_value=now):
        affinity = reader.get_user_category_affinity("user-1")

    assert affinity["dress"] == pytest.approx(1.0, rel=1e-6)


def test_category_affinity_read_falls_back_to_raw_score_when_timestamp_missing():
    redis_client = Mock()
    redis_client.pipeline.return_value = redis_client
    redis_client.execute.return_value = [
        {"dress": "2.5"},
        {},
    ]

    reader = LegacyFeatureReader(redis_client)
    affinity = reader.get_user_category_affinity("user-1")

    assert affinity["dress"] == 2.5
