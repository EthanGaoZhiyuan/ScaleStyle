import sys
from pathlib import Path
from unittest.mock import Mock, patch

import logging

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

from degradation import DegradationReason
from personalization.feature_reader import FeatureReader
from src.personalization.snapshot import PersonalizationSnapshot


def _pipeline_with_result(result):
    pipe = Mock()
    pipe.execute.return_value = result
    pipe.lrange.return_value = pipe
    pipe.hgetall.return_value = pipe
    pipe.hget.return_value = pipe
    pipe.ttl.return_value = pipe
    pipe.set.return_value = pipe  # For distributed lock acquisition
    pipe.zunionstore.return_value = pipe
    pipe.expire.return_value = pipe
    pipe.delete.return_value = pipe  # For lock release
    pipe.zmscore.return_value = pipe
    return pipe


def test_snapshot_loading_uses_bounded_pipelines_for_multiple_candidates():
    redis_client = Mock()
    user_pipe = _pipeline_with_result(
        [
            ["item_recent_1", "item_recent_2"],
            {"dress": "2.0"},
            {"dress": "1000.0"},
        ]
    )
    ttl_pipe = _pipeline_with_result([-2, -2, -2])
    lock_pipe = _pipeline_with_result([True, True, True])  # All locks acquired
    materialize_pipe = _pipeline_with_result(
        [None, True, None, True, None, True, None, None, None]
    )  # zunionstore + expire + delete for 3 windows
    item_pipe = _pipeline_with_result(
        [
            "dress",
            "pants",
            "dress",
            "tops",
            [5.0, 1.0],
            [8.0, 3.0],
            [9.0, 4.0],
        ]
    )
    redis_client.pipeline.side_effect = [
        user_pipe,
        ttl_pipe,
        lock_pipe,
        materialize_pipe,
        item_pipe,
    ]

    reader = FeatureReader(redis_client)
    snapshot = reader.load_personalization_snapshot(
        "user-1",
        ["candidate_a", "candidate_b"],
        max_recent_clicks=2,
    )

    assert isinstance(snapshot, PersonalizationSnapshot)
    assert snapshot.redis_round_trips == 5  # user + ttl + lock + materialize + item
    assert snapshot.recent_clicks == ("item_recent_1", "item_recent_2")
    assert snapshot.candidate_item_categories == {
        "candidate_a": "dress",
        "candidate_b": "pants",
    }
    assert snapshot.clicked_categories == {"dress", "tops"}
    assert snapshot.popularity_signals["candidate_a"]["1h"] == 5.0
    assert snapshot.popularity_signals["candidate_b"]["24h"] == 3.0


def test_snapshot_degrades_without_repeated_fetches_when_user_pipeline_fails():
    redis_client = Mock()
    user_pipe = _pipeline_with_result(None)
    user_pipe.execute.side_effect = RuntimeError("redis down")
    ttl_pipe = _pipeline_with_result([-2, -2, -2])
    lock_pipe = _pipeline_with_result([True, True, True])  # All locks acquired
    materialize_pipe = _pipeline_with_result(
        [None, True, None, True, None, True, None, None, None]
    )
    item_pipe = _pipeline_with_result(
        [
            "dress",
            [0.0],
            [0.0],
            [0.0],
        ]
    )
    redis_client.pipeline.side_effect = [
        user_pipe,
        ttl_pipe,
        lock_pipe,
        materialize_pipe,
        item_pipe,
    ]

    reader = FeatureReader(redis_client)
    snapshot = reader.load_personalization_snapshot(
        "user-1", ["candidate_a"], max_recent_clicks=2
    )

    assert snapshot.degraded is True
    assert DegradationReason.PERSONALIZATION_UNAVAILABLE in snapshot.degraded_reasons
    assert (
        redis_client.pipeline.call_count == 5
    )  # user + ttl + lock + materialize + item


def test_snapshot_degraded_log_uses_canonical_snake_case_fields(caplog):
    redis_client = Mock()
    user_pipe = _pipeline_with_result(None)
    user_pipe.execute.side_effect = RuntimeError("redis down")
    ttl_pipe = _pipeline_with_result([-2, -2, -2])
    lock_pipe = _pipeline_with_result([True, True, True])
    materialize_pipe = _pipeline_with_result(
        [None, True, None, True, None, True, None, None, None]
    )
    item_pipe = _pipeline_with_result(
        [
            "dress",
            [0.0],
            [0.0],
            [0.0],
        ]
    )
    redis_client.pipeline.side_effect = [
        user_pipe,
        ttl_pipe,
        lock_pipe,
        materialize_pipe,
        item_pipe,
    ]

    reader = FeatureReader(redis_client)
    with caplog.at_level(logging.WARNING):
        reader.load_personalization_snapshot(
            "user-1", ["candidate_a"], max_recent_clicks=2
        )

    assert "user_id=user-1" in caplog.text
    assert (
        "redis_round_trips=4" in caplog.text
    )  # user failed, so ttl + lock + materialize + item
    assert "degrade_reasons=PERSONALIZATION_UNAVAILABLE" in caplog.text


def test_repeated_snapshot_requests_reuse_local_materialized_windows_without_rebuilding():
    redis_client = Mock()
    user_pipe_1 = _pipeline_with_result([["item_recent_1"], {}, {}])
    ttl_pipe = _pipeline_with_result([-2, -2, -2])
    lock_pipe = _pipeline_with_result([True, True, True])
    materialize_pipe = _pipeline_with_result(
        [None, True, None, True, None, True, None, None, None]
    )
    item_pipe_1 = _pipeline_with_result(
        [
            "dress",
            "tops",
            [5.0],
            [8.0],
            [9.0],
        ]
    )
    user_pipe_2 = _pipeline_with_result([["item_recent_1"], {}, {}])
    cached_ttl_pipe = _pipeline_with_result([])
    item_pipe_2 = _pipeline_with_result(
        [
            "dress",
            "tops",
            [6.0],
            [9.0],
            [10.0],
        ]
    )
    redis_client.pipeline.side_effect = [
        user_pipe_1,
        ttl_pipe,
        lock_pipe,
        materialize_pipe,
        item_pipe_1,
        user_pipe_2,
        cached_ttl_pipe,
        item_pipe_2,
    ]

    reader = FeatureReader(redis_client)
    with patch("personalization.feature_reader.time.time", return_value=1000.0):
        first = reader.load_personalization_snapshot(
            "user-1", ["candidate_a"], max_recent_clicks=1
        )
        second = reader.load_personalization_snapshot(
            "user-1", ["candidate_a"], max_recent_clicks=1
        )

    assert first.redis_round_trips == 5  # user + ttl + lock + materialize + item
    assert second.redis_round_trips == 2  # user + item (windows cached)
    assert (
        redis_client.pipeline.call_count == 8
    )  # 5 for first + cached-ttl pipe + user + item
    assert second.popularity_signals["candidate_a"]["1h"] == 6.0


def test_expired_materialization_rebuilds_once_then_reuses():
    redis_client = Mock()
    user_pipe_1 = _pipeline_with_result([[], {}, {}])
    ttl_pipe_1 = _pipeline_with_result([-2, -2, -2])
    lock_pipe_1 = _pipeline_with_result([True, True, True])
    materialize_pipe_1 = _pipeline_with_result(
        [None, True, None, True, None, True, None, None, None]
    )
    item_pipe_1 = _pipeline_with_result(
        [
            "dress",
            [1.0],
            [2.0],
            [3.0],
        ]
    )
    user_pipe_2 = _pipeline_with_result([[], {}, {}])
    ttl_pipe_2 = _pipeline_with_result([-2, -2, -2])
    lock_pipe_2 = _pipeline_with_result([True, True, True])
    materialize_pipe_2 = _pipeline_with_result(
        [None, True, None, True, None, True, None, None, None]
    )
    item_pipe_2 = _pipeline_with_result(
        [
            "dress",
            [4.0],
            [5.0],
            [6.0],
        ]
    )
    user_pipe_3 = _pipeline_with_result([[], {}, {}])
    cached_ttl_pipe = _pipeline_with_result([])
    item_pipe_3 = _pipeline_with_result(
        [
            "dress",
            [7.0],
            [8.0],
            [9.0],
        ]
    )
    redis_client.pipeline.side_effect = [
        user_pipe_1,
        ttl_pipe_1,
        lock_pipe_1,
        materialize_pipe_1,
        item_pipe_1,
        user_pipe_2,
        ttl_pipe_2,
        lock_pipe_2,
        materialize_pipe_2,
        item_pipe_2,
        user_pipe_3,
        cached_ttl_pipe,
        item_pipe_3,
    ]

    reader = FeatureReader(redis_client)
    with patch("personalization.feature_reader.time.time", return_value=1000.0):
        first = reader.load_personalization_snapshot(
            "user-1", ["candidate_a"], max_recent_clicks=1
        )
    with patch("personalization.feature_reader.time.time", return_value=5000.0):
        second = reader.load_personalization_snapshot(
            "user-1", ["candidate_a"], max_recent_clicks=1
        )
    with patch("personalization.feature_reader.time.time", return_value=5001.0):
        third = reader.load_personalization_snapshot(
            "user-1", ["candidate_a"], max_recent_clicks=1
        )

    assert first.redis_round_trips == 5  # user + ttl + lock + materialize + item
    assert (
        second.redis_round_trips == 5
    )  # user + ttl + lock + materialize + item (expired)
    assert third.redis_round_trips == 2  # user + item (cached)
    assert (
        redis_client.pipeline.call_count == 13
    )  # 5 + 5 + cached-ttl pipe + user + item
    assert second.popularity_signals["candidate_a"]["7d"] == 6.0
    assert third.popularity_signals["candidate_a"]["7d"] == 9.0
