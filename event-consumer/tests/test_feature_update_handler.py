"""Tests for FeatureUpdateHandler."""
import os
import sys
from pathlib import Path
from unittest.mock import MagicMock, Mock, patch

import pytest

os.environ.setdefault("KAFKA_BOOTSTRAP_SERVERS", "test-kafka:9093")
os.environ.setdefault("CONSUMER_MODE", "primary")

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

import redis as redis_lib

from feature_update_handler import FeatureUpdateHandler, _CategoryCache
from models import ProcessingResult


@pytest.fixture
def lua_script():
    return Mock(return_value="OK")


@pytest.fixture
def redis_client():
    r = Mock()
    r.hget.return_value = "dress"
    return r


@pytest.fixture
def handler(lua_script, redis_client):
    with patch("feature_update_handler.metrics") as mock_metrics:
        _setup_metrics_mock(mock_metrics)
        h = FeatureUpdateHandler(
            lua_upsert_script=lua_script,
            redis_client=redis_client,
            category_cache_max_size=100,
            category_cache_ttl_seconds=3600,
        )
        h._mock_metrics = mock_metrics
        yield h


def _setup_metrics_mock(m):
    for attr in [
        "events_processed_total",
        "events_failed_total",
        "events_duplicate_total",
        "feature_failures_total",
        "redis_operations_total",
        "event_processing_latency_seconds",
        "redis_update_latency_seconds",
        "category_cache_ops_total",
        "category_cache_size",
    ]:
        mock_metric = MagicMock()
        mock_metric.labels.return_value = mock_metric
        mock_metric.inc.return_value = None
        mock_metric.observe.return_value = None
        mock_metric.set.return_value = None
        setattr(m, attr, mock_metric)
    m.record_redis_success = Mock()
    m.record_redis_error = Mock()


def _valid_event(**overrides):
    base = {
        "event_id": "evt-abc",
        "user_id": "user-1",
        "item_id": "item-1",
        "timestamp": "2026-03-07T10:30:00Z",
        "session_id": "sess-1",
        "source": "search",
    }
    base.update(overrides)
    return base


# ---------------------------------------------------------------------------
# APPLIED
# ---------------------------------------------------------------------------


def test_valid_event_returns_applied(handler, lua_script):
    lua_script.return_value = "OK"
    result = handler.update_features(_valid_event())
    assert result == ProcessingResult.APPLIED


def test_applied_calls_lua_with_correct_key_prefix(handler, lua_script):
    lua_script.return_value = "OK"
    handler.update_features(_valid_event(event_id="evt-xyz"))
    call_keys = lua_script.call_args.kwargs["keys"]
    assert call_keys == ["dedupe:event:evt-xyz"]


def test_category_passed_as_argv6(handler, lua_script, redis_client):
    lua_script.return_value = "OK"
    redis_client.hget.return_value = "shoes"
    handler.update_features(_valid_event())
    args = lua_script.call_args.kwargs["args"]
    assert args[5] == "shoes"


# ---------------------------------------------------------------------------
# DUPLICATE
# ---------------------------------------------------------------------------


def test_duplicate_lua_response_returns_duplicate(handler, lua_script):
    lua_script.return_value = "DUPLICATE"
    result = handler.update_features(_valid_event())
    assert result == ProcessingResult.DUPLICATE


def test_duplicate_bytes_response_returns_duplicate(handler, lua_script):
    lua_script.return_value = b"DUPLICATE"
    result = handler.update_features(_valid_event())
    assert result == ProcessingResult.DUPLICATE


# ---------------------------------------------------------------------------
# PERMANENT_FAILURE
# ---------------------------------------------------------------------------


def test_missing_event_id_returns_permanent_failure(handler):
    event = _valid_event()
    del event["event_id"]
    result = handler.update_features(event)
    assert result == ProcessingResult.PERMANENT_FAILURE


def test_missing_user_id_returns_permanent_failure(handler):
    result = handler.update_features(_valid_event(user_id=None))
    assert result == ProcessingResult.PERMANENT_FAILURE


def test_missing_item_id_returns_permanent_failure(handler):
    result = handler.update_features(_valid_event(item_id=None))
    assert result == ProcessingResult.PERMANENT_FAILURE


def test_invalid_timestamp_returns_permanent_failure(handler):
    result = handler.update_features(_valid_event(timestamp="not-a-date"))
    assert result == ProcessingResult.PERMANENT_FAILURE


def test_missing_timestamp_returns_permanent_failure(handler):
    result = handler.update_features(_valid_event(timestamp=None))
    assert result == ProcessingResult.PERMANENT_FAILURE


# ---------------------------------------------------------------------------
# TRANSIENT_FAILURE
# ---------------------------------------------------------------------------


def test_redis_connection_error_returns_transient_failure(handler, lua_script):
    lua_script.side_effect = redis_lib.ConnectionError("refused")
    result = handler.update_features(_valid_event())
    assert result == ProcessingResult.TRANSIENT_FAILURE


def test_generic_runtime_error_returns_transient_failure(handler, lua_script):
    lua_script.side_effect = RuntimeError("unexpected redis error")
    result = handler.update_features(_valid_event())
    assert result == ProcessingResult.TRANSIENT_FAILURE


def test_redis_timeout_error_returns_transient_failure(handler, lua_script):
    lua_script.side_effect = redis_lib.TimeoutError("timeout")
    result = handler.update_features(_valid_event())
    assert result == ProcessingResult.TRANSIENT_FAILURE


# ---------------------------------------------------------------------------
# Category cache
# ---------------------------------------------------------------------------


def test_unknown_category_when_redis_returns_none(handler, lua_script, redis_client):
    lua_script.return_value = "OK"
    redis_client.hget.return_value = None
    handler.update_features(_valid_event())
    args = lua_script.call_args.kwargs["args"]
    assert args[5] == "unknown"


def test_category_cache_hit_avoids_redis_hget(handler, lua_script, redis_client):
    """Second call for same item should use the cache, not Redis."""
    lua_script.return_value = "OK"
    redis_client.hget.return_value = "dress"
    handler.update_features(_valid_event(event_id="evt-1", item_id="item-cache"))
    handler.update_features(_valid_event(event_id="evt-2", item_id="item-cache"))
    # hget should only be called once (first time)
    assert redis_client.hget.call_count == 1


# ---------------------------------------------------------------------------
# Category LRU cache unit tests
# ---------------------------------------------------------------------------


def test_category_cache_miss_then_hit():
    cache = _CategoryCache(max_size=10, ttl_seconds=3600)
    cat, status = cache.get("item-1")
    assert status == "miss"
    assert cat is None
    cache.put("item-1", "dress")
    cat, status = cache.get("item-1")
    assert status == "hit"
    assert cat == "dress"


def test_category_cache_lru_eviction():
    cache = _CategoryCache(max_size=2, ttl_seconds=3600)
    cache.put("a", "dress")
    cache.put("b", "shoes")
    cache.put("c", "bags")  # evicts "a"
    _, status_a = cache.get("a")
    assert status_a == "miss"
    _, status_b = cache.get("b")
    assert status_b == "hit"
