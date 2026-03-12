"""Unit tests for _CategoryCache — in-process LRU + TTL cache for item categories.

Tests are isolated from Redis and EventConsumer; they exercise only the cache
class itself so that correctness of the caching contract is verified
independently of consumer integration.
"""

import os
import sys
import time
from pathlib import Path
from unittest.mock import Mock, patch

import pytest

# CONSUMER_MODE is required by config.py at import time.
os.environ.setdefault("CONSUMER_MODE", "primary")
os.environ.setdefault("KAFKA_BOOTSTRAP_SERVERS", "test-kafka:9093")

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

from consumer import _CategoryCache, EventConsumer, ProcessingResult


# ── _CategoryCache unit tests ─────────────────────────────────────────────────

class TestCategoryCacheGet:
    def test_miss_on_empty_cache(self):
        cache = _CategoryCache(max_size=10, ttl_seconds=60)
        category, status = cache.get("item-1")
        assert category is None
        assert status == "miss"

    def test_hit_after_put(self):
        cache = _CategoryCache(max_size=10, ttl_seconds=60)
        cache.put("item-1", "dress")
        category, status = cache.get("item-1")
        assert category == "dress"
        assert status == "hit"

    def test_hit_for_unknown_category(self):
        """'unknown' is a valid cached value, not a sentinel for absence."""
        cache = _CategoryCache(max_size=10, ttl_seconds=60)
        cache.put("item-1", "unknown")
        category, status = cache.get("item-1")
        assert category == "unknown"
        assert status == "hit"

    def test_expired_entry_returns_expired_status(self):
        cache = _CategoryCache(max_size=10, ttl_seconds=0.01)
        cache.put("item-1", "dress")
        time.sleep(0.05)  # let TTL elapse
        category, status = cache.get("item-1")
        assert category is None
        assert status == "expired"

    def test_expired_entry_is_evicted_from_store(self):
        cache = _CategoryCache(max_size=10, ttl_seconds=0.01)
        cache.put("item-1", "dress")
        time.sleep(0.05)
        cache.get("item-1")
        assert len(cache) == 0

    def test_miss_for_unknown_item(self):
        cache = _CategoryCache(max_size=10, ttl_seconds=60)
        category, status = cache.get("no-such-item")
        assert category is None
        assert status == "miss"


class TestCategoryCacheLRUEviction:
    def test_evicts_lru_entry_when_full(self):
        cache = _CategoryCache(max_size=3, ttl_seconds=60)
        cache.put("a", "cat-a")
        cache.put("b", "cat-b")
        cache.put("c", "cat-c")

        # "a" is LRU; adding "d" should evict it
        cache.put("d", "cat-d")

        assert len(cache) == 3
        _, status_a = cache.get("a")
        assert status_a == "miss", "LRU entry 'a' should have been evicted"
        _, status_d = cache.get("d")
        assert status_d == "hit", "'d' should still be present"

    def test_accessing_entry_promotes_it_from_lru_position(self):
        cache = _CategoryCache(max_size=3, ttl_seconds=60)
        cache.put("a", "cat-a")
        cache.put("b", "cat-b")
        cache.put("c", "cat-c")

        # Access "a" to promote it; "b" is now LRU
        cache.get("a")

        cache.put("d", "cat-d")  # should evict "b", not "a"

        _, status_a = cache.get("a")
        assert status_a == "hit", "'a' was promoted and must survive eviction"
        _, status_b = cache.get("b")
        assert status_b == "miss", "'b' was LRU after 'a' was promoted"

    def test_put_existing_key_does_not_grow_cache(self):
        cache = _CategoryCache(max_size=3, ttl_seconds=60)
        cache.put("a", "cat-a")
        cache.put("a", "cat-a-updated")
        assert len(cache) == 1

    def test_put_existing_key_updates_value(self):
        cache = _CategoryCache(max_size=10, ttl_seconds=60)
        cache.put("a", "dress")
        cache.put("a", "sportswear")
        category, status = cache.get("a")
        assert category == "sportswear"
        assert status == "hit"

    def test_size_at_capacity(self):
        cache = _CategoryCache(max_size=5, ttl_seconds=60)
        for i in range(5):
            cache.put(f"item-{i}", f"cat-{i}")
        assert len(cache) == 5


# ── Integration: _infer_category uses the cache ───────────────────────────────

@pytest.fixture
def consumer_ctx():
    """Minimal consumer fixture for testing _infer_category."""
    with patch("consumer.MetricsServer"), \
         patch("consumer.redis.Redis") as mock_redis_cls, \
         patch("consumer.KafkaConsumer"), \
         patch("consumer.KafkaProducer"):

        redis_client = Mock()
        redis_client.ping.return_value = True
        redis_client.register_script.return_value = Mock()
        mock_redis_cls.return_value = redis_client

        consumer = EventConsumer()
        yield consumer, redis_client


class TestInferCategoryWithCache:
    def test_first_call_hits_redis(self, consumer_ctx):
        consumer, redis_client = consumer_ctx
        redis_client.hget.return_value = "dress"

        result = consumer._infer_category("item-1")

        assert result == "dress"
        redis_client.hget.assert_called_once_with("item:item-1:meta", "category")

    def test_numeric_item_id_is_normalized_to_canonical_meta_key(self, consumer_ctx):
        consumer, redis_client = consumer_ctx
        redis_client.hget.return_value = "dress"

        result = consumer._infer_category("123456")

        assert result == "dress"
        redis_client.hget.assert_called_once_with("item:0000123456:meta", "category")

    def test_second_call_served_from_cache_no_redis(self, consumer_ctx):
        consumer, redis_client = consumer_ctx
        redis_client.hget.return_value = "dress"

        consumer._infer_category("item-1")
        redis_client.hget.reset_mock()

        result = consumer._infer_category("item-1")

        assert result == "dress"
        redis_client.hget.assert_not_called()

    def test_unknown_cached_prevents_repeated_redis_lookup(self, consumer_ctx):
        consumer, redis_client = consumer_ctx
        redis_client.hget.return_value = None  # metadata absent

        consumer._infer_category("item-99")
        redis_client.hget.reset_mock()

        result = consumer._infer_category("item-99")

        assert result == "unknown"
        redis_client.hget.assert_not_called()

    def test_redis_error_not_cached_retried_on_next_call(self, consumer_ctx):
        consumer, redis_client = consumer_ctx
        redis_client.hget.side_effect = [Exception("Redis down"), "shoes"]

        result1 = consumer._infer_category("item-1")
        result2 = consumer._infer_category("item-1")

        assert result1 == "unknown"
        assert result2 == "shoes"
        assert redis_client.hget.call_count == 2

    def test_different_items_each_get_their_own_cache_entry(self, consumer_ctx):
        consumer, redis_client = consumer_ctx
        redis_client.hget.side_effect = ["dress", "shoes"]

        r1 = consumer._infer_category("item-1")
        r2 = consumer._infer_category("item-2")

        assert r1 == "dress"
        assert r2 == "shoes"
        assert redis_client.hget.call_count == 2

        redis_client.hget.reset_mock()
        # Both entries are now cached
        assert consumer._infer_category("item-1") == "dress"
        assert consumer._infer_category("item-2") == "shoes"
        redis_client.hget.assert_not_called()
