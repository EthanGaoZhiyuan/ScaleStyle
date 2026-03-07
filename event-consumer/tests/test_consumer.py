"""
Unit tests for EventConsumer

Tests aligned with current Lua-based atomic materialization:
1. Valid click event -> Lua script succeeds -> features updated
2. Malformed events (missing required fields) are rejected
3. Duplicate event_id -> Lua returns 'DUPLICATE' -> no double feature update
4. Feature update returns correct success/failure status
5. Redis/Lua failure -> no Kafka commit
"""

import sys
from pathlib import Path
from unittest.mock import Mock, patch
import pytest

# Add src to path
sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

from consumer import EventConsumer


@pytest.fixture
def mock_redis():
    """Mock Redis client with Lua script support"""
    redis_mock = Mock()
    redis_mock.ping = Mock(return_value=True)

    # Mock Lua script registration
    lua_script_mock = Mock()
    redis_mock.register_script = Mock(return_value=lua_script_mock)

    return redis_mock, lua_script_mock


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
        "event_id": "550e8400-e29b-41d4-a716-446655440000",
        "event_type": "click",
        "user_id": "user_12345",
        "item_id": "108775051",
        "timestamp": "2026-03-07T10:30:45.123Z",
        "session_id": "sess_abc123",
        "source": "search",
        "query": "red dress",
        "position": 2,
    }


class TestEventConsumer:
    """Test EventConsumer class with current Lua-based implementation"""

    @patch("consumer.KafkaConsumer")
    def test_valid_event_updates_features(
        self, mock_kafka, valid_click_event, mock_redis, mock_metrics_server
    ):
        """Test that a valid click event triggers Lua materialization and succeeds"""
        redis_client, lua_script = mock_redis

        # Lua script returns 'OK' (successful materialization)
        lua_script.return_value = "OK"

        with patch("consumer.redis.Redis", return_value=redis_client):
            consumer = EventConsumer()
            consumer.redis_client = redis_client
            consumer.lua_upsert_script = lua_script

            # Mock category lookup
            redis_client.hget = Mock(return_value=b"dress")

            # Process event
            result = consumer.update_features(valid_click_event)

            # Assertions
            assert result is True, "Valid event should return True"

            # Verify Lua script was called (atomic materialization)
            lua_script.assert_called_once()
            call_args = lua_script.call_args

            # Verify keys argument contains dedupe key
            assert (
                "dedupe:event:550e8400-e29b-41d4-a716-446655440000"
                in call_args[1]["keys"][0]
            )

            # Verify args contain user_id, item_id
            args = call_args[1]["args"]
            assert "user_12345" in args
            assert "108775051" in args

    @patch("consumer.KafkaConsumer")
    def test_missing_event_id_rejected(
        self, mock_kafka, mock_redis, mock_metrics_server
    ):
        """Test that event without event_id is rejected before Lua execution"""
        redis_client, lua_script = mock_redis

        with patch("consumer.redis.Redis", return_value=redis_client):
            consumer = EventConsumer()
            consumer.redis_client = redis_client
            consumer.lua_upsert_script = lua_script

            invalid_event = {
                "user_id": "user_12345",
                "item_id": "108775051",
                "timestamp": "2026-03-07T10:30:45.123Z",
                # Missing event_id
            }

            result = consumer.update_features(invalid_event)

            assert result is False, "Event missing event_id should be rejected"
            lua_script.assert_not_called()

    @patch("consumer.KafkaConsumer")
    def test_missing_user_id_rejected(
        self, mock_kafka, mock_redis, mock_metrics_server
    ):
        """Test that event without user_id is rejected before Lua execution"""
        redis_client, lua_script = mock_redis

        with patch("consumer.redis.Redis", return_value=redis_client):
            consumer = EventConsumer()
            consumer.redis_client = redis_client
            consumer.lua_upsert_script = lua_script

            invalid_event = {
                "event_id": "550e8400-e29b-41d4-a716-446655440000",
                "item_id": "108775051",
                "timestamp": "2026-03-07T10:30:45.123Z",
                # Missing user_id
            }

            result = consumer.update_features(invalid_event)

            assert result is False, "Event missing user_id should be rejected"
            lua_script.assert_not_called()

    @patch("consumer.KafkaConsumer")
    def test_missing_item_id_rejected(
        self, mock_kafka, mock_redis, mock_metrics_server
    ):
        """Test that event without item_id is rejected before Lua execution"""
        redis_client, lua_script = mock_redis

        with patch("consumer.redis.Redis", return_value=redis_client):
            consumer = EventConsumer()
            consumer.redis_client = redis_client
            consumer.lua_upsert_script = lua_script

            invalid_event = {
                "event_id": "550e8400-e29b-41d4-a716-446655440000",
                "user_id": "user_12345",
                "timestamp": "2026-03-07T10:30:45.123Z",
                # Missing item_id
            }

            result = consumer.update_features(invalid_event)

            assert result is False, "Event missing item_id should be rejected"
            lua_script.assert_not_called()

    @patch("consumer.KafkaConsumer")
    def test_duplicate_event_id_skipped(
        self, mock_kafka, valid_click_event, mock_redis, mock_metrics_server
    ):
        """Test that duplicate event_id is detected by Lua and skipped (idempotency)"""
        redis_client, lua_script = mock_redis

        # Lua script returns 'DUPLICATE' (dedupe key already exists)
        lua_script.return_value = "DUPLICATE"

        with patch("consumer.redis.Redis", return_value=redis_client):
            consumer = EventConsumer()
            consumer.redis_client = redis_client
            consumer.lua_upsert_script = lua_script
            consumer.deduped_count = 0

            # Mock category lookup
            redis_client.hget = Mock(return_value=b"dress")

            result = consumer.update_features(valid_click_event)

            # Should return True (idempotent, not an error), but not update features
            assert result is True, "Duplicate event should return True (idempotent)"
            assert consumer.deduped_count == 1, "Deduped counter should increment"

            # Verify Lua script was called (it handles dedupe internally)
            lua_script.assert_called_once()

    @patch("consumer.KafkaConsumer")
    def test_redis_lua_failure_returns_false(
        self, mock_kafka, valid_click_event, mock_redis, mock_metrics_server
    ):
        """Test that Lua script failure returns False (no commit)"""
        redis_client, lua_script = mock_redis

        # Simulate Lua script failure
        lua_script.side_effect = Exception("Lua script execution failed")

        with patch("consumer.redis.Redis", return_value=redis_client):
            consumer = EventConsumer()
            consumer.redis_client = redis_client
            consumer.lua_upsert_script = lua_script
            consumer.error_count = 0

            # Mock category lookup
            redis_client.hget = Mock(return_value=b"dress")

            result = consumer.update_features(valid_click_event)

            assert result is False, "Lua failure should return False (no commit)"
            assert consumer.error_count == 1, "Error counter should increment"

    @patch("consumer.KafkaConsumer")
    def test_category_lookup_failure_continues_with_unknown(
        self, mock_kafka, valid_click_event, mock_redis, mock_metrics_server
    ):
        """Test that category lookup failure continues with 'unknown' category"""
        redis_client, lua_script = mock_redis

        # Lua script succeeds
        lua_script.return_value = "OK"

        with patch("consumer.redis.Redis", return_value=redis_client):
            consumer = EventConsumer()
            consumer.redis_client = redis_client
            consumer.lua_upsert_script = lua_script

            # Mock category lookup failure
            redis_client.hget = Mock(side_effect=Exception("Redis error"))

            result = consumer.update_features(valid_click_event)

            # Should still succeed (category is optional)
            assert result is True, "Should succeed with unknown category"

            # Verify Lua was called with 'unknown' category
            lua_script.assert_called_once()
            call_args = lua_script.call_args[1]["args"]
            assert "unknown" in call_args


class TestCategoryInference:
    """Test category inference helper"""

    @patch("consumer.KafkaConsumer")
    def test_category_inference_fallback(
        self, mock_kafka, mock_redis, mock_metrics_server
    ):
        """Test that missing category metadata falls back to 'unknown'"""
        redis_client, lua_script = mock_redis

        with patch("consumer.redis.Redis", return_value=redis_client):
            consumer = EventConsumer()
            consumer.redis_client = redis_client
            consumer.lua_upsert_script = lua_script

            # Mock: no category metadata found
            redis_client.hget = Mock(return_value=None)

            category = consumer._infer_category("108775051")

            assert (
                category == "unknown"
            ), "Should return 'unknown' when metadata missing"
            redis_client.hget.assert_called_once_with("item:108775051:meta", "category")

    @patch("consumer.KafkaConsumer")
    def test_category_inference_success(
        self, mock_kafka, mock_redis, mock_metrics_server
    ):
        """Test that category lookup succeeds when metadata exists"""
        redis_client, lua_script = mock_redis

        with patch("consumer.redis.Redis", return_value=redis_client):
            consumer = EventConsumer()
            consumer.redis_client = redis_client
            consumer.lua_upsert_script = lua_script

            # Mock: category metadata found
            redis_client.hget = Mock(return_value=b"dress")

            category = consumer._infer_category("108775051")

            assert category == "dress", "Should return actual category"


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
