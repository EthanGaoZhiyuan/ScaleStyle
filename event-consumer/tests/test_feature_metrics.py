"""
Tests for feature-quality metrics (P1.3)

Tests:
1. Category lookup hit/miss metrics
2. Unknown category events tracked
3. Lua materialization failure metrics
4. Feature update metrics by type
"""

import json
import sys
from pathlib import Path
from unittest.mock import Mock, patch
import pytest

# Add src to path
sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

from consumer import EventConsumer
from metrics import MetricsCollector


@pytest.fixture
def mock_redis():
    """Mock Redis client with script support"""
    redis_mock = Mock()
    redis_mock.ping = Mock(return_value=True)

    # Mock Lua script
    lua_script = Mock()
    redis_mock.register_script = Mock(return_value=lua_script)

    return redis_mock, lua_script


@pytest.fixture
def valid_event_with_category():
    """Valid event with known category"""
    return {
        "event_id": "evt_001",
        "event_type": "click",
        "user_id": "user_123",
        "item_id": "item_789",
        "timestamp": "2026-03-07T10:30:00Z",
        "session_id": "sess_abc",
        "source": "search",
    }


class TestCategoryMetrics:
    """Test category lookup metrics (P1.3)"""

    @patch("consumer.KafkaConsumer")
    def test_category_lookup_hit_increments_metric(
        self, mock_kafka, mock_redis, valid_event_with_category
    ):
        """Test that successful category lookup increments hit metric"""
        redis_client, lua_script = mock_redis
        lua_script.return_value = "OK"

        with patch("consumer.redis.Redis", return_value=redis_client):
            consumer = EventConsumer()
            consumer.redis_client = redis_client
            consumer.lua_upsert_script = lua_script

            # Mock category lookup to return a valid category
            redis_client.hget = Mock(return_value=b"dress")

            # Process event
            result = consumer.update_features(valid_event_with_category)

            # Assert
            assert result is True
            assert consumer.metrics.category_lookup_hit == 1
            assert consumer.metrics.category_lookup_miss == 0
            assert consumer.metrics.unknown_category_events == 0

    @patch("consumer.KafkaConsumer")
    def test_category_lookup_miss_increments_metric(
        self, mock_kafka, mock_redis, valid_event_with_category
    ):
        """Test that failed category lookup increments miss metric"""
        redis_client, lua_script = mock_redis
        lua_script.return_value = "OK"

        with patch("consumer.redis.Redis", return_value=redis_client):
            consumer = EventConsumer()
            consumer.redis_client = redis_client
            consumer.lua_upsert_script = lua_script

            # Mock category lookup to return None
            redis_client.hget = Mock(return_value=None)

            # Process event
            result = consumer.update_features(valid_event_with_category)

            # Assert
            assert result is True
            assert consumer.metrics.category_lookup_hit == 0
            assert consumer.metrics.category_lookup_miss == 1
            assert consumer.metrics.unknown_category_events == 1

    @patch("consumer.KafkaConsumer")
    def test_unknown_category_tracked(
        self, mock_kafka, mock_redis, valid_event_with_category
    ):
        """Test that unknown category is tracked separately"""
        redis_client, lua_script = mock_redis
        lua_script.return_value = "OK"

        with patch("consumer.redis.Redis", return_value=redis_client):
            consumer = EventConsumer()
            consumer.redis_client = redis_client
            consumer.lua_upsert_script = lua_script

            # Mock category lookup to raise exception
            redis_client.hget = Mock(side_effect=Exception("Redis error"))

            # Process event
            result = consumer.update_features(valid_event_with_category)

            # Assert
            assert result is True
            assert consumer.metrics.category_lookup_miss == 1
            assert consumer.metrics.unknown_category_events == 1


class TestLuaMetrics:
    """Test Lua materialization metrics (P1.3)"""

    @patch("consumer.KafkaConsumer")
    def test_lua_failure_tracked(
        self, mock_kafka, mock_redis, valid_event_with_category
    ):
        """Test that Lua script failure increments failure metric"""
        redis_client, lua_script = mock_redis

        # Mock Lua script to raise exception
        lua_script.side_effect = Exception("Lua script error")

        with patch("consumer.redis.Redis", return_value=redis_client):
            consumer = EventConsumer()
            consumer.redis_client = redis_client
            consumer.lua_upsert_script = lua_script
            redis_client.hget = Mock(return_value=b"dress")

            # Process event
            result = consumer.update_features(valid_event_with_category)

            # Assert
            assert result is False
            assert consumer.metrics.lua_materialization_failures == 1
            assert consumer.metrics.failed == 1


class TestMetricsExposition:
    """Test metrics exposed via HTTP endpoint (P1.3)"""

    def test_category_metrics_in_prometheus_format(self):
        """Test that category metrics appear in Prometheus format"""
        collector = MetricsCollector()
        collector.incr_category_hit()
        collector.incr_category_miss()
        collector.incr_unknown_category()
        collector.incr_lua_failure()

        prometheus_output = collector.to_prometheus()

        # Assert all metrics present
        assert "category_lookup_hit_total 1" in prometheus_output
        assert "category_lookup_miss_total 1" in prometheus_output
        assert "unknown_category_events_total 1" in prometheus_output
        assert "lua_materialization_failures_total 1" in prometheus_output

    def test_category_metrics_in_json_format(self):
        """Test that category metrics appear in JSON format"""
        collector = MetricsCollector()
        collector.incr_category_hit()
        collector.incr_category_miss()
        collector.incr_unknown_category()
        collector.incr_lua_failure()

        json_output = collector.to_json()
        data = json.loads(json_output)

        # Assert all metrics present
        assert data["category_lookup_hit_total"] == 1
        assert data["category_lookup_miss_total"] == 1
        assert data["unknown_category_events_total"] == 1
        assert data["lua_materialization_failures_total"] == 1


class TestDebugMode:
    """Test personalization debug mode (P1.5)"""

    def test_debug_mode_adds_metadata(self):
        """Test that debug mode adds _debug field to results"""
        # This test would go in inference-service tests
        # Placeholder to document requirement
        pass


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
