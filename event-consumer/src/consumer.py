"""
Event Consumer - Kafka Consumer for Real-time Feature Updates

Consumes click events from Kafka and updates Redis online features.

Architecture:
  Gateway → Kafka (scalestyle.clicks) → This Service → Redis → Inference

Retry topology:
  - Primary consumer: main topic only; transient failures → retry-1s
  - Retry consumer: retry-1s → retry-10s → retry-60s → DLQ
  - Tier isolation: 1st/2nd/3rd retries go to separate topics (bounded retries)
    - RETRY_ENFORCE_DELAY: true by default; retry messages wait until
        routed_at_ts + tier_delay (partition pause, no seek)
    - Unsafe local override: RETRY_ENFORCE_DELAY=false requires
        ALLOW_UNSAFE_IMMEDIATE_RETRY=true
  - Poison messages: permanent failures or max-retries exceeded → DLQ
"""

import json
import logging
import math
import sys
import time
from collections import OrderedDict
from datetime import datetime, timezone
from typing import Dict, Any, Optional, Tuple
from enum import Enum

import redis
from kafka import KafkaConsumer, KafkaProducer, TopicPartition, OffsetAndMetadata

import config
import metrics
import observability
from metrics import MetricsServer
from redis_metadata import canonical_article_id, item_meta_key

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    handlers=[logging.StreamHandler(sys.stdout)],
)
logger = logging.getLogger(__name__)


def decay_score(previous_score: float, previous_timestamp: Optional[float], current_timestamp: float, decay_lambda: float) -> float:
    """Apply exponential decay to a score last materialized at *previous_timestamp*."""
    if previous_score <= 0.0:
        return 0.0
    if previous_timestamp is None or previous_timestamp > current_timestamp:
        return previous_score
    elapsed = max(0.0, current_timestamp - previous_timestamp)
    return previous_score * math.exp(-decay_lambda * elapsed)


def apply_decay_update(
    previous_score: float,
    previous_timestamp: Optional[float],
    current_timestamp: float,
    decay_lambda: float,
    increment: float = 1.0,
) -> float:
    """Decay the previous score to *current_timestamp* and add the new event weight."""
    return decay_score(previous_score, previous_timestamp, current_timestamp, decay_lambda) + increment


def popularity_rank_score(actual_score: float, last_update_timestamp: float, decay_lambda: float) -> float:
    """Return the Redis ZSET ranking surrogate for decayed popularity."""
    if actual_score <= 0.0:
        raise ValueError("actual_score must be positive for popularity ranking")
    return math.log(actual_score) + (decay_lambda * last_update_timestamp)


class ProcessingResult(Enum):
    """Processing result for each message"""
    APPLIED = "applied"
    DUPLICATE = "duplicate"
    TRANSIENT_FAILURE = "transient_failure"  # Temporary failure (will retry)
    PERMANENT_FAILURE = "permanent_failure"  # Permanent failure (send to DLQ)


class RetryPublishedCommitFailedError(Exception):
    """Raised when a retry message was durably published but the source offset
    commit subsequently failed.

    This is a terminal state for the consumer process.  The retry message
    already exists on the retry topic, so if the consumer continues running
    the uncommitted source offset will be re-delivered on the next restart
    or rebalance and will produce a second (duplicate) retry entry.

    The consumer loop re-raises this exception — never swallows it — so that
    the process exits, Kubernetes restarts it, and the consumer group
    rebalances cleanly.  The duplicate retry entry produced on restart is
    safely idempotent: the retry consumer's event_id Lua duplicate suppression in Redis
    returns DUPLICATE and commits without applying any feature update.
    """


class DlqPublishedCommitFailedError(Exception):
    """Raised when a DLQ message was durably sent (and optionally marked) but
    the source offset commit subsequently failed.

    Same fail-fast semantics as RetryPublishedCommitFailedError: the consumer
    must not continue with an uncommitted source offset after the downstream
    write (DLQ) has succeeded.  The loop re-raises this so the process exits
    and Kubernetes restarts it.  On re-delivery the message may be sent to
    DLQ again; DLQ is explicitly at-least-once.
    Replay/triage tooling MUST suppress duplicates by dlq_id (Kafka key/payload).
    """


class _CategoryCache:
    """In-process LRU cache with per-entry TTL for item→category lookups.

    Eliminates one Redis round trip per event: without this cache, every call to
    ``_infer_category`` issued a synchronous ``HGET item:{id}:meta category``
    before the Lua upsert.  Because item categories are stable after ingestion,
    a 1-hour TTL is safe and keeps the cache warm across normal traffic patterns.

    ``"unknown"`` entries (items absent from Redis) are cached with the same TTL
    to prevent repeated lookups for cold or unindexed items.

    Thread-safety: the consumer loop is single-threaded, so no locking is needed.

    :param max_size: Maximum number of entries.  LRU entry is evicted when full.
    :param ttl_seconds: Seconds before a cached entry is considered stale.
    """

    def __init__(self, max_size: int, ttl_seconds: float) -> None:
        self._max_size = max_size
        self._ttl = ttl_seconds
        # OrderedDict preserves insertion / access order for O(1) LRU management.
        self._store: "OrderedDict[str, Tuple[str, float]]" = OrderedDict()

    def get(self, item_id: str) -> Tuple[Optional[str], str]:
        """Look up *item_id* in the cache.

        Returns ``(category, status)`` where *status* is one of:
        - ``"hit"``     — valid entry found; Redis round trip saved.
        - ``"expired"`` — entry existed but TTL elapsed; caller must re-fetch.
        - ``"miss"``    — entry not present; caller must fetch from Redis.
        """
        entry = self._store.get(item_id)
        if entry is None:
            return None, "miss"
        category, expires_at = entry
        if time.monotonic() > expires_at:
            del self._store[item_id]
            return None, "expired"
        # Move to end (most-recently-used position) for LRU accounting.
        self._store.move_to_end(item_id)
        return category, "hit"

    def put(self, item_id: str, category: str) -> None:
        """Insert or refresh *item_id* → *category*.  Evicts the LRU entry when full."""
        if item_id in self._store:
            self._store.move_to_end(item_id)
        elif len(self._store) >= self._max_size:
            self._store.popitem(last=False)  # evict LRU (oldest) entry
        self._store[item_id] = (category, time.monotonic() + self._ttl)

    def __len__(self) -> int:
        return len(self._store)


class EventConsumer:
    """Real-time event consumer that processes click events and updates Redis features.
    
    Redis Cluster Compatibility:
        The Lua script below is INCOMPATIBLE with Redis Cluster mode (including
        ElastiCache Cluster Mode Enabled). It performs atomic writes across multiple
        key prefixes (user:*, item:*, global:*, popularity:*, session:*) that hash
        to different cluster slots. Redis Cluster requires all keys in a Lua script
        to be declared in KEYS[] and hash to the same slot.
        
        CROSSSLOT error on every event would occur if cluster mode is enabled.
        
        Production requirement: Use Redis standalone or replication group mode.
        ElastiCache: Set cluster_mode_enabled = false (replication group with replicas).
        
        A startup check in _connect_redis() validates this and fails fast if cluster
        mode is detected.
    """

    LUA_UPSERT_FEATURES = """
    -- Args:
    --   dedupe_ttl, user_id, item_id, event_ts, last_activity_ts, category, session_id,
    --   recent_clicks_max, affinity_lambda, item_click_lambda, recent_item_lambda,
    --   popularity_lambda, session_ttl, feature_state_ttl,
    --   popularity_1h_bucket_seconds, popularity_1h_bucket_ttl,
    --   popularity_24h_bucket_seconds, popularity_24h_bucket_ttl,
    --   popularity_7d_bucket_seconds, popularity_7d_bucket_ttl,
    --   popularity_bucket_prefix
    local dedupe_key = KEYS[1]
    local dedupe_ttl = tonumber(ARGV[1])
    local user_id = ARGV[2]
    local item_id = ARGV[3]
    local event_ts = tonumber(ARGV[4])
    local last_activity_ts = ARGV[5]
    local category = ARGV[6]
    local session_id = ARGV[7]
    local recent_clicks_max = tonumber(ARGV[8])
    local affinity_lambda = tonumber(ARGV[9])
    local item_click_lambda = tonumber(ARGV[10])
    local recent_item_lambda = tonumber(ARGV[11])
    local popularity_lambda = tonumber(ARGV[12])
    local session_ttl = tonumber(ARGV[13])
    local feature_state_ttl = tonumber(ARGV[14])
    local popularity_1h_bucket_seconds = tonumber(ARGV[15])
    local popularity_1h_bucket_ttl = tonumber(ARGV[16])
    local popularity_24h_bucket_seconds = tonumber(ARGV[17])
    local popularity_24h_bucket_ttl = tonumber(ARGV[18])
    local popularity_7d_bucket_seconds = tonumber(ARGV[19])
    local popularity_7d_bucket_ttl = tonumber(ARGV[20])
    local popularity_bucket_prefix = ARGV[21]

    local function format_number(value)
        return string.format('%.17g', value)
    end

    local function decayed_score(score_key, timestamp_key, now_ts, decay_lambda)
        local raw_score = redis.call('GET', score_key)
        if not raw_score then
            return 0.0
        end
        local score = tonumber(raw_score) or 0.0
        if score <= 0.0 then
            return 0.0
        end
        local raw_last_ts = redis.call('GET', timestamp_key)
        local last_ts = tonumber(raw_last_ts)
        if not last_ts or last_ts > now_ts then
            return score
        end
        local elapsed = now_ts - last_ts
        if elapsed <= 0.0 then
            return score
        end
        return score * math.exp(-decay_lambda * elapsed)
    end

    local function decayed_hash_score(score_hash_key, timestamp_hash_key, field, now_ts, decay_lambda)
        local raw_score = redis.call('HGET', score_hash_key, field)
        if not raw_score then
            return 0.0
        end
        local score = tonumber(raw_score) or 0.0
        if score <= 0.0 then
            return 0.0
        end
        local raw_last_ts = redis.call('HGET', timestamp_hash_key, field)
        local last_ts = tonumber(raw_last_ts)
        if not last_ts or last_ts > now_ts then
            return score
        end
        local elapsed = now_ts - last_ts
        if elapsed <= 0.0 then
            return score
        end
        return score * math.exp(-decay_lambda * elapsed)
    end

    local function popularity_bucket_key(window_name, now_ts, bucket_seconds)
        local bucket_start = math.floor(now_ts / bucket_seconds) * bucket_seconds
        return popularity_bucket_prefix .. ':' .. window_name .. ':' .. tostring(bucket_start)
    end
    
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
    redis.call('SET', 'user:' .. user_id .. ':last_activity', last_activity_ts)
    
    -- 3. Category affinity with true exponential time decay per category
    if category ~= '' and category ~= 'unknown' then
        local affinity_key = 'user:' .. user_id .. ':category_affinity'
        local affinity_ts_key = affinity_key .. ':last_ts'
        local affinity_score = decayed_hash_score(affinity_key, affinity_ts_key, category, event_ts, affinity_lambda) + 1.0
        redis.call('HSET', affinity_key, category, format_number(affinity_score))
        redis.call('HSET', affinity_ts_key, category, format_number(event_ts))
        redis.call('EXPIRE', affinity_key, feature_state_ttl)
        redis.call('EXPIRE', affinity_ts_key, feature_state_ttl)
    end
    
    -- 4. Item click signals with real decay
    local item_click_key = 'item:' .. item_id .. ':clicks'
    local item_click_ts_key = item_click_key .. ':last_ts'
    local item_click_score = decayed_score(item_click_key, item_click_ts_key, event_ts, item_click_lambda) + 1.0
    redis.call('SET', item_click_key, format_number(item_click_score))
    redis.call('SET', item_click_ts_key, format_number(event_ts))
    redis.call('EXPIRE', item_click_key, feature_state_ttl)
    redis.call('EXPIRE', item_click_ts_key, feature_state_ttl)

    local recent_item_key = 'item:' .. item_id .. ':recent_clicks'
    local recent_item_ts_key = recent_item_key .. ':last_ts'
    local recent_item_score = decayed_score(recent_item_key, recent_item_ts_key, event_ts, recent_item_lambda) + 1.0
    redis.call('SET', recent_item_key, format_number(recent_item_score))
    redis.call('SET', recent_item_ts_key, format_number(event_ts))
    redis.call('EXPIRE', recent_item_key, feature_state_ttl)
    redis.call('EXPIRE', recent_item_ts_key, feature_state_ttl)
    
    -- 5. Global popularity with true exponential decay.
    -- We store the actual score in a hash and a ranking surrogate in the ZSET:
    --   zset_score = log(actual_score_at_last_update) + lambda * last_update_ts
    -- This preserves correct ordering at read time without a full ZSET rescore sweep.
    local popularity_score_key = 'global:popular:score'
    local popularity_ts_key = 'global:popular:last_ts'
    local popularity_score = decayed_hash_score(popularity_score_key, popularity_ts_key, item_id, event_ts, popularity_lambda) + 1.0
    redis.call('HSET', popularity_score_key, item_id, format_number(popularity_score))
    redis.call('HSET', popularity_ts_key, item_id, format_number(event_ts))
    redis.call('ZADD', 'global:popular', math.log(popularity_score) + (popularity_lambda * event_ts), item_id)
    
    -- Prevent unbounded growth: cap ZSET at top 50K items and set TTL on all structures
    redis.call('ZREMRANGEBYRANK', 'global:popular', 0, -(50001))
    local popularity_ttl = 30 * 86400  -- 30 days
    redis.call('EXPIRE', 'global:popular', popularity_ttl)
    redis.call('EXPIRE', popularity_score_key, popularity_ttl)
    redis.call('EXPIRE', popularity_ts_key, popularity_ttl)

    -- 5b. Windowed popularity buckets used as the primary online ranking signal.
    local popularity_1h_key = popularity_bucket_key('1h', event_ts, popularity_1h_bucket_seconds)
    redis.call('ZINCRBY', popularity_1h_key, 1.0, item_id)
    redis.call('EXPIRE', popularity_1h_key, popularity_1h_bucket_ttl)

    local popularity_24h_key = popularity_bucket_key('24h', event_ts, popularity_24h_bucket_seconds)
    redis.call('ZINCRBY', popularity_24h_key, 1.0, item_id)
    redis.call('EXPIRE', popularity_24h_key, popularity_24h_bucket_ttl)

    local popularity_7d_key = popularity_bucket_key('7d', event_ts, popularity_7d_bucket_seconds)
    redis.call('ZINCRBY', popularity_7d_key, 1.0, item_id)
    redis.call('EXPIRE', popularity_7d_key, popularity_7d_bucket_ttl)
    
    -- After local session_id = ARGV[7], add:
    if session_id ~= '' then
        local session_key = 'session:' .. session_id .. ':clicks'
        redis.call('LPUSH', session_key, item_id)
        redis.call('LTRIM', session_key, 0, 99)
        redis.call('EXPIRE', session_key, session_ttl)
    end
    
    return 'OK'
    """

    def __init__(self):
        self.redis_client = None
        self.consumer = None
        self.kafka_producer = None  # Producer for retry and dead letter flows
        self.event_count = 0
        self.error_count = 0
        self.deduped_count = 0
        self.dlq_count = 0  # Track messages sent to DLQ
        self.retry_routed_count = 0
        self.start_time = time.time()
        self.last_poll_ts: Optional[float] = None
        self.loop_alive = True

        # Track paused partitions for retry backoff (avoid worker sleep)
        self.paused_partitions = {}  # {TopicPartition: resume_at_timestamp}
        self.retry_topic_to_delay_seconds = {
            topic: delay_seconds for _, topic, delay_seconds in config.KAFKA_RETRY_TIERS
        }
        self.retry_topic_to_tier_name = {
            topic: tier_name for tier_name, topic, _ in config.KAFKA_RETRY_TIERS
        }
        self.tracer = observability.setup_tracing(service_name="event-consumer")

        self.metrics_server = MetricsServer(port=config.METRICS_PORT)

        self._category_cache = _CategoryCache(
            max_size=config.CATEGORY_CACHE_MAX_SIZE,
            ttl_seconds=config.CATEGORY_CACHE_TTL_SECONDS,
        )
        logger.info(
            "[SUCCESS] Category LRU cache initialised: max_size=%d, ttl=%ds",
            config.CATEGORY_CACHE_MAX_SIZE,
            config.CATEGORY_CACHE_TTL_SECONDS,
        )

        self._connect_redis()
        self._connect_kafka()
        self._connect_kafka_producer()

        self.lua_upsert_script = self.redis_client.register_script(
            self.LUA_UPSERT_FEATURES
        )
        logger.info("[SUCCESS] Loaded atomic duplicate-suppression + decayed-feature Lua script")

        self.metrics_server.set_liveness_check(self._liveness_check)
        self.metrics_server.set_health_check(self._health_check)
        self.metrics_server.start()

    def _parse_event_timestamp_seconds(self, raw_timestamp: Any) -> Tuple[float, str]:
        """Parse event time into epoch seconds and a canonical ISO-8601 string.

        Real decay uses event timestamps, not wall-clock update time, so malformed
        timestamps are treated as permanent input errors.
        """
        if isinstance(raw_timestamp, (int, float)):
            ts_seconds = float(raw_timestamp)
            return ts_seconds, datetime.fromtimestamp(ts_seconds, tz=timezone.utc).isoformat().replace("+00:00", "Z")

        if not isinstance(raw_timestamp, str) or not raw_timestamp.strip():
            raise ValueError("timestamp is required for decayed feature updates")

        normalized = raw_timestamp.strip()
        if normalized.endswith("Z"):
            normalized = normalized[:-1] + "+00:00"
        parsed = datetime.fromisoformat(normalized)
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        else:
            parsed = parsed.astimezone(timezone.utc)
        return parsed.timestamp(), parsed.isoformat().replace("+00:00", "Z")

    def _connect_redis(self):
        """Connect to Redis with production-grade connection pooling and resource limits.
        
        Hardness contracts:
        - Max connections: Hard cap to prevent unbounded resource usage
        - Socket timeouts: Defense-in-depth at TCP and command level
        - Health checks: Periodic validation to detect stale connections
        - ConnectionPool: Reuses connections, bounded queue for fairness
        """
        try:
            # Create connection pool with hard resource limits
            # Similar to gateway WebClient approach: bounded resources prevent OOM
            pool = redis.ConnectionPool(
                host=config.REDIS_HOST,
                port=config.REDIS_PORT,
                db=config.REDIS_DB,
                max_connections=config.REDIS_MAX_CONNECTIONS,  # Hard cap: max 50 concurrent
                socket_connect_timeout=config.REDIS_SOCKET_CONNECT_TIMEOUT_SEC,
                socket_timeout=config.REDIS_SOCKET_TIMEOUT_SEC,
                health_check_interval=config.REDIS_HEALTH_CHECK_INTERVAL_SEC,
                retry_on_timeout=config.REDIS_RETRY_ON_TIMEOUT,
                decode_responses=True,
                ssl=config.REDIS_TLS,
                ssl_cert_reqs="required" if config.REDIS_TLS else None,
            )
            
            self.redis_client = redis.Redis(connection_pool=pool)
            
            # Verify connectivity with timeout
            ping_start = time.time()
            self.redis_client.ping()
            ping_latency = time.time() - ping_start
            
            logger.info(
                f"[SUCCESS] Connected to Redis: {config.REDIS_HOST}:{config.REDIS_PORT} "
                f"(pool_size={config.REDIS_MAX_CONNECTIONS}, "
                f"connect_timeout={config.REDIS_SOCKET_CONNECT_TIMEOUT_SEC}s, "
                f"cmd_timeout={config.REDIS_SOCKET_TIMEOUT_SEC}s, "
                f"ping_latency={ping_latency*1000:.1f}ms)"
            )
            metrics.record_redis_success()
            metrics.redis_operations_total.labels(operation="ping", status="success").inc()
            
            # ── Fail-fast: Verify Redis is NOT in cluster mode ────────────────────
            # The Lua upsert script writes to multiple key prefixes (user:*, item:*,
            # global:*, popularity:*) that hash to different cluster slots. Redis
            # Cluster requires all keys to be declared in KEYS[] and hash to the same
            # slot. This constraint is architecturally incompatible with our atomic
            # cross-domain feature update pattern.
            #
            # CROSSSLOT errors would occur on every event if cluster mode is enabled.
            # This check prevents silent production failures.
            try:
                info = self.redis_client.info("cluster")
                cluster_enabled = info.get("cluster_enabled", 0)
                if cluster_enabled == 1:
                    logger.error(
                        "[FATAL] Redis Cluster mode detected but NOT supported. "
                        "The Lua upsert script performs atomic writes across multiple "
                        "key prefixes (user:*, item:*, global:*, popularity:*) that hash "
                        "to different cluster slots, which causes CROSSSLOT errors. "
                        "\n\nProduction requirement: Use Redis standalone or replication group mode. "
                        "\nElastiCache: Set cluster_mode_enabled = false in terraform/elasticache.tf "
                        "\n(replication group with automatic_failover_enabled = true is supported)."
                    )
                    metrics.redis_available.set(0)
                    metrics.consumer_health.set(0)
                    sys.exit(1)
                logger.info("[SUCCESS] Redis cluster mode check passed (cluster_enabled=0)")
            except redis.ResponseError as e:
                # INFO CLUSTER command may not be available in all Redis versions/configs
                # If the command fails, assume standalone mode and log a warning
                logger.warning(
                    "Could not verify Redis cluster mode (INFO CLUSTER failed: %s). "
                    "Assuming standalone mode. If you encounter CROSSSLOT errors, "
                    "verify Redis is not in cluster mode.", e
                )
            
        except redis.ConnectionError as e:
            logger.error(
                f"[ERROR] Redis connection failed: {e.__class__.__name__}: {e} "
                f"(host={config.REDIS_HOST}, port={config.REDIS_PORT})"
            )
            metrics.redis_available.set(0)
            metrics.consumer_health.set(0)
            metrics.redis_operations_total.labels(operation="ping", status="error").inc()
            sys.exit(1)
        except redis.TimeoutError as e:
            logger.error(
                f"[ERROR] Redis connection timeout: {e} "
                f"(connect_timeout={config.REDIS_SOCKET_CONNECT_TIMEOUT_SEC}s)"
            )
            metrics.redis_available.set(0)
            metrics.consumer_health.set(0)
            metrics.redis_operations_total.labels(operation="ping", status="timeout").inc()
            sys.exit(1)
        except Exception as e:
            logger.error(
                f"[ERROR] Unexpected Redis connection error: {e.__class__.__name__}: {e}"
            )
            metrics.redis_available.set(0)
            metrics.consumer_health.set(0)
            metrics.redis_operations_total.labels(operation="ping", status="error").inc()
            sys.exit(1)

    def _connect_kafka(self):
        """Connect to Kafka and create consumer"""
        try:
            # Determine which topics to subscribe to based on CONSUMER_MODE
            # Mode validation already done in config.py
            topics_to_subscribe = []
            
            if config.CONSUMER_MODE == "primary":
                topics_to_subscribe = [config.KAFKA_TOPIC]
                logger.info(f"🔵 Running in PRIMARY mode - consuming only main traffic")
            elif config.CONSUMER_MODE == "retry":
                topics_to_subscribe = list(config.KAFKA_RETRY_TOPICS)
                logger.info(
                    "🟠 Running in RETRY mode - consuming tiered retry traffic: %s",
                    topics_to_subscribe,
                )
            else:
                # Should never reach here due to config.py validation
                logger.error(
                    f"❌ Invalid CONSUMER_MODE: {config.CONSUMER_MODE}. "
                    f"Must be 'primary' or 'retry'."
                )
                sys.exit(1)
            
            self.consumer = KafkaConsumer(
                *topics_to_subscribe,
                bootstrap_servers=config.KAFKA_BOOTSTRAP_SERVERS.split(","),
                group_id=config.KAFKA_GROUP_ID,
                auto_offset_reset=config.KAFKA_AUTO_OFFSET_RESET,
                enable_auto_commit=False,
                value_deserializer=lambda m: json.loads(m.decode("utf-8")),
                key_deserializer=lambda k: k.decode("utf-8") if k else None,
                # Explicitly pin max_poll_interval_ms so this contract is visible in code
                # and is not silently changed by a kafka-python version bump.
                # Formula: KAFKA_POLL_MAX_RECORDS × REDIS_SOCKET_TIMEOUT_SEC << 300s
                # With the 0.5s override: 100 × 0.5 = 50s worst-case, well under budget.
                max_poll_interval_ms=300_000,
                **self._kafka_auth_kwargs(),
            )
            logger.info(f"[SUCCESS] Connected to Kafka: {config.KAFKA_BOOTSTRAP_SERVERS}")
            logger.info(f"[INFO] Subscribed to topics: {topics_to_subscribe}")
            logger.info(f"[INFO] Consumer group: {config.KAFKA_GROUP_ID}")
        except Exception as e:
            logger.error(f"❌ Failed to connect to Kafka: {e}")
            metrics.consumer_health.set(0)
            sys.exit(1)

    def _connect_kafka_producer(self):
        """Connect to Kafka producer for retry and Dead Letter Queue.

        Uses the same producer settings as Gateway. send().get() treats broker
        acknowledgment as the source-of-truth for downstream publish success.
        acks=all + idempotence reduce duplicate risk during broker retries.
        """
        try:
            self.kafka_producer = KafkaProducer(
                bootstrap_servers=config.KAFKA_BOOTSTRAP_SERVERS.split(","),
                value_serializer=lambda v: json.dumps(v).encode("utf-8"),
                key_serializer=lambda k: k.encode("utf-8") if k else None,
                acks="all",
                retries=3,                                  # bounded, matching gateway semantics
                enable_idempotence=True,
                max_in_flight_requests_per_connection=5,
                request_timeout_ms=3_500,
                # Keep producer delivery deadline strictly inside future.get(timeout=5)
                # so the app never gives up while producer retries could still succeed later.
                delivery_timeout_ms=4_500,
                compression_type="gzip",
                **self._kafka_auth_kwargs(),
            )
            logger.info(
                "✅ Producer connected for retry_tiers=%s, dlq=%s with idempotence enabled",
                config.KAFKA_RETRY_TOPICS,
                config.KAFKA_DLQ_TOPIC,
            )
        except Exception as e:
            logger.error("❌ Failed to connect producer: %s", e, exc_info=True)
            metrics.consumer_health.set(0)
            sys.exit(1)

    def _kafka_auth_kwargs(self):
        """Build Kafka auth config from environment-driven settings."""
        auth_kwargs = {}
        normalized_security_protocol = (config.KAFKA_SECURITY_PROTOCOL or "").strip().upper()
        sasl_enabled = normalized_security_protocol.startswith("SASL_")
        ssl_enabled = "SSL" in normalized_security_protocol

        if normalized_security_protocol:
            auth_kwargs["security_protocol"] = normalized_security_protocol
        if sasl_enabled and config.KAFKA_SASL_MECHANISM:
            auth_kwargs["sasl_mechanism"] = config.KAFKA_SASL_MECHANISM
        if sasl_enabled and config.KAFKA_USERNAME and config.KAFKA_PASSWORD:
            auth_kwargs["sasl_plain_username"] = config.KAFKA_USERNAME
            auth_kwargs["sasl_plain_password"] = config.KAFKA_PASSWORD
        if ssl_enabled and config.KAFKA_SSL_CAFILE:
            auth_kwargs["ssl_cafile"] = config.KAFKA_SSL_CAFILE

        return auth_kwargs

    def _classify_error(self, exc: Exception) -> ProcessingResult:
        """
        Classify exception as transient (retryable) or permanent (send to DLQ).
        
        Permanent failures:
        - Invalid schema (ValueError, KeyError, TypeError, JSONDecodeError)
        - Missing required fields
        - Invalid data format
        
        Transient failures:
        - Redis connection errors
        - Network timeouts
        - Temporary resource exhaustion
        """
        if isinstance(exc, (ValueError, KeyError, TypeError, json.JSONDecodeError)):
            return ProcessingResult.PERMANENT_FAILURE
        
        # Redis connection errors are typically transient
        if isinstance(exc, (redis.ConnectionError, redis.TimeoutError)):
            return ProcessingResult.TRANSIENT_FAILURE
        
        # Default to transient for unknown errors (safer)
        return ProcessingResult.TRANSIENT_FAILURE

    def _extract_retry_count(self, message, event: Optional[Dict[str, Any]] = None) -> int:
        """Read retry count from Kafka headers, then fallback to payload retry metadata."""
        try:
            if not message.headers:
                raise ValueError("retry header missing")
            for key, value in message.headers:
                if key == config.KAFKA_RETRY_HEADER and value is not None:
                    return int(value.decode("utf-8"))
        except Exception as e:
            logger.debug(
                "Retry header unavailable topic=%s partition=%s offset=%s err=%s",
                message.topic,
                message.partition,
                message.offset,
                e,
            )

        payload = event if isinstance(event, dict) else {}
        retry_meta = payload.get("_retry_meta") if isinstance(payload.get("_retry_meta"), dict) else {}
        try:
            retry_from_payload = retry_meta.get("retry_count")
            return int(retry_from_payload) if retry_from_payload is not None else 0
        except Exception:
            return 0

    def _extract_header_value(self, message, header_key: str) -> Optional[str]:
        """Decode a Kafka header value as UTF-8 string."""
        if not message.headers:
            return None
        for key, value in message.headers:
            if key == header_key and value is not None:
                try:
                    return value.decode("utf-8")
                except Exception:
                    return None
        return None

    def _extract_trace_context(self, message) -> Dict[str, Optional[str]]:
        """Read W3C trace context headers from Kafka message."""
        traceparent = self._extract_header_value(message, config.KAFKA_TRACEPARENT_HEADER)
        tracestate = self._extract_header_value(message, config.KAFKA_TRACESTATE_HEADER)

        trace_id = None
        if traceparent:
            parts = traceparent.split("-")
            if len(parts) >= 4:
                trace_id = parts[1]

        return {
            "traceparent": traceparent,
            "tracestate": tracestate,
            "trace_id": trace_id,
        }

    def _build_trace_carrier(self, message) -> Dict[str, str]:
        """Convert Kafka trace headers into a propagator carrier."""
        trace_ctx = self._extract_trace_context(message)
        carrier: Dict[str, str] = {}
        if trace_ctx["traceparent"]:
            carrier[config.KAFKA_TRACEPARENT_HEADER] = trace_ctx["traceparent"]
        if trace_ctx["tracestate"]:
            carrier[config.KAFKA_TRACESTATE_HEADER] = trace_ctx["tracestate"]
        return carrier

    def _forward_trace_headers(self, message) -> list[tuple[str, bytes]]:
        """Build Kafka headers to preserve trace context on retry/DLQ hops."""
        headers = []
        trace_ctx = self._extract_trace_context(message)
        if trace_ctx["traceparent"]:
            headers.append((config.KAFKA_TRACEPARENT_HEADER, trace_ctx["traceparent"].encode("utf-8")))
        if trace_ctx["tracestate"]:
            headers.append((config.KAFKA_TRACESTATE_HEADER, trace_ctx["tracestate"].encode("utf-8")))
        return headers

    def _log_terminal_commit_uncertainty(
        self,
        *,
        reason: str,
        downstream_action: str,
        message,
        error: Exception,
        event_id: Optional[str] = None,
        trace_id: Optional[str] = None,
        retry_count: Optional[int] = None,
    ) -> None:
        """Emit a single machine-readable critical log before fail-fast exit."""
        logger.critical(
            "CRITICAL: downstream write acknowledged but source commit failed; "
            "terminating process for clean restart "
            "reason=%s downstream_action=%s topic=%s partition=%s offset=%s "
            "event_id=%s trace_id=%s retry_count=%s max_retries=%s err=%s",
            reason,
            downstream_action,
            message.topic,
            message.partition,
            message.offset,
            event_id or "unknown",
            trace_id or "unknown",
            retry_count if retry_count is not None else "na",
            config.MAX_RETRIES if retry_count is not None else "na",
            error,
        )

    def _retry_tier_for_count(self, retry_count: int) -> tuple[str, str, float]:
        if retry_count <= 0 or retry_count > len(config.KAFKA_RETRY_TIERS):
            raise ValueError(f"retry_count {retry_count} out of configured tier range")
        return config.KAFKA_RETRY_TIERS[retry_count - 1]

    def _respect_retry_tier_delay(self, message, event: Dict[str, Any], retry_count: int) -> bool:
        """
        Honor the fixed delay associated with a retry topic tier.

        Retry topology: primary routes failures -> retry-1s -> retry-10s -> retry-60s -> DLQ.
        Standard behavior defers retry messages until routed_at_ts + tier_delay.
        RETRY_ENFORCE_DELAY=false is an explicit unsafe override for local/dev only.

        Returns:
            True if message should be skipped (deferred, not yet ready)
            False if message is ready to process
        """
        if not config.RETRY_ENFORCE_DELAY:
            return False

        if message.topic not in self.retry_topic_to_delay_seconds:
            return False

        delay_seconds = self.retry_topic_to_delay_seconds[message.topic]
        retry_meta = event.get("_retry_meta") if isinstance(event.get("_retry_meta"), dict) else {}
        routed_at_ts = retry_meta.get("routed_at_ts")
        if not isinstance(routed_at_ts, (int, float)):
            return False

        now = time.time()
        ready_at_ts = float(routed_at_ts) + delay_seconds
        remaining = ready_at_ts - now
        if remaining <= 0:
            metrics.retry_delay_seconds.observe(abs(remaining))
            return False

        tp = TopicPartition(message.topic, message.partition)
        pause_duration = min(remaining, 60.0)  # Cap to avoid unbounded pause
        resume_at = now + pause_duration

        try:
            if tp not in self.paused_partitions:
                self.consumer.pause(tp)
                self.paused_partitions[tp] = resume_at
                metrics.retry_partitions_paused.set(len(self.paused_partitions))
                logger.info(
                    "⏸️  Paused retry partition topic=%s tier=%s partition=%s offset=%s retry=%s resume_in=%.1fs (total_paused=%d)",
                    message.topic,
                    self.retry_topic_to_tier_name.get(message.topic, "unknown"),
                    message.partition,
                    message.offset,
                    retry_count,
                    pause_duration,
                    len(self.paused_partitions),
                )
            else:
                if resume_at > self.paused_partitions[tp]:
                    self.paused_partitions[tp] = resume_at
        except Exception as e:
            logger.error(
                "Failed to defer retry message topic=%s tier=%s partition=%s offset=%s err=%s",
                message.topic,
                self.retry_topic_to_tier_name.get(message.topic, "unknown"),
                message.partition,
                message.offset,
                e,
            )
            return False

        metrics.retry_delay_seconds.observe(remaining)
        return True

    def _build_dlq_id(self, message) -> str:
        """Build stable id used as DLQ idempotency key.

        This key is carried in Kafka (record key + payload field) and is the
        canonical duplicate-suppression key for downstream replay tooling.
        """
        event = message.value or {}
        event_id = event.get("event_id") if isinstance(event, dict) else None
        if event_id:
            return str(event_id)
        return f"{message.topic}:{message.partition}:{message.offset}"

    def _is_dlq_dispatched(self, dlq_id: str) -> bool:
        """Check whether advisory DLQ marker already exists.

        Returns True if marker exists, False otherwise (including on errors - fail-open).
        Marker semantics are best-effort noise reduction only; this does NOT
        upgrade DLQ semantics to exactly-once.
        """
        try:
            exists = bool(self.redis_client.exists(f"dlq:sent:{dlq_id}"))
            metrics.record_redis_success()
            metrics.redis_operations_total.labels(operation="dlq_check", status="success").inc()
            return exists
        except redis.TimeoutError as e:
            metrics.record_redis_error()
            logger.warning(
                "Redis timeout during DLQ check: dlq_id=%s timeout=%ss error=%s",
                dlq_id,
                config.REDIS_SOCKET_TIMEOUT_SEC,
                e,
            )
            metrics.redis_operations_total.labels(operation="dlq_check", status="timeout").inc()
            return False  # Fail-open: assume not sent to allow retry
        except redis.ConnectionError as e:
            metrics.record_redis_error()
            logger.warning(
                "Redis connection error during DLQ check: dlq_id=%s error=%s",
                dlq_id,
                e,
            )
            metrics.redis_operations_total.labels(operation="dlq_check", status="connection_error").inc()
            return False
        except Exception as e:
            metrics.record_redis_error()
            logger.error(
                "DLQ marker key read failed dlq_id=%s error_type=%s err=%s",
                dlq_id,
                e.__class__.__name__,
                e,
            )
            metrics.redis_operations_total.labels(operation="dlq_check", status="error").inc()
            return False

    def _mark_dlq_dispatched(self, dlq_id: str) -> bool:
        """Persist advisory DLQ dispatch marker in Redis.

        Returns True only for first dispatch attempt (NX succeeded).
        Returns False if already exists or on Redis errors.

        The marker is a replay-noise hint only. Kafka broker ack remains the
        source of truth for DLQ "sent".
        """
        dedupe_key = f"dlq:sent:{dlq_id}"
        try:
            marked = self.redis_client.set(
                dedupe_key,
                "1",
                nx=True,
                ex=config.DLQ_DEDUPE_TTL_SECONDS,
            )
            metrics.record_redis_success()
            if marked:
                metrics.redis_operations_total.labels(operation="dlq_mark", status="success").inc()
            else:
                # NX failed: key already exists (race condition or duplicate)
                metrics.redis_operations_total.labels(operation="dlq_mark", status="already_exists").inc()
                logger.debug("DLQ mark skipped (already exists): dlq_id=%s", dlq_id)
            return bool(marked)
        except redis.TimeoutError as e:
            metrics.record_redis_error()
            logger.error(
                "Redis timeout marking DLQ sent: dlq_id=%s timeout=%ss error=%s",
                dlq_id,
                config.REDIS_SOCKET_TIMEOUT_SEC,
                e,
            )
            metrics.redis_operations_total.labels(operation="dlq_mark", status="timeout").inc()
            return False
        except redis.ConnectionError as e:
            metrics.record_redis_error()
            logger.error(
                "Redis connection error marking DLQ sent: dlq_id=%s error=%s",
                dlq_id,
                e,
            )
            metrics.redis_operations_total.labels(operation="dlq_mark", status="connection_error").inc()
            return False
        except Exception as e:
            metrics.record_redis_error()
            logger.error(
                "DLQ marker key set failed dlq_id=%s error_type=%s err=%s",
                dlq_id,
                e.__class__.__name__,
                e,
            )
            metrics.redis_operations_total.labels(operation="dlq_mark", status="error").inc()
            return False

    def _send_to_retry_topic(self, message, retry_count: int, reason: str) -> None:
        """
        Requeue message into the next retry tier topic.

        Blocks until broker ack is received to avoid committing source offset
        before retry message is durably written.
        """
        retry_tier_name, retry_topic, delay_seconds = self._retry_tier_for_count(retry_count)
        base_event = message.value if isinstance(message.value, dict) else {"payload": message.value}
        event = dict(base_event)
        event["_retry_meta"] = {
            "retry_count": retry_count,
            "retry_tier": retry_tier_name,
            "retry_topic": retry_topic,
            "delay_seconds": delay_seconds,
            "routed_at_ts": time.time(),
            "reason": reason,
        }
        retry_key = event.get("event_id") or message.key or f"{message.topic}:{message.partition}:{message.offset}"
        headers = self._forward_trace_headers(message)
        headers.append((config.KAFKA_RETRY_HEADER, str(retry_count).encode("utf-8")))
        future = self.kafka_producer.send(
            retry_topic,
            key=retry_key,
            value=event,
            headers=headers,
        )
        metadata = future.get(timeout=5)
        self.retry_routed_count += 1
        metrics.kafka_produce_total.labels(topic=retry_tier_name, status="success").inc()
        metrics.events_retry_routed_total.labels(retry_count=str(retry_count)).inc()
        metrics.events_retry_tier_routed_total.labels(
            retry_count=str(retry_count),
            retry_tier=retry_tier_name,
        ).inc()
        logger.warning(
            "Routed to retry retry_tier=%s retry_topic=%s retry_count=%s delay_seconds=%.1f reason=%s topic=%s partition=%s offset=%s retry_partition=%s retry_offset=%s",
            retry_tier_name,
            retry_topic,
            retry_count,
            delay_seconds,
            reason,
            message.topic,
            message.partition,
            message.offset,
            metadata.partition,
            metadata.offset,
        )

    def _send_to_dlq(self, message, reason: str, error: str) -> str:
        """
        Send poisoned message to Dead Letter Queue with stable dlq_id key.
        Returns one of: "sent", "duplicate", "failed".

        DLQ semantics (explicit contract):
        - at-least-once delivery to DLQ
        - Kafka broker ack is the source of truth for "sent"
        - Redis dlq:sent marker is advisory only
        - marker failure after broker ack MUST NOT downgrade send status
        - replay/triage tooling MUST suppress duplicates by dlq_id
        """
        dlq_id = self._build_dlq_id(message)
        trace_ctx = self._extract_trace_context(message)
        if self._is_dlq_dispatched(dlq_id):
            logger.warning(
                "Suppressing potential duplicate DLQ dispatch via advisory marker "
                "dlq_id=%s topic=%s partition=%s offset=%s event_id=%s",
                dlq_id,
                message.topic,
                message.partition,
                message.offset,
                (message.value or {}).get("event_id") if isinstance(message.value, dict) else None,
            )
            metrics.events_dlq_sent_total.labels(reason="duplicate").inc()
            return "duplicate"

        try:
            dlq_payload = {
                "dlq_id": dlq_id,
                "original_topic": message.topic,
                "original_partition": message.partition,
                "original_offset": message.offset,
                "original_key": message.key,
                "original_value": message.value,
                "dlq_reason": reason,
                "error_message": error,
                "retry_count": self._extract_retry_count(message),
                "trace_id": trace_ctx["trace_id"],
                "traceparent": trace_ctx["traceparent"],
                "tracestate": trace_ctx["tracestate"],
                "timestamp": time.time(),
            }

            future = self.kafka_producer.send(
                config.KAFKA_DLQ_TOPIC,
                key=dlq_id,
                value=dlq_payload,
                headers=self._forward_trace_headers(message),
            )
            metadata = future.get(timeout=5)

            def on_dlq_success(meta):
                logger.info(
                    "DLQ sent dlq_id=%s dlq_partition=%s dlq_offset=%s topic=%s partition=%s offset=%s",
                    dlq_id,
                    meta.partition,
                    meta.offset,
                    message.topic,
                    message.partition,
                    message.offset,
                )

            on_dlq_success(metadata)

            # Persist advisory marker only after DLQ broker ack.
            # kafka_produce_total is the sole emit point for the Kafka-send outcome.
            # redis_operations_total for dlq_mark is emitted inside _mark_dlq_dispatched
            # only — do not re-emit here.
            metrics.kafka_produce_total.labels(topic="dlq", status="success").inc()
            if not self._mark_dlq_dispatched(dlq_id):
                logger.warning(
                    "DLQ marker persistence failed after broker ack; proceeding with at-least-once semantics "
                    "dlq_id=%s topic=%s partition=%s offset=%s",
                    dlq_id,
                    message.topic,
                    message.partition,
                    message.offset,
                )
            self.dlq_count += 1
            metrics.events_dlq_sent_total.labels(reason=reason).inc()
            return "sent"
        except Exception as e:
            logger.error("Failed to send message to DLQ dlq_id=%s err=%s", dlq_id, e)
            metrics.kafka_produce_total.labels(topic="dlq", status="error").inc()
            return "failed"

    def _commit_offsets(self, offsets: Dict[TopicPartition, OffsetAndMetadata]):
        """Commit a prepared offset map in a single Kafka round trip."""
        self.consumer.commit(offsets=offsets)

    def _commit_message(self, message):
        """
        Precisely commit the offset for this specific message.
        
        Raises exception on commit failure to ensure state consistency.
        Caller must handle commit failures explicitly.
        
        Args:
            message: Kafka message to commit
            
        Raises:
            Exception: If offset commit fails
        """
        try:
            tp = TopicPartition(message.topic, message.partition)
            # Commit offset+1 (next message to consume)
            offsets = {tp: OffsetAndMetadata(message.offset + 1, None)}
            self._commit_offsets(offsets)
            
            logger.debug(
                f"✅ Committed offset: topic={message.topic}, partition={message.partition}, "
                f"offset={message.offset + 1}"
            )
        except Exception as e:
            logger.error(
                f"❌ Offset commit FAILED: topic={message.topic}, partition={message.partition}, "
                f"offset={message.offset}, error={e}"
            )
            # Re-raise to force caller to handle commit failure explicitly
            raise

    def update_features(self, event: Dict[str, Any]) -> ProcessingResult:
        """
        Update Redis online features using an atomic Lua script.

        Time-decayed features use half-life based exponential decay. The script
        stores both the score-at-last-update and its last-update timestamp so each
        new event can decay the previous value before adding the new click weight.

        Returns:
            ProcessingResult: APPLIED, DUPLICATE, TRANSIENT_FAILURE, or PERMANENT_FAILURE
        """
        t0 = time.time()

        event_id = event.get("event_id")
        user_id = event.get("user_id")
        item_id = event.get("item_id")
        session_id = event.get("session_id", "")
        raw_timestamp = event.get("timestamp")
        source = event.get("source", "unknown")

        # Validate required fields - permanent failures
        if not event_id:
            logger.warning(f"⚠️  Invalid event (missing event_id): {event}")
            return ProcessingResult.PERMANENT_FAILURE

        if not user_id or not item_id:
            logger.warning(f"⚠️  Invalid event (missing user_id or item_id): {event}")
            return ProcessingResult.PERMANENT_FAILURE

        try:
            event_ts_seconds, canonical_timestamp = self._parse_event_timestamp_seconds(raw_timestamp)
        except ValueError as exc:
            logger.warning("⚠️  Invalid event timestamp event_id=%s timestamp=%r error=%s", event_id, raw_timestamp, exc)
            return ProcessingResult.PERMANENT_FAILURE

        category = self._infer_category(item_id)
        if category == "unknown":
            metrics.feature_failures_total.labels(feature="category_affinity").inc()

        try:
            dedupe_key = f"dedupe:event:{event_id}"

            # Time Redis Lua execution separately
            redis_t0 = time.time()
            result = self.lua_upsert_script(
                keys=[dedupe_key],
                args=[
                    config.DEDUPE_WINDOW_SECONDS,
                    user_id,
                    item_id,
                    format(event_ts_seconds, ".6f"),
                    canonical_timestamp,
                    category,
                    session_id,
                    config.RECENT_CLICKS_MAX,
                    format(config.CATEGORY_AFFINITY_DECAY_LAMBDA, ".18g"),
                    format(config.ITEM_CLICK_DECAY_LAMBDA, ".18g"),
                    format(config.RECENT_ITEM_CLICK_DECAY_LAMBDA, ".18g"),
                    format(config.GLOBAL_POPULARITY_DECAY_LAMBDA, ".18g"),
                    config.SESSION_EXPIRE_SECONDS,
                    config.ONLINE_FEATURE_STATE_TTL_SECONDS,
                    config.POPULARITY_1H_BUCKET_SECONDS,
                    config.POPULARITY_1H_BUCKET_TTL_SECONDS,
                    config.POPULARITY_24H_BUCKET_SECONDS,
                    config.POPULARITY_24H_BUCKET_TTL_SECONDS,
                    config.POPULARITY_7D_BUCKET_SECONDS,
                    config.POPULARITY_7D_BUCKET_TTL_SECONDS,
                    config.POPULARITY_BUCKET_PREFIX,
                ],
            )
            redis_latency_s = time.time() - redis_t0
            metrics.redis_update_latency_seconds.observe(redis_latency_s)

            if result == b"DUPLICATE" or result == "DUPLICATE":
                logger.debug(
                    f"🔁 Duplicate event (already processed): event_id={event_id}"
                )
                self.deduped_count += 1
                metrics.events_duplicate_total.inc()
                metrics.events_processed_total.labels(result="duplicate").inc()
                metrics.record_redis_success()
                return ProcessingResult.DUPLICATE

            latency_ms = (time.time() - t0) * 1000
            logger.debug(
                f"✅ Updated decayed online features: user_id={user_id}, item_id={item_id}, category={category}, source={source}, event_ts={canonical_timestamp}, latency_ms={latency_ms:.1f}"
            )
            metrics.event_processing_latency_seconds.observe(latency_ms / 1000.0)
            metrics.events_processed_total.labels(result="applied").inc()
            metrics.redis_operations_total.labels(operation="lua_decay_upsert", status="success").inc()
            metrics.record_redis_success()
            return ProcessingResult.APPLIED

        except Exception as e:
            # Record Redis latency even on exception
            if 'redis_t0' in locals():
                redis_latency_s = time.time() - redis_t0
                metrics.redis_update_latency_seconds.observe(redis_latency_s)
            
            latency_ms = (time.time() - t0) * 1000
            logger.error(
                f"❌ Failed to update features: event_id={event_id}, user_id={user_id}, item_id={item_id}, error={e}",
                exc_info=True
            )
            self.error_count += 1
            metrics.record_redis_error()
            metrics.redis_operations_total.labels(operation="lua_decay_upsert", status="error").inc()
            metrics.event_processing_latency_seconds.observe(latency_ms / 1000.0)
            
            # Classify the error to determine retry strategy
            return self._classify_error(e)

    def _infer_category(self, item_id: str) -> str:
        """Return the category for *item_id*, served from the in-process LRU cache
        wherever possible to avoid an extra Redis round trip per event.

        Flow:
          1. Check ``_category_cache`` (O(1) LRU lookup).
             - ``hit``     → return cached value immediately (no Redis I/O).
             - ``expired`` → fall through to Redis; re-populate cache on success.
             - ``miss``    → fall through to Redis; populate cache on first fetch.

          2. On Redis success, store the result (including ``"unknown"``) in the
             cache so that subsequent events for the same item are served locally.
             Caching ``"unknown"`` prevents repeated lookups for items that have
             no metadata in Redis — a common case during cold-start or for items
             outside the active catalog.

          3. On Redis error, return ``"unknown"`` without caching so the next
             event for this item retries the lookup (transient failures should
             not be persisted into the cache).

        Returns:
            Category string if metadata is available, ``"unknown"`` otherwise.
        """
        # ── Cache lookup ──────────────────────────────────────────────────────
        cached_category, cache_status = self._category_cache.get(item_id)
        metrics.category_cache_ops_total.labels(status=cache_status).inc()
        metrics.category_cache_size.set(len(self._category_cache))

        if cache_status == "hit":
            return cached_category  # type: ignore[return-value]

        # ── Cache miss or expired: fetch from Redis ───────────────────────────
        try:
            normalized_item_id = canonical_article_id(item_id)
            meta_key = item_meta_key(normalized_item_id)
            raw = self.redis_client.hget(meta_key, "category")
            metrics.record_redis_success()

            if raw:
                category = raw
                if category and category != "unknown":
                    metrics.redis_operations_total.labels(
                        operation="category_lookup", status="success"
                    ).inc()
                    self._category_cache.put(item_id, category)
                    return category

        except Exception as e:
            metrics.record_redis_error()
            logger.debug("Category lookup failed for item %s: %s", item_id, e)
            # Do NOT cache on error: transient Redis failure should not poison
            # the cache entry for this item_id.
            metrics.redis_operations_total.labels(
                operation="category_lookup", status="error"
            ).inc()
            return "unknown"

        # Metadata absent or value is "unknown": cache the absence so we do not
        # hammer Redis for this item on every subsequent event.
        self._category_cache.put(item_id, "unknown")
        metrics.redis_operations_total.labels(operation="category_lookup", status="miss").inc()
        return "unknown"

    def _process_message_internal(self, message, commit_immediately: bool) -> Tuple[ProcessingResult, bool, bool]:
        """
        Process one Kafka message with proper error handling and offset management.

        Strategy:
        - APPLIED/DUPLICATE: safe to commit source offset
        - PERMANENT_FAILURE: safe to commit only if DLQ send is acknowledged
        - TRANSIENT_FAILURE: safe to commit only if retry copy (or DLQ fallback) is acknowledged

        Does NOT block the main loop with sleep() on transient failures.

        Args:
            message: Kafka message

        Returns:
            Tuple of:
            - ProcessingResult indicating outcome
            - commit_safe: whether source offset can be advanced
            - strict_commit_required: commit failure after this message is terminal
              because downstream Kafka write (retry/DLQ) has already succeeded.
        """
        event = message.value if isinstance(message.value, dict) else {}
        trace_ctx = self._extract_trace_context(message)
        retry_count = self._extract_retry_count(message, event)

        # Check retry tier delay - if not ready, only this retry-tier partition is paused.
        if self._respect_retry_tier_delay(message, event, retry_count):
            return ProcessingResult.TRANSIENT_FAILURE, False, False

        result = self.update_features(event)

        if result == ProcessingResult.APPLIED:
            # Success - commit offset and clear retry tracking
            if commit_immediately:
                try:
                    self._commit_message(message)
                except Exception as e:
                    logger.error(
                        f"❌ CRITICAL: Successfully processed but commit failed. "
                        f"Will reprocess: topic={message.topic}, partition={message.partition}, "
                        f"offset={message.offset}, error={e}"
                    )
                    return result, False, False

            self.event_count += 1

            if self.event_count % config.LOG_INTERVAL == 0:
                elapsed = time.time() - self.start_time
                rate = self.event_count / elapsed if elapsed > 0 else 0
                logger.info(
                    f"📈 Stats: processed={self.event_count}, errors={self.error_count}, "
                    f"deduped={self.deduped_count}, retry_routed={self.retry_routed_count}, "
                    f"dlq={self.dlq_count}, rate={rate:.1f} events/sec, uptime={elapsed:.1f}s"
                )
                # Update gauge metrics
                self.metrics_server.update_uptime()
                self.metrics_server.set_processing_rate(rate)
            return result, True, False

        elif result == ProcessingResult.DUPLICATE:
            # Duplicate - commit offset (already processed)
            if commit_immediately:
                try:
                    self._commit_message(message)
                except Exception as e:
                    logger.warning(
                        f"⚠️  Duplicate detected but commit failed: topic={message.topic}, "
                        f"partition={message.partition}, offset={message.offset}, error={e}"
                    )
            logger.debug(
                "Duplicate event processed topic=%s partition=%s offset=%s",
                message.topic,
                message.partition,
                message.offset,
            )
            return result, True, False

        elif result == ProcessingResult.PERMANENT_FAILURE:
            # Permanent failure - send to DLQ and commit offset (no retry)
            event_id = event.get("event_id", "unknown") if isinstance(event, dict) else "unknown"
            logger.error(
                f"🚨 Permanent failure detected: topic={message.topic}, partition={message.partition}, "
                f"offset={message.offset}, event_id={event_id}, trace_id={trace_ctx['trace_id']}"
            )
            metrics.events_failed_total.labels(failure_type="permanent").inc()
            dlq_result = self._send_to_dlq(message, "permanent_failure", "Invalid schema or data format")
            if dlq_result in ("sent", "duplicate"):
                if commit_immediately:
                    try:
                        self._commit_message(message)
                    except Exception as e:
                        self._log_terminal_commit_uncertainty(
                            reason="permanent_failure",
                            downstream_action="dlq_sent",
                            message=message,
                            error=e,
                            event_id=event_id,
                            trace_id=trace_ctx["trace_id"],
                        )
                        metrics.events_commit_failed_total.labels(reason="dlq_send_success").inc()
                        raise DlqPublishedCommitFailedError(
                            f"DLQ published but source commit failed (permanent_failure): "
                            f"topic={message.topic} partition={message.partition} offset={message.offset}"
                        ) from e
                self.error_count += 1
                return result, True, True
            else:
                logger.error(
                    "DLQ send failed; offset not committed topic=%s partition=%s offset=%s",
                    message.topic,
                    message.partition,
                    message.offset,
                )
                metrics.events_failed_total.labels(failure_type="dlq_failed").inc()
            self.error_count += 1
            return result, False, False

        elif result == ProcessingResult.TRANSIENT_FAILURE:
            # Transient failure - persist retry count in retry topic
            next_retry = retry_count + 1

            if next_retry <= config.MAX_RETRIES:
                event_id = event.get("event_id", "unknown") if isinstance(event, dict) else "unknown"
                try:
                    self._send_to_retry_topic(
                        message,
                        retry_count=next_retry,
                        reason="transient_failure",
                    )
                except Exception as e:
                    retry_tier_name, retry_topic, _ = self._retry_tier_for_count(next_retry)
                    logger.error(
                        "Retry routing failed topic=%s partition=%s offset=%s retry_count=%s retry_topic=%s retry_tier=%s trace_id=%s err=%s",
                        message.topic,
                        message.partition,
                        message.offset,
                        next_retry,
                        retry_topic,
                        retry_tier_name,
                        trace_ctx["trace_id"],
                        e,
                    )
                    self.error_count += 1
                    metrics.kafka_produce_total.labels(topic=retry_tier_name, status="error").inc()
                    metrics.events_failed_total.labels(failure_type="transient").inc()
                    return result, False, False
                if commit_immediately:
                    try:
                        # Commit original only after retry copy is produced.
                        self._commit_message(message)
                    except Exception as e:
                        # CRITICAL: the retry message is already broker-acknowledged on retry
                        # topic, but the source offset could not be committed.
                        # Continuing would leave the source offset uncommitted; on the
                        # next restart or rebalance the broker will re-deliver the
                        # original message, and we will route it to retry again —
                        # producing a duplicate retry entry.
                        #
                        # Raising RetryPublishedCommitFailedError here terminates the
                        # consumer process so Kubernetes restarts it promptly.  After
                        # restart, the group rebalances and re-delivers the uncommitted
                        # message.  If it is routed to retry again, the retry
                        # consumer's event_id duplicate suppression (Redis Lua script) handles the
                        # duplicate safely (DUPLICATE result → commit, no feature
                        # update applied twice).
                        self._log_terminal_commit_uncertainty(
                            reason="transient_failure",
                            downstream_action="retry_sent",
                            message=message,
                            error=e,
                            event_id=event_id,
                            trace_id=trace_ctx["trace_id"],
                            retry_count=next_retry,
                        )
                        metrics.events_commit_failed_total.labels(
                            reason="retry_routed_terminating"
                        ).inc()
                        raise RetryPublishedCommitFailedError(
                            f"Retry published but source commit failed: "
                            f"topic={message.topic} partition={message.partition} "
                            f"offset={message.offset}"
                        ) from e
                logger.warning(
                    "Transient failure rerouted topic=%s partition=%s offset=%s event_id=%s retry_count=%s max_retries=%s trace_id=%s",
                    message.topic,
                    message.partition,
                    message.offset,
                    event_id,
                    next_retry,
                    config.MAX_RETRIES,
                    trace_ctx["trace_id"],
                )
                return result, True, True
            else:
                # Max retries exceeded - send to DLQ and commit
                event_id = event.get("event_id", "unknown") if isinstance(event, dict) else "unknown"
                logger.error(
                    f"🚨 Max retries exceeded ({config.MAX_RETRIES}): "
                    f"topic={message.topic}, partition={message.partition}, offset={message.offset}, "
                    f"event_id={event_id}, trace_id={trace_ctx['trace_id']}"
                )
                dlq_result = self._send_to_dlq(
                    message,
                    "max_retries_exceeded",
                    f"Failed after {config.MAX_RETRIES} retry attempts",
                )
                if dlq_result in ("sent", "duplicate"):
                    if commit_immediately:
                        try:
                            self._commit_message(message)
                        except Exception as e:
                            self._log_terminal_commit_uncertainty(
                                reason="max_retries_exceeded",
                                downstream_action="dlq_sent",
                                message=message,
                                error=e,
                                event_id=event_id,
                                trace_id=trace_ctx["trace_id"],
                            )
                            metrics.events_commit_failed_total.labels(reason="dlq_send_success").inc()
                            raise DlqPublishedCommitFailedError(
                                f"DLQ published but source commit failed (max_retries_exceeded): "
                                f"topic={message.topic} partition={message.partition} offset={message.offset}"
                            ) from e
                    self.error_count += 1
                    return result, True, True
                else:
                    logger.error(
                        "DLQ send failed after max retries; offset not committed topic=%s partition=%s offset=%s",
                        message.topic,
                        message.partition,
                        message.offset,
                    )
                    metrics.kafka_produce_total.labels(topic="dlq", status="error").inc()
                    metrics.events_failed_total.labels(failure_type="dlq_failed").inc()
                self.error_count += 1
                return result, False, False

        return result, False, False

    def process_message(self, message) -> ProcessingResult:
        """
        Process one Kafka message with immediate offset commit (legacy behavior).
        """
        carrier = self._build_trace_carrier(message)
        span_kwargs = {
            "context": observability.extract_context(carrier),
            "attributes": {
                "messaging.system": "kafka",
                "messaging.operation": "process",
                "messaging.destination.name": getattr(message, "topic", "unknown"),
                "messaging.kafka.partition": getattr(message, "partition", -1),
                "messaging.kafka.offset": getattr(message, "offset", -1),
            },
        }
        if observability.SPAN_KIND_CONSUMER is not None:
            span_kwargs["kind"] = observability.SPAN_KIND_CONSUMER

        with self.tracer.start_as_current_span("event-consumer.process", **span_kwargs) as span:
            result, _, _ = self._process_message_internal(message, commit_immediately=True)
            span.set_attribute("messaging.event.result", result.value)
            return result

    def run(self):
        """Main consumer loop with explicit processing state handling."""
        # Log actual subscribed topics based on mode
        if config.CONSUMER_MODE == "primary":
            actual_topics = [config.KAFKA_TOPIC]
        else:
            actual_topics = list(config.KAFKA_RETRY_TOPICS)
        
        logger.info("[START] Event Consumer started")
        logger.info(f"[INFO] Mode: {config.CONSUMER_MODE}")
        logger.info(f"[INFO] Consuming from: {actual_topics}")
        logger.info(f"[INFO] Updating Redis: {config.REDIS_HOST}:{config.REDIS_PORT}")
        
        # Warn if auto_offset_reset=latest is explicitly configured (potential message loss)
        if config.KAFKA_AUTO_OFFSET_RESET == "latest":
            logger.warning(
                "[WARN] KAFKA_AUTO_OFFSET_RESET=latest: new consumer groups will skip historical messages. "
                "This may cause data loss on first deployment or after offset deletion. "
                "Consider using 'earliest' for production deployments."
            )
        
        logger.info("-" * 60)

        message_count = 0
        last_resume_check = time.time()
        resume_check_interval = 1.0  # Check every second
        last_metrics_log = time.time()
        metrics_log_interval = 60.0  # Log pause status every minute
        # Lag must stay fresh regardless of message throughput.
        # Message-count-driven updates go stale at low traffic; a fixed 30 s
        # interval ensures operators always see a recent lag reading.
        last_lag_update = time.time()
        lag_update_interval_s = 30.0

        try:
            # Use poll() instead of iterator to ensure resume checks even without new messages
            while True:
                # Check for messages with timeout to ensure periodic resume checks
                message_batch = self.consumer.poll(
                    timeout_ms=int(resume_check_interval * 1000),
                    max_records=config.KAFKA_POLL_MAX_RECORDS,
                )
                self.last_poll_ts = time.time()

                # All periodic checks use `now` so we only call time.time() once per loop.
                now = time.time()

                if now - last_resume_check >= resume_check_interval:
                    self._resume_ready_partitions()
                    last_resume_check = now

                # Time-driven lag update: always fresh, independent of message rate.
                if now - last_lag_update >= lag_update_interval_s:
                    self._update_kafka_lag()
                    last_lag_update = now

                # Periodic pause status logging
                if now - last_metrics_log >= metrics_log_interval and self.paused_partitions:
                    logger.info(
                        "📊 Retry backoff status: %d partition(s) currently paused",
                        len(self.paused_partitions)
                    )
                    last_metrics_log = now
                
                # Process received messages and track highest safe offset per partition.
                pending_commits: Dict[TopicPartition, OffsetAndMetadata] = {}
                strict_commit_required = False
                for tp, messages in message_batch.items():
                    highest_safe_offset = None
                    for message in messages:
                        try:
                            _, commit_safe, strict_required = self._process_message_internal(
                                message,
                                commit_immediately=False,
                            )
                            if strict_required:
                                strict_commit_required = True
                            if commit_safe:
                                highest_safe_offset = message.offset
                            else:
                                # Stop this partition at the first unsafe offset so we
                                # never commit beyond a failed message.
                                break
                            message_count += 1
                        except (RetryPublishedCommitFailedError, DlqPublishedCommitFailedError):
                            # Do NOT swallow: propagate so the process terminates
                            # and Kubernetes restarts it. Downstream write (retry or
                            # DLQ) is already broker-acknowledged; clean restart ensures the
                            # uncommitted source offset is re-delivered and handled
                            # idempotently (retry duplicate suppression; DLQ replay duplicate suppression by dlq_id).
                            raise
                        except Exception as e:
                            logger.error(f"❌ Error processing message: {e}")
                            self.error_count += 1
                            break
                    if highest_safe_offset is not None:
                        pending_commits[tp] = OffsetAndMetadata(highest_safe_offset + 1, None)

                if pending_commits:
                    try:
                        self._commit_offsets(pending_commits)
                    except Exception as e:
                        logger.critical(
                            "CRITICAL: Batch source offset commit failed offsets=%s err=%s",
                            {f"{tp.topic}:{tp.partition}": meta.offset for tp, meta in pending_commits.items()},
                            e,
                        )
                        metrics.events_commit_failed_total.labels(reason="batch_commit_failed").inc()
                        if strict_commit_required:
                            raise RetryPublishedCommitFailedError(
                                "Batch commit failed after retry/DLQ publish; terminating for clean restart"
                            ) from e
                        raise

        except KeyboardInterrupt:
            logger.info("⏹️  Shutting down gracefully...")
        except (RetryPublishedCommitFailedError, DlqPublishedCommitFailedError) as e:
            # Downstream write is already broker-acknowledged but the source-offset
            # commit failed.  Exit non-zero so Kubernetes restarts the pod immediately
            # rather than waiting for the liveness probe to time out (~90 s).
            # The finally block below still runs and calls close().
            logger.critical(
                "CRITICAL: offset commit failed after broker-acked downstream write; "
                "intentional fail-fast exit reason=downstream_commit_uncertain err=%s",
                e,
                exc_info=True,
            )
            self.loop_alive = False
            metrics.consumer_health.set(0)
            metrics.consumer_terminations_total.labels(reason="downstream_commit_uncertain").inc()
            sys.exit(1)
        except Exception as e:
            logger.error("❌ Fatal error in consumer loop: %s", e, exc_info=True)
            self.loop_alive = False
            metrics.consumer_health.set(0)
            metrics.consumer_terminations_total.labels(reason="fatal_loop_error").inc()
            # Exit non-zero so Kubernetes restarts the pod immediately instead of
            # waiting up to failureThreshold × periodSeconds (~90 s) for the liveness
            # probe to detect the dead loop.  The finally block still runs (close()).
            sys.exit(1)
        finally:
            # Resume any paused partitions before shutdown
            if self.paused_partitions:
                logger.info(f"🔄 Resuming {len(self.paused_partitions)} paused partition(s) before shutdown")
                try:
                    self.consumer.resume(*list(self.paused_partitions.keys()))
                except Exception as e:
                    logger.warning(f"Failed to resume partitions during shutdown: {e}")
            self.close()

    def _liveness_check(self) -> dict:
        """Return liveness status for the Kubernetes livenessProbe (/live).

        Checks ONLY in-process state.  Never calls Kafka, Redis, or any external
        dependency.  A failure here causes Kubernetes to restart the pod, so the
        bar must be high: only genuine process-level failures should return live=False.

        The poll-freshness threshold is deliberately conservative (300 s, matching
        the Kafka default max.poll.interval.ms) so that a prolonged rebalance or
        momentary broker outage does not trigger an unnecessary restart.

        Returns a dict with 'live' (bool) and 'checks' (per-component detail).
        """
        # 300 s: matches Kafka default max.poll.interval.ms.  Using a threshold
        # below this would risk restarting a pod whose loop is alive but blocked
        # inside consumer.poll() during a legitimate group rebalance.
        _LIVENESS_STALE_THRESHOLD_S = 300.0

        checks: Dict[str, Any] = {}
        live = True

        # 1. Consumer loop state — False only on fatal unhandled exception.
        if not self.loop_alive:
            checks["consumer_loop"] = {"status": "dead"}
            live = False
        else:
            checks["consumer_loop"] = {"status": "ok"}

        # 2. Poll freshness (conservative threshold).
        # "pending" means the loop has not yet completed its first poll; this is
        # expected during startup and must not fail liveness.
        if self.last_poll_ts is not None:
            stale_s = time.time() - self.last_poll_ts
            if stale_s > _LIVENESS_STALE_THRESHOLD_S:
                checks["poll_freshness"] = {
                    "status": "stale",
                    "last_poll_seconds_ago": round(stale_s, 1),
                    "threshold_s": _LIVENESS_STALE_THRESHOLD_S,
                }
                live = False
            else:
                checks["poll_freshness"] = {
                    "status": "ok",
                    "last_poll_seconds_ago": round(stale_s, 1),
                }
        else:
            checks["poll_freshness"] = {"status": "pending"}

        return {"live": live, "checks": checks}

    def _health_check(self) -> dict:
        """Return readiness status for the Kubernetes readinessProbe (/ready)
        and the backward-compat /health endpoint.

        Checks whether the consumer is ready to process traffic: loop state,
        poll freshness, Kafka partition assignment, and Redis availability.
        Redis failures are informational only — a transient Redis outage marks
        the pod informational-unhealthy but does NOT flip ready=False (the
        consumer handles per-message Redis errors via its own retry/DLQ pipeline).

        A ready=False response causes Kubernetes to mark the pod NotReady, which
        excludes it from rolling-deployment gates.  It does NOT restart the pod.

        Returns a dict with 'healthy' (bool) and 'checks' (per-component detail).
        """
        checks: Dict[str, Any] = {}
        healthy = True

        # 1. Consumer loop state
        if not self.loop_alive:
            checks["consumer_loop"] = {"status": "dead"}
            healthy = False
        else:
            checks["consumer_loop"] = {"status": "ok"}

        # 2. Poll freshness — catches a hung loop that doesn't raise
        _STALE_THRESHOLD_S = 120.0
        if self.last_poll_ts is not None:
            stale_s = time.time() - self.last_poll_ts
            if stale_s > _STALE_THRESHOLD_S:
                checks["poll_freshness"] = {
                    "status": "stale",
                    "last_poll_seconds_ago": round(stale_s, 1),
                }
                healthy = False
            else:
                checks["poll_freshness"] = {
                    "status": "ok",
                    "last_poll_seconds_ago": round(stale_s, 1),
                }
        else:
            checks["poll_freshness"] = {"status": "pending"}

        # 3. Kafka consumer assignment
        # Primary consumer MUST hold partitions after the rebalance grace period.
        # Zero assignment on a primary consumer means the consumer group has lost
        # its position (rebalance stuck, broker unreachable, or group coordinator
        # unresponsive) and it cannot process any traffic — treat as unhealthy.
        # Retry consumer is more lenient: it may legitimately have zero assignment
        # when the retry topic is empty or not yet assigned.
        _ASSIGNMENT_GRACE_S = 30.0
        try:
            partitions = self.consumer.assignment()
            partition_count = len(partitions)
            startup_elapsed = time.time() - self.start_time

            if partition_count == 0 and startup_elapsed > _ASSIGNMENT_GRACE_S:
                if config.CONSUMER_MODE == "primary":
                    checks["kafka"] = {
                        "status": "no_assignment",
                        "assigned_partitions": 0,
                        "startup_elapsed_s": round(startup_elapsed, 1),
                        "detail": "primary consumer lost partition assignment",
                    }
                    healthy = False
                else:
                    # Retry consumer: empty assignment is acceptable (topic may be idle)
                    checks["kafka"] = {
                        "status": "ok_empty",
                        "assigned_partitions": 0,
                        "note": "retry consumer — empty assignment is acceptable",
                    }
            else:
                checks["kafka"] = {"status": "ok", "assigned_partitions": partition_count}
        except Exception as exc:
            checks["kafka"] = {"status": "error", "detail": type(exc).__name__}
            healthy = False

        # 4. Redis ping (informational — does not flip healthy to False)
        #
        # Design choice: Redis failure keeps pod Ready; traffic goes to retry/DLQ.
        # This is intentional. Flipping ready=False on a Redis blip would cause a
        # coordinated Kafka rebalance storm across all pods.
        #
        # Operator visibility for this decision lives in three places:
        #   Metrics:  redis_consecutive_errors, redis_unavailable_duration_seconds
        #   Rate:     rate(events_retry_routed_total[5m])
        #   Alerts:   observability/event-consumer-alerts.yaml
        #   Runbook:  event-consumer/RUNBOOK.md (§4: "Consumer Ready but Feature Updates Failing")
        try:
            t0 = time.time()
            self.redis_client.ping()
            metrics.record_redis_success()
            checks["redis"] = {
                "status": "ok",
                "latency_ms": round((time.time() - t0) * 1000, 1),
            }
        except Exception as exc:
            metrics.record_redis_error()
            checks["redis"] = {"status": "error", "detail": type(exc).__name__}
        metrics.refresh_redis_unavailable_duration()

        # 5. Paused partitions count (informational)
        checks["paused_partitions"] = {"count": len(self.paused_partitions)}

        return {"healthy": healthy, "checks": checks}

    def _resume_ready_partitions(self):
        """Resume partitions whose retry backoff has expired"""
        if not self.paused_partitions:
            return
        
        now = time.time()
        to_resume = []
        
        for tp, resume_at in list(self.paused_partitions.items()):
            if now >= resume_at:
                to_resume.append(tp)
        
        if to_resume:
            try:
                self.consumer.resume(*to_resume)
                for tp in to_resume:
                    del self.paused_partitions[tp]
                    logger.info(
                        "▶️  Resumed partition topic=%s partition=%s (remaining_paused=%d)",
                        tp.topic,
                        tp.partition,
                        len(self.paused_partitions),
                    )
                metrics.retry_partitions_paused.set(len(self.paused_partitions))
            except Exception as e:
                logger.error(f"Failed to resume partitions {to_resume}: {e}")
                # Clear the dict anyway to avoid permanent pause
                for tp in to_resume:
                    self.paused_partitions.pop(tp, None)
                metrics.retry_partitions_paused.set(len(self.paused_partitions))
    
    def _update_kafka_lag(self):
        """Update Kafka consumer lag metrics"""
        try:
            for tp in self.consumer.assignment():
                # Get committed offset
                committed = self.consumer.committed(tp)
                if committed is None:
                    continue
                
                # Get end offset (high water mark)
                end_offsets = self.consumer.end_offsets([tp])
                end_offset = end_offsets.get(tp, 0)
                
                # Calculate lag
                lag = end_offset - committed
                metrics.kafka_consumer_lag.labels(
                    topic=tp.topic,
                    partition=str(tp.partition)
                ).set(max(0, lag))
        except Exception as e:
            logger.warning(f"Failed to update Kafka lag metrics: {e}")

    def close(self):
        """Cleanup resources"""
        logger.info("[SHUTDOWN] Shutting down consumer...")
        
        # Mark consumer as unhealthy during shutdown
        metrics.consumer_health.set(0)

        # Flush producer to ensure all pending retry/DLQ messages are sent
        if self.kafka_producer:
            try:
                logger.info("Flushing Kafka producer...")
                self.kafka_producer.flush(timeout=5)
                self.kafka_producer.close(timeout=5)
            except Exception as e:
                logger.error(f"❌ Error closing Kafka producer: {e}")
        
        if self.consumer:
            try:
                self.consumer.close()
            except Exception as e:
                logger.error(f"❌ Error closing consumer: {e}")
        
        if self.redis_client:
            try:
                self.redis_client.close()
            except Exception as e:
                logger.error(f"❌ Error closing Redis client: {e}")
        
        if self.metrics_server:
            try:
                self.metrics_server.stop()
            except Exception as e:
                logger.error(f"❌ Error stopping metrics server: {e}")
        
        logger.info(
            f"[SUCCESS] Shutdown complete. Total processed: {self.event_count}, errors: {self.error_count}, "
            f"retry_routed: {self.retry_routed_count}, dlq: {self.dlq_count}"
        )


def main():
    """Entry point"""
    consumer = EventConsumer()
    consumer.run()


if __name__ == "__main__":
    main()
