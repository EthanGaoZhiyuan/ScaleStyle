"""
Event Consumer - Kafka Consumer for Real-time Feature Updates

Week 2: Real-time behavior loop
Consumes click events from Kafka and updates Redis online features

Architecture:
  Gateway → Kafka (scalestyle.clicks) → This Service → Redis → Inference
"""

import json
import logging
import sys
import time
from typing import Dict, Any

import redis
from kafka import KafkaConsumer

import config
from metrics import MetricsCollector, MetricsServer

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    handlers=[logging.StreamHandler(sys.stdout)],
)
logger = logging.getLogger(__name__)


class EventConsumer:
    """
    Real-time event consumer that processes click events and updates Redis features
    """

    # Lua script for atomic dedupe + feature materialization (P0.1)
    # Significantly reduces the partial-failure hole via atomic Redis-side execution
    # Provides at-least-once transport with idempotent Redis materialization
    LUA_UPSERT_FEATURES = """
    -- Args: dedupe_key, dedupe_ttl, user_id, item_id, timestamp, category, session_id,
    --       recent_clicks_max, affinity_ttl, session_ttl, recent_item_ttl
    local dedupe_key = KEYS[1]
    local dedupe_ttl = tonumber(ARGV[1])
    local user_id = ARGV[2]
    local item_id = ARGV[3]
    local timestamp = ARGV[4]
    local category = ARGV[5]
    local session_id = ARGV[6]
    local recent_clicks_max = tonumber(ARGV[7])
    local affinity_ttl = tonumber(ARGV[8])
    local session_ttl = tonumber(ARGV[9])
    local recent_item_ttl = tonumber(ARGV[10])
    
    -- Check if event already processed (idempotency)
    if redis.call('EXISTS', dedupe_key) == 1 then
        return 'DUPLICATE'
    end
    
    -- Set dedupe marker
    redis.call('SETEX', dedupe_key, dedupe_ttl, '1')
    
    -- Update all features atomically
    -- 1. Recent clicks
    local recent_clicks_key = 'user:' .. user_id .. ':recent_clicks'
    redis.call('LPUSH', recent_clicks_key, item_id)
    redis.call('LTRIM', recent_clicks_key, 0, recent_clicks_max - 1)
    
    -- 2. Last activity
    redis.call('SET', 'user:' .. user_id .. ':last_activity', timestamp)
    
    -- 3. Category affinity (use hash for better schema - P2.8)
    if category ~= '' and category ~= 'unknown' then
        local affinity_key = 'user:' .. user_id .. ':category_affinity'
        redis.call('HINCRBYFLOAT', affinity_key, category, 1.0)
        redis.call('EXPIRE', affinity_key, affinity_ttl)
    end
    
    -- 4. Item clicks
    redis.call('INCR', 'item:' .. item_id .. ':clicks')
    local recent_item_key = 'item:' .. item_id .. ':recent_clicks'
    redis.call('INCR', recent_item_key)
    redis.call('EXPIRE', recent_item_key, recent_item_ttl)
    
    -- 5. Global popularity
    redis.call('ZINCRBY', 'global:popular', 1.0, item_id)
    
    -- 6. Session clicks
    if session_id ~= '' then
        local session_key = 'session:' .. session_id .. ':clicks'
        redis.call('LPUSH', session_key, item_id)
        redis.call('EXPIRE', session_key, session_ttl)
    end
    
    return 'OK'
    """

    def __init__(self):
        self.redis_client = None
        self.consumer = None
        self.event_count = 0
        self.error_count = 0
        self.deduped_count = 0  # Track duplicate events
        self.start_time = time.time()

        # Metrics (P1.1)
        self.metrics = MetricsCollector()
        self.metrics_server = MetricsServer(self.metrics, port=config.METRICS_PORT)

        self._connect_redis()
        self._connect_kafka()

        # Load Lua script (P0.1)
        self.lua_upsert_script = self.redis_client.register_script(
            self.LUA_UPSERT_FEATURES
        )
        logger.info("✅ Loaded atomic dedupe+upsert Lua script")

        self.metrics_server.start()

    def _connect_redis(self):
        """Connect to Redis"""
        try:
            self.redis_client = redis.Redis(
                host=config.REDIS_HOST,
                port=config.REDIS_PORT,
                db=config.REDIS_DB,
                decode_responses=True,
            )
            self.redis_client.ping()
            logger.info(
                f"✅ Connected to Redis: {config.REDIS_HOST}:{config.REDIS_PORT}"
            )
        except Exception as e:
            logger.error(f"❌ Failed to connect to Redis: {e}")
            sys.exit(1)

    def _connect_kafka(self):
        """Connect to Kafka and create consumer"""
        try:
            self.consumer = KafkaConsumer(
                config.KAFKA_TOPIC,
                bootstrap_servers=config.KAFKA_BOOTSTRAP_SERVERS.split(","),
                group_id=config.KAFKA_GROUP_ID,
                auto_offset_reset=config.KAFKA_AUTO_OFFSET_RESET,
                enable_auto_commit=False,  # Manual commit for at-least-once semantics
                value_deserializer=lambda m: json.loads(m.decode("utf-8")),
                key_deserializer=lambda k: k.decode("utf-8") if k else None,
            )
            logger.info(f"✅ Connected to Kafka: {config.KAFKA_BOOTSTRAP_SERVERS}")
            logger.info(f"📡 Subscribed to topic: {config.KAFKA_TOPIC}")
            logger.info(f"👥 Consumer group: {config.KAFKA_GROUP_ID}")
        except Exception as e:
            logger.error(f"❌ Failed to connect to Kafka: {e}")
            sys.exit(1)

    def update_features(self, event: Dict[str, Any]) -> bool:
        """
        Update Redis features based on click event using atomic Lua script (P0.1)

        Features updated atomically:
        1. user:{uid}:recent_clicks - List of recent item clicks
        2. user:{uid}:category_affinity - Hash of category -> score (P2.8 improved schema)
        3. user:{uid}:last_activity - Last activity timestamp
        4. item:{item_id}:clicks - Total click count
        5. item:{item_id}:recent_clicks - Recent clicks (24h TTL)
        6. global:popular - Global popularity sorted set
        7. session:{sid}:clicks - Session clicks (1h TTL)

        Returns:
            bool: True if update successful, False otherwise
        """
        t0 = time.time()

        event_id = event.get("event_id")
        user_id = event.get("user_id")
        item_id = event.get("item_id")
        session_id = event.get("session_id", "")
        timestamp = event.get("timestamp", "")
        source = event.get("source", "unknown")

        # Validate required fields
        if not event_id:
            logger.warning(f"⚠️  Invalid event (missing event_id): {event}")
            return False

        if not user_id or not item_id:
            logger.warning(f"⚠️  Invalid event (missing user_id or item_id): {event}")
            return False

        # Fetch category (best effort, before Lua script)
        # P1.4: Explicit category handling with metrics
        category = self._infer_category(item_id)
        if category == "unknown":
            self.metrics.incr_unknown_category()

        try:
            dedupe_key = f"dedupe:event:{event_id}"

            # Execute atomic Lua script (P0.1 fix)
            # Significantly reduces partial-failure hole via server-side atomicity
            result = self.lua_upsert_script(
                keys=[dedupe_key],
                args=[
                    config.DEDUPE_WINDOW_SECONDS,
                    user_id,
                    item_id,
                    timestamp,
                    category,
                    session_id,
                    config.RECENT_CLICKS_MAX,
                    config.AFFINITY_DECAY_DAYS * 86400,
                    config.SESSION_EXPIRE_SECONDS,
                    config.RECENT_ITEM_CLICKS_EXPIRE,
                ],
            )

            if result == b"DUPLICATE" or result == "DUPLICATE":
                logger.debug(
                    f"🔁 Duplicate event (already processed): event_id={event_id}"
                )
                self.deduped_count += 1
                self.metrics.incr_deduped()
                return True  # Not an error, just already processed

            # Success
            latency_ms = (time.time() - t0) * 1000
            logger.debug(
                f"✅ Updated features: user={user_id}, item={item_id}, category={category}, source={source}, latency={latency_ms:.1f}ms"
            )
            self.metrics.incr_processed()
            self.metrics.observe_processing_latency(latency_ms)
            return True

        except Exception as e:
            latency_ms = (time.time() - t0) * 1000
            logger.error(f"❌ Failed to update features for event {event}: {e}")
            self.error_count += 1
            self.metrics.incr_failed()
            self.metrics.incr_lua_failure()  # P1.3: Track Lua failures
            self.metrics.observe_processing_latency(latency_ms)
            return False

    def _infer_category(self, item_id: str) -> str:
        """
        Infer category from item_id
        P1.4: Improved category handling with explicit metrics

        In production, fetch from Redis: item:{item_id}:meta → category
        Tracks lookup success/failure for observability

        Returns:
            - Category string if found
            - "unknown" if not found (caller decides whether to skip affinity update)
        """
        # Try to fetch from Redis metadata (if exists)
        try:
            meta_key = f"item:{item_id}:meta"
            category = self.redis_client.hget(meta_key, "category")
            if category:
                if isinstance(category, bytes):
                    category = category.decode("utf-8")
                if category and category != "unknown":
                    self.metrics.incr_category_hit()
                    return category
        except Exception as e:
            logger.debug(f"Category lookup failed for item {item_id}: {e}")

        # Fallback: return "unknown"
        # P1.4: Track unknown categories for feature quality monitoring
        self.metrics.incr_category_miss()
        return "unknown"

    def run(self):
        """
        Main consumer loop
        """
        logger.info("🚀 Event Consumer started")
        logger.info(f"📊 Consuming from: {config.KAFKA_TOPIC}")
        logger.info(f"🗄️  Updating Redis: {config.REDIS_HOST}:{config.REDIS_PORT}")
        logger.info("-" * 60)

        try:
            for message in self.consumer:
                success = False
                try:
                    event = message.value
                    success = self.update_features(event)

                    if success:
                        # Only commit offset if processing succeeded
                        # This ensures at-least-once delivery semantics
                        self.consumer.commit()
                        self.event_count += 1

                        # Log stats periodically
                        if self.event_count % config.LOG_INTERVAL == 0:
                            elapsed = time.time() - self.start_time
                            rate = self.event_count / elapsed if elapsed > 0 else 0
                            logger.info(
                                f"📈 Stats: processed={self.event_count}, errors={self.error_count}, "
                                f"deduped={self.deduped_count}, rate={rate:.1f} events/sec, uptime={elapsed:.1f}s"
                            )
                    else:
                        # Processing failed, do not commit offset
                        # Message will be reprocessed after consumer restart/rebalance
                        logger.warning(
                            "⚠️  Failed to process event, offset NOT committed (will retry)"
                        )
                        self.error_count += 1

                except Exception as e:
                    logger.error(f"❌ Error processing message: {e}")
                    self.error_count += 1
                    # Do not commit on exception

        except KeyboardInterrupt:
            logger.info("⏹️  Shutting down gracefully...")
        finally:
            self.close()

    def close(self):
        """Cleanup resources"""
        if self.consumer:
            self.consumer.close()
        if self.redis_client:
            self.redis_client.close()
        if self.metrics_server:
            self.metrics_server.stop()
        logger.info(
            f"✅ Shutdown complete. Total processed: {self.event_count}, errors: {self.error_count}"
        )


def main():
    """Entry point"""
    consumer = EventConsumer()
    consumer.run()


if __name__ == "__main__":
    main()
