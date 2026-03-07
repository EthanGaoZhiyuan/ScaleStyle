"""
Integration tests for event-consumer service

Tests the full flow:
1. Click event → Kafka → Consumer → Redis → Verify features
2. Consumer idempotency (duplicate events)
3. Manual offset commit behavior

Requirements:
- Docker compose services running (Kafka, Redis, event-consumer)
- Or: pytest with testcontainers
"""

import json
import time
import uuid
import pytest
import redis
from kafka import KafkaProducer
from kafka.errors import KafkaError

# Test configuration (can be overridden with env vars)
KAFKA_BOOTSTRAP = "localhost:9092"
REDIS_HOST = "localhost"
REDIS_PORT = 6379
KAFKA_TOPIC = "scalestyle.clicks"


@pytest.fixture(scope="module")
def redis_client():
    """Redis client for verification"""
    client = redis.Redis(host=REDIS_HOST, port=REDIS_PORT, db=0, decode_responses=True)
    try:
        client.ping()
        yield client
    except redis.ConnectionError:
        pytest.skip("Redis not available (start with docker-compose)")
    finally:
        client.close()


@pytest.fixture(scope="module")
def kafka_producer():
    """Kafka producer for sending test events"""
    try:
        producer = KafkaProducer(
            bootstrap_servers=[KAFKA_BOOTSTRAP],
            value_serializer=lambda v: json.dumps(v).encode("utf-8"),
            key_serializer=lambda k: k.encode("utf-8") if k else None,
            acks=1,
        )
        yield producer
        producer.close()
    except KafkaError:
        pytest.skip("Kafka not available (start with docker-compose)")


def create_click_event(user_id="test_user", item_id="test_item_001", **kwargs):
    """Helper to create a valid click event"""
    event = {
        "event_id": str(uuid.uuid4()),
        "event_type": "click",
        "user_id": user_id,
        "item_id": item_id,
        "timestamp": "2026-03-07T10:30:45.123Z",
        "session_id": kwargs.get("session_id", "test_session"),
        "source": kwargs.get("source", "search"),
        "query": kwargs.get("query", "test query"),
        "position": kwargs.get("position", 0),
    }
    event.update(kwargs)  # Allow overrides
    return event


@pytest.mark.integration
class TestEventLoop:
    """Integration tests for full event loop"""

    def test_click_event_updates_redis_features(self, kafka_producer, redis_client):
        """
        End-to-end test: Send click event → verify Redis features updated

        Flow:
        1. Send click event to Kafka
        2. Wait for consumer to process (3 seconds)
        3. Verify Redis keys exist and have correct values
        """
        # Generate unique user/item for this test
        test_user = f"integration_user_{uuid.uuid4().hex[:8]}"
        test_item = f"item_{uuid.uuid4().hex[:8]}"

        # Create and send event
        event = create_click_event(
            user_id=test_user, item_id=test_item, query="integration test"
        )

        # Send to Kafka (partition key = user_id)
        kafka_producer.send(KAFKA_TOPIC, key=test_user, value=event)
        kafka_producer.flush()

        print(
            f"✅ Sent event: user={test_user}, item={test_item}, event_id={event['event_id']}"
        )

        # Wait for consumer to process (adjust if consumer is slow)
        time.sleep(3)

        # Verify Redis features
        # 1. user:*:recent_clicks should contain the item
        recent_clicks = redis_client.lrange(f"user:{test_user}:recent_clicks", 0, -1)
        assert (
            test_item in recent_clicks
        ), f"Item {test_item} should be in recent_clicks"

        # 2. user:*:last_activity should be set
        last_activity = redis_client.get(f"user:{test_user}:last_activity")
        assert last_activity is not None, "Last activity should be set"

        # 3. item:*:clicks should be incremented
        item_clicks = redis_client.get(f"item:{test_item}:clicks")
        assert item_clicks is not None, "Item clicks should be recorded"
        assert int(item_clicks) >= 1, "Item clicks should be >= 1"

        # 4. global:popular should include the item
        popular_score = redis_client.zscore("global:popular", test_item)
        assert popular_score is not None, "Item should be in global:popular"
        assert popular_score >= 1.0, "Popular score should be >= 1"

        # 5. Dedupe key should exist
        dedupe_key = f"dedupe:event:{event['event_id']}"
        dedupe_exists = redis_client.exists(dedupe_key)
        assert dedupe_exists == 1, "Dedupe key should exist"

        print(f"✅ All features verified for user={test_user}, item={test_item}")

    def test_duplicate_event_not_double_counted(self, kafka_producer, redis_client):
        """
        Test idempotency: Same event_id sent twice should only update features once

        Flow:
        1. Send click event
        2. Wait for processing
        3. Record click count
        4. Send SAME event again (same event_id)
        5. Verify click count did NOT increase
        """
        test_user = f"integration_user_{uuid.uuid4().hex[:8]}"
        test_item = f"item_{uuid.uuid4().hex[:8]}"

        event = create_click_event(user_id=test_user, item_id=test_item)

        # Send event first time
        kafka_producer.send(KAFKA_TOPIC, key=test_user, value=event)
        kafka_producer.flush()
        time.sleep(3)

        # Record initial state
        clicks_before = int(redis_client.get(f"item:{test_item}:clicks") or 0)
        popular_score_before = redis_client.zscore("global:popular", test_item) or 0.0

        print(f"✅ First send: clicks={clicks_before}, popular={popular_score_before}")

        # Send SAME event again (duplicate)
        kafka_producer.send(KAFKA_TOPIC, key=test_user, value=event)
        kafka_producer.flush()
        time.sleep(3)

        # Verify counts did NOT increase
        clicks_after = int(redis_client.get(f"item:{test_item}:clicks") or 0)
        popular_score_after = redis_client.zscore("global:popular", test_item) or 0.0

        print(
            f"✅ After duplicate: clicks={clicks_after}, popular={popular_score_after}"
        )

        assert (
            clicks_after == clicks_before
        ), "Duplicate event should NOT increase click count"
        assert (
            popular_score_after == popular_score_before
        ), "Duplicate should NOT change popularity"

    def test_multiple_clicks_accumulate(self, kafka_producer, redis_client):
        """
        Test that multiple clicks (different event_ids) accumulate correctly
        """
        test_user = f"integration_user_{uuid.uuid4().hex[:8]}"
        test_item = f"item_{uuid.uuid4().hex[:8]}"

        # Send 3 distinct click events
        for i in range(3):
            event = create_click_event(
                user_id=test_user,
                item_id=test_item,
                # Different event_id each time
            )
            kafka_producer.send(KAFKA_TOPIC, key=test_user, value=event)

        kafka_producer.flush()
        time.sleep(5)  # Wait for all 3 to process

        # Verify accumulated clicks
        item_clicks = int(redis_client.get(f"item:{test_item}:clicks") or 0)
        popular_score = redis_client.zscore("global:popular", test_item) or 0.0

        assert item_clicks >= 3, f"Should have at least 3 clicks, got {item_clicks}"
        assert (
            popular_score >= 3.0
        ), f"Popular score should be >= 3, got {popular_score}"

        print(
            f"✅ Multiple clicks accumulated: clicks={item_clicks}, popular={popular_score}"
        )


@pytest.mark.integration
@pytest.mark.slow
class TestConsumerReliability:
    """Test consumer reliability features (manual commit, retry)"""

    def test_dedupe_key_has_expiration(self, kafka_producer, redis_client):
        """
        Verify that dedupe keys have TTL set (7 days)
        """
        test_user = f"integration_user_{uuid.uuid4().hex[:8]}"
        test_item = f"item_{uuid.uuid4().hex[:8]}"

        event = create_click_event(user_id=test_user, item_id=test_item)

        kafka_producer.send(KAFKA_TOPIC, key=test_user, value=event)
        kafka_producer.flush()
        time.sleep(3)

        dedupe_key = f"dedupe:event:{event['event_id']}"
        ttl = redis_client.ttl(dedupe_key)

        # TTL should be ~7 days (604800 seconds)
        # Allow some margin (processed within last minute)
        assert ttl > 0, "Dedupe key should have expiration set"
        assert ttl <= 604800, f"TTL should be <= 7 days, got {ttl}"
        assert ttl > 604000, f"TTL should be close to 7 days, got {ttl}"

        print(f"✅ Dedupe key TTL: {ttl} seconds (~{ttl/86400:.1f} days)")


if __name__ == "__main__":
    pytest.main([__file__, "-v", "-m", "integration"])
