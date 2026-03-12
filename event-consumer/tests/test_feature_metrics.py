"""Unified metrics tests for current EventConsumer and prometheus_client metrics behavior."""

import os
import sys
from pathlib import Path
from unittest.mock import Mock, patch, MagicMock

import pytest

os.environ.setdefault("KAFKA_BOOTSTRAP_SERVERS", "test-kafka:9093")
os.environ.setdefault("CONSUMER_MODE", "primary")

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

from consumer import EventConsumer, ProcessingResult


@pytest.fixture
def consumer_ctx():
    with patch("consumer.MetricsServer") as mock_metrics_server_cls, patch(
        "consumer.redis.Redis"
    ) as mock_redis_cls, patch("consumer.KafkaConsumer") as mock_kafka_cls, patch(
        "consumer.KafkaProducer"
    ) as mock_kafka_producer_cls, patch(
        "consumer.metrics"
    ) as mock_metrics:

        redis_client = Mock()
        redis_client.ping.return_value = True
        redis_client.hget.return_value = "dress"

        lua_script = Mock()
        redis_client.register_script.return_value = lua_script
        mock_redis_cls.return_value = redis_client

        kafka_consumer = Mock()
        mock_kafka_cls.return_value = kafka_consumer

        kafka_producer = Mock()
        mock_kafka_producer_cls.return_value = kafka_producer

        metrics_server = Mock()
        mock_metrics_server_cls.return_value = metrics_server

        # Mock all prometheus metrics
        for attr in [
            "events_processed_total",
            "events_failed_total",
            "events_duplicate_total",
            "feature_updates_total",
            "feature_failures_total",
            "redis_operations_total",
            "event_processing_latency_seconds",
            "redis_update_latency_seconds",
        ]:
            metric_mock = MagicMock()
            metric_mock.labels.return_value = metric_mock
            metric_mock.inc.return_value = None
            metric_mock.observe.return_value = None
            setattr(mock_metrics, attr, metric_mock)

        consumer = EventConsumer()
        consumer.redis_client = redis_client
        consumer.consumer = kafka_consumer
        consumer.kafka_producer = kafka_producer
        consumer.lua_upsert_script = lua_script
        yield consumer, redis_client, lua_script, mock_metrics


@pytest.fixture
def valid_event():
    return {
        "event_id": "evt_001",
        "event_type": "click",
        "user_id": "user_123",
        "item_id": "item_789",
        "timestamp": "2026-03-07T10:30:00Z",
        "session_id": "sess_abc",
        "source": "search",
    }


class TestCategoryAndLuaMetrics:
    def test_category_lookup_hit_increments_metric(self, consumer_ctx, valid_event):
        consumer, redis_client, lua_script, mock_metrics = consumer_ctx
        lua_script.return_value = "OK"
        redis_client.hget.return_value = "dress"

        result = consumer.update_features(valid_event)

        assert result == ProcessingResult.APPLIED
        # Verify redis_operations_total was called with category_lookup success
        mock_metrics.redis_operations_total.labels.assert_any_call(
            operation="category_lookup", status="success"
        )

    def test_category_lookup_miss_and_unknown_increment(
        self, consumer_ctx, valid_event
    ):
        consumer, redis_client, lua_script, mock_metrics = consumer_ctx
        lua_script.return_value = "OK"
        redis_client.hget.return_value = None

        result = consumer.update_features(valid_event)

        assert result == ProcessingResult.APPLIED
        # Verify category lookup miss was tracked
        mock_metrics.redis_operations_total.labels.assert_any_call(
            operation="category_lookup", status="miss"
        )
        # Verify unknown category was tracked
        mock_metrics.feature_failures_total.labels.assert_any_call(
            feature="category_affinity"
        )

    def test_lua_failure_tracked(self, consumer_ctx, valid_event):
        consumer, redis_client, lua_script, mock_metrics = consumer_ctx
        redis_client.hget.return_value = b"dress"
        lua_script.side_effect = Exception("Lua script error")

        result = consumer.update_features(valid_event)

        # Lua errors are treated as transient failures
        assert result == ProcessingResult.TRANSIENT_FAILURE
        # Verify redis operations failure was tracked
        mock_metrics.redis_operations_total.labels.assert_any_call(
            operation="lua_decay_upsert", status="error"
        )


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
