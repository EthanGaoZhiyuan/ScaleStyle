"""
Failure-path tests for FeatureReader snapshot loading.

Validates the request-time popularity materialization fix and snapshot-only
personalization hot path: Redis TimeoutError/ConnectionError during any stage
produces a degraded snapshot with the correct canonical reason, never raises.
"""

import sys
from pathlib import Path
from unittest.mock import Mock

import redis

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

from src.degradation import DegradationReason
from src.personalization.feature_reader import FeatureReader
from src.personalization.snapshot import PersonalizationSnapshot


def _pipeline_with_result(result):
    """Helper: pipeline that returns result from execute()."""
    pipe = Mock()
    pipe.execute.return_value = result
    pipe.lrange.return_value = pipe
    pipe.hgetall.return_value = pipe
    pipe.hget.return_value = pipe
    pipe.ttl.return_value = pipe
    pipe.set.return_value = pipe  # For distributed lock
    pipe.zunionstore.return_value = pipe
    pipe.expire.return_value = pipe
    pipe.delete.return_value = pipe  # For lock release
    pipe.zmscore.return_value = pipe
    return pipe


def test_materialization_stage_redis_timeout_returns_degraded_snapshot_with_redis_timeout():
    """
    Regression: Materialization stage Redis TimeoutError -> snapshot degraded with REDIS_TIMEOUT.

    Request-time popularity materialization fix: when Redis times out during
    TTL check or window rebuild, the snapshot must not raise; it must return
    a degraded snapshot with DegradationReason.REDIS_TIMEOUT.
    """
    redis_client = Mock()
    user_pipe = _pipeline_with_result([["item1"], {}, {}])
    ttl_pipe = _pipeline_with_result([-2, -2, -2])
    ttl_pipe.execute.side_effect = redis.TimeoutError("Connection timed out")
    # materialize_pipe unused when ttl_pipe.execute raises; item_pipe still runs for stage 3
    materialize_pipe = _pipeline_with_result([None, True, None, True, None, True])
    item_pipe = _pipeline_with_result(["dress", [1.0], [2.0], [3.0]])

    # When ttl_pipe.execute raises, rebuild_pipe is never created; stage 3 uses next pipeline
    redis_client.pipeline.side_effect = [user_pipe, ttl_pipe, item_pipe]

    reader = FeatureReader(redis_client)
    snapshot = reader.load_personalization_snapshot("user-1", ["candidate_a"], max_recent_clicks=2)

    assert isinstance(snapshot, PersonalizationSnapshot)
    assert snapshot.degraded is True
    assert DegradationReason.REDIS_TIMEOUT in snapshot.degraded_reasons


def test_materialization_stage_redis_connection_error_returns_degraded_snapshot_with_redis_unavailable():
    """
    Regression: Materialization stage Redis ConnectionError -> REDIS_UNAVAILABLE.

    When Redis is unreachable during popularity window resolution, snapshot
    must degrade with REDIS_UNAVAILABLE, not REDIS_TIMEOUT.
    """
    redis_client = Mock()
    ttl_pipe = _pipeline_with_result([-2, -2, -2])
    ttl_pipe.execute.side_effect = redis.ConnectionError("Connection refused")
    item_pipe = _pipeline_with_result(["dress", [0.0], [0.0], [0.0]])

    redis_client.pipeline.side_effect = [ttl_pipe, item_pipe]

    reader = FeatureReader(redis_client)
    snapshot = reader.load_personalization_snapshot(None, ["candidate_a"], max_recent_clicks=2)

    assert snapshot.degraded is True
    assert DegradationReason.REDIS_UNAVAILABLE in snapshot.degraded_reasons


def test_item_stage_redis_timeout_returns_degraded_snapshot_with_redis_timeout():
    """
    Regression: Stage 3 (item-level) Redis TimeoutError -> REDIS_TIMEOUT.

    Candidate item categories and popularity zmscore fetch can time out;
    snapshot must degrade gracefully with canonical reason.
    """
    redis_client = Mock()
    # user_id=None skips stage 1; stage 2 uses ttl then lock then rebuild; stage 3 uses item
    ttl_pipe = _pipeline_with_result([-2, -2, -2])
    lock_pipe = _pipeline_with_result([True, True, True])
    materialize_pipe = _pipeline_with_result([None, True, None, True, None, True, None, None, None])
    item_pipe = _pipeline_with_result(["dress", [1.0], [2.0], [3.0]])
    item_pipe.execute.side_effect = redis.TimeoutError("Socket timeout")

    redis_client.pipeline.side_effect = [ttl_pipe, lock_pipe, materialize_pipe, item_pipe]

    reader = FeatureReader(redis_client)
    snapshot = reader.load_personalization_snapshot(None, ["candidate_a"], max_recent_clicks=2)

    assert snapshot.degraded is True
    assert DegradationReason.REDIS_TIMEOUT in snapshot.degraded_reasons


def test_item_stage_redis_connection_error_returns_degraded_snapshot_with_redis_unavailable():
    """
    Regression: Stage 3 Redis ConnectionError -> REDIS_UNAVAILABLE.
    """
    redis_client = Mock()
    ttl_pipe = _pipeline_with_result([-2, -2, -2])
    lock_pipe = _pipeline_with_result([True, True, True])
    materialize_pipe = _pipeline_with_result([None, True, None, True, None, True, None, None, None])
    item_pipe = _pipeline_with_result(["dress", [0.0], [0.0], [0.0]])
    item_pipe.execute.side_effect = redis.ConnectionError("ECONNREFUSED")

    redis_client.pipeline.side_effect = [ttl_pipe, lock_pipe, materialize_pipe, item_pipe]

    reader = FeatureReader(redis_client)
    snapshot = reader.load_personalization_snapshot(None, ["candidate_a"], max_recent_clicks=2)

    assert snapshot.degraded is True
    assert DegradationReason.REDIS_UNAVAILABLE in snapshot.degraded_reasons


def test_user_stage_redis_timeout_returns_degraded_snapshot_with_redis_timeout():
    """
    Regression: Stage 1 (user-level) Redis TimeoutError -> REDIS_TIMEOUT.

    User recent_clicks / category_affinity pipeline can time out.
    """
    redis_client = Mock()
    user_pipe = _pipeline_with_result([["item1"], {}, {}])
    user_pipe.execute.side_effect = redis.TimeoutError("Timed out")
    ttl_pipe = _pipeline_with_result([-2, -2, -2])
    lock_pipe = _pipeline_with_result([True, True, True])
    materialize_pipe = _pipeline_with_result([None, True, None, True, None, True, None, None, None])
    item_pipe = _pipeline_with_result(["dress", [1.0], [2.0], [3.0]])

    redis_client.pipeline.side_effect = [user_pipe, ttl_pipe, lock_pipe, materialize_pipe, item_pipe]

    reader = FeatureReader(redis_client)
    snapshot = reader.load_personalization_snapshot("user-1", ["candidate_a"], max_recent_clicks=2)

    assert snapshot.degraded is True
    assert DegradationReason.REDIS_TIMEOUT in snapshot.degraded_reasons


def test_stage2_and_stage3_both_fail_accumulates_degraded_reasons():
    """
    Regression: Stage 2 (materialization) and stage 3 (item) both fail -> both reasons present.

    Unified degrade reasons: when materialization times out and item fetch
    connection fails, snapshot must contain both REDIS_TIMEOUT and REDIS_UNAVAILABLE.
    """
    redis_client = Mock()
    # user_id=None skips stage 1
    ttl_pipe = _pipeline_with_result([-2, -2, -2])
    ttl_pipe.execute.side_effect = redis.TimeoutError("materialization timed out")
    item_pipe = _pipeline_with_result(["dress", [0.0], [0.0], [0.0]])
    item_pipe.execute.side_effect = redis.ConnectionError("item stage connection refused")

    redis_client.pipeline.side_effect = [ttl_pipe, item_pipe]

    reader = FeatureReader(redis_client)
    snapshot = reader.load_personalization_snapshot(None, ["candidate_a"], max_recent_clicks=2)

    assert snapshot.degraded is True
    assert DegradationReason.REDIS_TIMEOUT in snapshot.degraded_reasons
    assert DegradationReason.REDIS_UNAVAILABLE in snapshot.degraded_reasons
