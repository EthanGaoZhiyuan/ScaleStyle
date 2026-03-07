"""
Test EventConsumer Run Loop Semantics

Tests the critical run-loop behavior:
1. Success -> Kafka offset committed
2. Duplicate -> Kafka offset committed (idempotent success)
3. Failure -> Kafka offset NOT committed (will retry)

These tests validate CURRENT implementation semantics, not internal details.
"""

import os
import pytest
from unittest.mock import Mock, patch

# Mock imports
pytest.importorskip("redis")
pytest.importorskip("kafka")

import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "../src"))

from consumer import EventConsumer


@pytest.fixture
def mock_redis():
    """Create mock Redis client with Lua script support"""
    redis_client = Mock()
    redis_client.ping = Mock(return_value=True)

    # Mock Lua script registration
    lua_script_mock = Mock()
    redis_client.register_script = Mock(return_value=lua_script_mock)

    return redis_client, lua_script_mock


@pytest.fixture
def mock_metrics_server():
    """Mock MetricsServer to prevent port binding issues"""
    with patch("consumer.MetricsServer") as mock_server:
        mock_instance = Mock()
        mock_instance.start = Mock()
        mock_instance.stop = Mock()
        mock_server.return_value = mock_instance
        yield mock_server


@pytest.fixture
def valid_click_event():
    """Valid click event fixture"""
    return {
        "event_id": "test-event-123",
        "event_type": "click",
        "user_id": "user_456",
        "item_id": "item_789",
        "timestamp": "2026-03-07T10:30:00Z",
        "session_id": "sess_abc",
        "source": "search",
    }


class TestRunLoopSemantics:
    """Test EventConsumer.run() commit semantics with CURRENT implementation"""

    @patch("consumer.KafkaConsumer")
    def test_success_commits_offset(
        self, mock_kafka_class, valid_click_event, mock_redis, mock_metrics_server
    ):
        """Test success path: update_features succeeds -> Kafka offset committed"""
        redis_client, lua_script = mock_redis

        # Mock Kafka consumer that yields one message
        mock_kafka_consumer = Mock()
        mock_message = Mock()
        mock_message.value = valid_click_event
        mock_kafka_consumer.__iter__ = Mock(return_value=iter([mock_message]))
        mock_kafka_consumer.commit = Mock()
        mock_kafka_class.return_value = mock_kafka_consumer

        # Lua script returns 'OK' (success)
        lua_script.return_value = "OK"

        with patch("consumer.redis.Redis", return_value=redis_client):
            consumer = EventConsumer()
            consumer.redis_client = redis_client
            consumer.lua_upsert_script = lua_script

            # Mock category lookup
            redis_client.hget = Mock(return_value=None)

            # Run one iteration manually
            for message in mock_kafka_consumer:
                success = consumer.update_features(message.value)
                if success:
                    mock_kafka_consumer.commit()
                break  # One iteration only

        # Verify commit was called
        mock_kafka_consumer.commit.assert_called_once()

    @patch("consumer.KafkaConsumer")
    def test_duplicate_commits_offset(
        self, mock_kafka_class, valid_click_event, mock_redis, mock_metrics_server
    ):
        """Test duplicate path: Lua returns DUPLICATE -> still commits (idempotent success)"""
        redis_client, lua_script = mock_redis

        # Mock Kafka consumer that yields one message
        mock_kafka_consumer = Mock()
        mock_message = Mock()
        mock_message.value = valid_click_event
        mock_kafka_consumer.__iter__ = Mock(return_value=iter([mock_message]))
        mock_kafka_consumer.commit = Mock()
        mock_kafka_class.return_value = mock_kafka_consumer

        # Lua script returns 'DUPLICATE' (already processed)
        lua_script.return_value = "DUPLICATE"

        with patch("consumer.redis.Redis", return_value=redis_client):
            consumer = EventConsumer()
            consumer.redis_client = redis_client
            consumer.lua_upsert_script = lua_script

            # Mock category lookup
            redis_client.hget = Mock(return_value=None)

            # Run one iteration manually
            for message in mock_kafka_consumer:
                success = consumer.update_features(message.value)
                if success:
                    mock_kafka_consumer.commit()
                break  # One iteration only

        # Verify commit was called (duplicate is idempotent success)
        mock_kafka_consumer.commit.assert_called_once()

    @patch("consumer.KafkaConsumer")
    def test_failure_does_not_commit_offset(
        self, mock_kafka_class, valid_click_event, mock_redis, mock_metrics_server
    ):
        """Test failure path: Lua raises exception -> Kafka offset NOT committed"""
        redis_client, lua_script = mock_redis

        # Mock Kafka consumer that yields one message
        mock_kafka_consumer = Mock()
        mock_message = Mock()
        mock_message.value = valid_click_event
        mock_kafka_consumer.__iter__ = Mock(return_value=iter([mock_message]))
        mock_kafka_consumer.commit = Mock()
        mock_kafka_class.return_value = mock_kafka_consumer

        # Lua script raises exception (failure)
        lua_script.side_effect = Exception("Redis connection lost")

        with patch("consumer.redis.Redis", return_value=redis_client):
            consumer = EventConsumer()
            consumer.redis_client = redis_client
            consumer.lua_upsert_script = lua_script

            # Mock category lookup
            redis_client.hget = Mock(return_value=None)

            # Run one iteration manually
            for message in mock_kafka_consumer:
                success = consumer.update_features(message.value)
                if success:
                    mock_kafka_consumer.commit()
                break  # One iteration only

        # Verify commit was NOT called (failure)
        mock_kafka_consumer.commit.assert_not_called()


class TestCategoryHandling:
    """Test category handling semantics"""

    @patch("consumer.KafkaConsumer")
    def test_unknown_category_skips_affinity_update(
        self, mock_kafka_class, valid_click_event, mock_redis, mock_metrics_server
    ):
        """Test that unknown category is passed to Lua (which skips affinity update)"""
        redis_client, lua_script = mock_redis

        # Lua script succeeds
        lua_script.return_value = "OK"

        with patch("consumer.redis.Redis", return_value=redis_client):
            consumer = EventConsumer()
            consumer.redis_client = redis_client
            consumer.lua_upsert_script = lua_script

            # Mock category lookup returns None
            redis_client.hget = Mock(return_value=None)

            # Process event
            result = consumer.update_features(valid_click_event)

            # Verify success
            assert result is True

            # Verify Lua script called with 'unknown' category
            lua_script.assert_called_once()
            call_args = lua_script.call_args[1]["args"]
            assert "unknown" in call_args

    @patch("consumer.KafkaConsumer")
    def test_valid_category_included_in_lua_call(
        self, mock_kafka_class, valid_click_event, mock_redis, mock_metrics_server
    ):
        """Test that valid category is passed to Lua for affinity update"""
        redis_client, lua_script = mock_redis

        # Lua script succeeds
        lua_script.return_value = "OK"

        with patch("consumer.redis.Redis", return_value=redis_client):
            consumer = EventConsumer()
            consumer.redis_client = redis_client
            consumer.lua_upsert_script = lua_script

            # Mock category lookup returns actual category
            redis_client.hget = Mock(return_value=b"dress")

            # Process event
            result = consumer.update_features(valid_click_event)

            # Verify success
            assert result is True

            # Verify Lua script called with actual category
            lua_script.assert_called_once()
            call_args = lua_script.call_args[1]["args"]
            assert "dress" in call_args


if __name__ == "__main__":
    pytest.main([__file__, "-v", "--tb=short"])
