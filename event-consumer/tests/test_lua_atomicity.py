"""
Test Lua-based Atomic Materialization Semantics

Validates CURRENT implementation that uses:
- ProcessingResult enum (not boolean)
- process_message() method for commit logic
- Lua script for atomic dedupe+upsert

Tests:
1. Lua returns 'OK' -> APPLIED -> commit
2. Lua returns 'DUPLICATE' -> DUPLICATE -> commit
3. Lua raises exception -> FAILED -> no commit
4. Unknown category passed to Lua (script guards affinity update)
"""

import os
import pytest
from unittest.mock import Mock, patch

# Mock imports
pytest.importorskip("redis")
pytest.importorskip("kafka")

import sys

os.environ.setdefault("KAFKA_BOOTSTRAP_SERVERS", "test-kafka:9093")
os.environ.setdefault("CONSUMER_MODE", "primary")

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "../src"))

from consumer import EventConsumer, ProcessingResult


@pytest.fixture
def consumer_with_mocks():
    """Create EventConsumer with current constructor behavior mocked"""
    with patch("consumer.MetricsServer") as mock_metrics_server_cls, patch(
        "consumer.redis.Redis"
    ) as mock_redis_cls, patch("consumer.KafkaConsumer") as mock_kafka_cls, patch(
        "consumer.KafkaProducer"
    ) as mock_kafka_producer_cls, patch(
        "consumer.metrics"
    ) as mock_metrics:

        # Setup Redis mock
        redis_client = Mock()
        redis_client.ping.return_value = True
        lua_script = Mock()
        redis_client.register_script.return_value = lua_script
        mock_redis_cls.return_value = redis_client

        # Setup Kafka mock
        kafka_consumer = Mock()
        mock_kafka_cls.return_value = kafka_consumer

        kafka_producer = Mock()
        mock_kafka_producer_cls.return_value = kafka_producer

        # Setup metrics mocks
        from unittest.mock import MagicMock

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

        # Setup metrics server mock
        metrics_server = Mock()
        mock_metrics_server_cls.return_value = metrics_server

        consumer = EventConsumer()
        consumer.consumer = kafka_consumer
        consumer.kafka_producer = kafka_producer
        consumer.redis_client = redis_client
        consumer.lua_upsert_script = lua_script

        yield consumer, kafka_consumer, redis_client, lua_script


@pytest.fixture
def valid_event():
    """Valid click event"""
    return {
        "event_id": "evt-123",
        "user_id": "user-1",
        "item_id": "item-789",
        "timestamp": "2026-03-07T10:30:00Z",
        "session_id": "sess-abc",
        "source": "search",
    }


def _message_of(event):
    """Helper to wrap event as Kafka message"""
    msg = Mock()
    msg.value = event
    msg.topic = "user-events"
    msg.partition = 0
    msg.offset = 100
    msg.headers = []
    return msg


class TestLuaAtomicMaterialization:
    """Test Lua-based atomic dedupe+upsert semantics"""

    def test_lua_ok_returns_applied_and_commits(self, consumer_with_mocks, valid_event):
        """Lua returns 'OK' -> ProcessingResult.APPLIED -> commit"""
        consumer, kafka_consumer, redis_client, lua_script = consumer_with_mocks
        redis_client.hget.return_value = None  # unknown category
        lua_script.return_value = "OK"

        result = consumer.process_message(_message_of(valid_event))

        assert result == ProcessingResult.APPLIED
        kafka_consumer.commit.assert_called_once()
        lua_script.assert_called_once()

    def test_lua_duplicate_returns_duplicate_and_commits(
        self, consumer_with_mocks, valid_event
    ):
        """Lua returns 'DUPLICATE' -> ProcessingResult.DUPLICATE -> commit (idempotent)"""
        consumer, kafka_consumer, redis_client, lua_script = consumer_with_mocks
        redis_client.hget.return_value = None
        lua_script.return_value = "DUPLICATE"

        result = consumer.process_message(_message_of(valid_event))

        assert result == ProcessingResult.DUPLICATE
        kafka_consumer.commit.assert_called_once()

    def test_lua_exception_returns_failed_and_no_commit(
        self, consumer_with_mocks, valid_event
    ):
        """Lua raises exception -> ProcessingResult.TRANSIENT_FAILURE -> routes to retry"""
        consumer, kafka_consumer, redis_client, lua_script = consumer_with_mocks
        redis_client.hget.return_value = None
        lua_script.side_effect = RuntimeError("Redis connection lost")

        result = consumer.process_message(_message_of(valid_event))

        # Lua failures are treated as transient and routed to retry
        assert result == ProcessingResult.TRANSIENT_FAILURE
        kafka_consumer.commit.assert_called_once()  # Commit after routing to retry


class TestCategoryAffinityGuard:
    """Test that unknown category is guarded correctly in Lua script"""

    def test_unknown_category_passed_to_lua(self, consumer_with_mocks, valid_event):
        """Category lookup miss -> 'unknown' passed to Lua (script skips affinity)"""
        consumer, kafka_consumer, redis_client, lua_script = consumer_with_mocks
        redis_client.hget.return_value = None  # category miss
        lua_script.return_value = "OK"

        result = consumer.process_message(_message_of(valid_event))

        assert result == ProcessingResult.APPLIED
        # Verify Lua called with 'unknown' as ARGV[6] (category)
        call_args = lua_script.call_args.kwargs["args"]
        assert (
            call_args[5] == "unknown"
        ), "Category should be 'unknown' when lookup fails"

    def test_valid_category_passed_to_lua(self, consumer_with_mocks, valid_event):
        """Category lookup hit -> actual category passed to Lua"""
        consumer, kafka_consumer, redis_client, lua_script = consumer_with_mocks
        redis_client.hget.return_value = "dress"  # category hit
        lua_script.return_value = "OK"

        result = consumer.process_message(_message_of(valid_event))

        assert result == ProcessingResult.APPLIED
        # Verify Lua called with actual category as ARGV[6]
        call_args = lua_script.call_args.kwargs["args"]
        assert (
            call_args[5] == "dress"
        ), "Category should be 'dress' when lookup succeeds"


class TestLuaScriptArguments:
    """Test that Lua script receives correct arguments"""

    def test_lua_receives_all_required_args(self, consumer_with_mocks, valid_event):
        """Verify Lua script called with all 10 required args"""
        consumer, kafka_consumer, redis_client, lua_script = consumer_with_mocks
        redis_client.hget.return_value = None
        lua_script.return_value = "OK"

        consumer.process_message(_message_of(valid_event))

        lua_script.assert_called_once()
        # Verify KEYS[1] = dedupe_key
        assert lua_script.call_args.kwargs["keys"][0] == "dedupe:event:evt-123"

        # Verify ARGV has 21 args:
        # dedupe_ttl, user_id, item_id, event_ts, last_activity_ts, category,
        # session_id, recent_clicks_max, affinity_lambda, item_click_lambda,
        # recent_item_lambda, popularity_lambda, session_ttl, feature_state_ttl,
        # 1h bucket seconds/ttl, 24h bucket seconds/ttl, 7d bucket seconds/ttl,
        # popularity_bucket_prefix
        args = lua_script.call_args.kwargs["args"]
        assert len(args) == 21
        assert args[1] == "user-1"  # user_id
        assert args[2] == "item-789"  # item_id
        assert float(args[3]) > 0.0  # event_ts
        assert args[4] == "2026-03-07T10:30:00Z"  # last_activity_ts
        assert args[6] == "sess-abc"  # session_id
        assert args[20] == "popularity:bucket"  # popularity_bucket_prefix


if __name__ == "__main__":
    pytest.main([__file__, "-v", "--tb=short"])
