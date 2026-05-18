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
from typing import Dict, Any, Optional, Tuple

import redis
from kafka import KafkaConsumer, KafkaProducer

import config
import metrics
import observability
from metrics import MetricsServer

# ---------------------------------------------------------------------------
# Re-export new-module symbols at package level so existing imports keep working
# ---------------------------------------------------------------------------
from models import ProcessingResult  # noqa: F401
from redis_feature_writer import RedisFeatureWriter, LUA_UPSERT_FEATURES  # noqa: F401
from trace_context import (  # noqa: F401
    extract_trace_context as _tc_extract,
    build_trace_carrier as _tc_carrier,
    forward_trace_headers as _tc_forward,
)
from feature_update_handler import FeatureUpdateHandler, _CategoryCache  # noqa: F401
from retry_router import (
    RetryRouter,
)
from kafka_loop import KafkaConsumerLoop
from health import HealthChecker

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    handlers=[logging.StreamHandler(sys.stdout)],
)
logger = logging.getLogger(__name__)


def decay_score(
    previous_score: float,
    previous_timestamp: Optional[float],
    current_timestamp: float,
    decay_lambda: float,
) -> float:
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
    return (
        decay_score(previous_score, previous_timestamp, current_timestamp, decay_lambda)
        + increment
    )


def popularity_rank_score(
    actual_score: float, last_update_timestamp: float, decay_lambda: float
) -> float:
    """Return the Redis ZSET ranking surrogate for decayed popularity."""
    if actual_score <= 0.0:
        raise ValueError("actual_score must be positive for popularity ranking")
    return math.log(actual_score) + (decay_lambda * last_update_timestamp)


class EventConsumer:
    """Real-time event consumer that processes click events and updates Redis features.

    Redis Cluster Compatibility:
        The Lua script is INCOMPATIBLE with Redis Cluster mode.  See redis_feature_writer.py
        for the full compatibility note.  A startup check in _connect_redis() fails fast if
        cluster mode is detected.

    This class is the assembly root: it wires together FeatureUpdateHandler,
    RetryRouter, KafkaConsumerLoop, and HealthChecker via constructor injection
    and keeps thin delegation wrappers for backward compatibility with existing tests.
    """

    # Expose the Lua script as a class attribute so tests/callers that reference
    # EventConsumer.LUA_UPSERT_FEATURES continue to work unchanged.
    LUA_UPSERT_FEATURES = LUA_UPSERT_FEATURES

    def __init__(self):
        # --------------- counters (still on self for stats logging) ---------------
        self.event_count = 0
        self.error_count = 0
        self.deduped_count = 0
        self.dlq_count = 0
        self.retry_routed_count = 0
        self.start_time = time.time()
        self.last_poll_ts: Optional[float] = None
        self.loop_alive = True

        self.tracer = observability.setup_tracing(service_name="event-consumer")
        self.metrics_server = MetricsServer(port=config.METRICS_PORT)

        self._connect_redis()
        self._connect_kafka()
        self._connect_kafka_producer()

        # --- Lua script (registered on connected client) --------------------------
        # Exposed as an instance attribute so that tests can do:
        #   consumer.lua_upsert_script = mock_script
        # and the assignment propagates through to FeatureUpdateHandler via the
        # property setter defined below.
        _raw_script = self.redis_client.register_script(LUA_UPSERT_FEATURES)
        logger.info(
            "[SUCCESS] Loaded atomic duplicate-suppression + decayed-feature Lua script"
        )

        # --- Feature update handler -----------------------------------------------
        self._feature_update_handler = FeatureUpdateHandler(
            lua_upsert_script=_raw_script,
            redis_client=self.redis_client,
            category_cache_max_size=config.CATEGORY_CACHE_MAX_SIZE,
            category_cache_ttl_seconds=config.CATEGORY_CACHE_TTL_SECONDS,
        )

        # --- Retry router ----------------------------------------------------------
        self._retry_router = RetryRouter(
            kafka_producer=self.kafka_producer,
            redis_client=self.redis_client,
            feature_update_handler=self._feature_update_handler,
            kafka_consumer=self.consumer,
        )
        # paused_partitions is owned by RetryRouter; expose on self for backward compat
        # and for test assertions.
        self.paused_partitions = self._retry_router.paused_partitions

        # --- Kafka consumer loop --------------------------------------------------
        # Pass self._process_message_internal as process_message_fn so that test
        # code which does consumer._process_message_internal = mock automatically
        # takes effect inside the loop without additional patching.
        self._kafka_loop = KafkaConsumerLoop(
            kafka_consumer=self.consumer,
            retry_router=self._retry_router,
            paused_partitions=self.paused_partitions,
            get_loop_alive=lambda: self.loop_alive,
            set_loop_alive=lambda v: setattr(self, "loop_alive", v),
            get_event_count=lambda: self.event_count,
            add_event_count=lambda n: setattr(
                self, "event_count", self.event_count + n
            ),
            get_start_time=lambda: self.start_time,
            set_last_poll_ts=lambda ts: setattr(self, "last_poll_ts", ts),
            # Use a lambda so test assignments of consumer._process_message_internal
            # are resolved at call time, not at construction time.
            # Pass commit_immediately as a keyword arg so that test mocks with
            # signature (m, **_) capture it in **_ rather than failing on arity.
            process_message_fn=lambda msg, commit_immediately: (
                self._process_message_internal(
                    msg, commit_immediately=commit_immediately
                )
            ),
            metrics_server=self.metrics_server,
        )

        # --- Health checker -------------------------------------------------------
        self._health_checker = HealthChecker(
            get_loop_alive=lambda: self.loop_alive,
            get_last_poll_ts=lambda: self.last_poll_ts,
            get_paused_partitions=lambda: self.paused_partitions,
            get_start_time=lambda: self.start_time,
            kafka_consumer=self.consumer,
            redis_client=self.redis_client,
        )

        self.metrics_server.set_liveness_check(self._liveness_check)
        self.metrics_server.set_health_check(self._health_check)
        self.metrics_server.start()

    # ---------------------------------------------------------------------------
    # lua_upsert_script property — backward compat with tests that assign it
    # ---------------------------------------------------------------------------

    @property
    def lua_upsert_script(self):
        return self._feature_update_handler.lua_upsert_script

    @lua_upsert_script.setter
    def lua_upsert_script(self, value):
        self._feature_update_handler.lua_upsert_script = value

    # ---------------------------------------------------------------------------
    # Connection helpers (unchanged)
    # ---------------------------------------------------------------------------

    def _connect_redis(self):
        """Connect to Redis with production-grade connection pooling and resource limits."""
        try:
            pool_kwargs = dict(
                host=config.REDIS_HOST,
                port=config.REDIS_PORT,
                db=config.REDIS_DB,
                max_connections=config.REDIS_MAX_CONNECTIONS,
                socket_connect_timeout=config.REDIS_SOCKET_CONNECT_TIMEOUT_SEC,
                socket_timeout=config.REDIS_SOCKET_TIMEOUT_SEC,
                health_check_interval=config.REDIS_HEALTH_CHECK_INTERVAL_SEC,
                retry_on_timeout=config.REDIS_RETRY_ON_TIMEOUT,
                decode_responses=True,
            )
            if config.REDIS_TLS:
                pool_kwargs["ssl"] = True
                pool_kwargs["ssl_cert_reqs"] = "required"
            pool = redis.ConnectionPool(**pool_kwargs)
            self.redis_client = redis.Redis(connection_pool=pool)

            ping_start = time.time()
            self.redis_client.ping()
            ping_latency = time.time() - ping_start

            logger.info(
                "[SUCCESS] Connected to Redis: %s:%s (pool_size=%s, "
                "connect_timeout=%ss, cmd_timeout=%ss, ping_latency=%.1fms)",
                config.REDIS_HOST,
                config.REDIS_PORT,
                config.REDIS_MAX_CONNECTIONS,
                config.REDIS_SOCKET_CONNECT_TIMEOUT_SEC,
                config.REDIS_SOCKET_TIMEOUT_SEC,
                ping_latency * 1000,
            )
            metrics.record_redis_success()
            metrics.redis_operations_total.labels(
                operation="ping", status="success"
            ).inc()

            # Fail-fast: Verify Redis is NOT in cluster mode
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
                logger.info(
                    "[SUCCESS] Redis cluster mode check passed (cluster_enabled=0)"
                )
            except redis.ResponseError as e:
                logger.warning(
                    "Could not verify Redis cluster mode (INFO CLUSTER failed: %s). "
                    "Assuming standalone mode.",
                    e,
                )

        except redis.ConnectionError as e:
            logger.error(
                "[ERROR] Redis connection failed: %s: %s (host=%s, port=%s)",
                e.__class__.__name__,
                e,
                config.REDIS_HOST,
                config.REDIS_PORT,
            )
            metrics.redis_available.set(0)
            metrics.consumer_health.set(0)
            metrics.redis_operations_total.labels(
                operation="ping", status="error"
            ).inc()
            sys.exit(1)
        except redis.TimeoutError as e:
            logger.error(
                "[ERROR] Redis connection timeout: %s (connect_timeout=%ss)",
                e,
                config.REDIS_SOCKET_CONNECT_TIMEOUT_SEC,
            )
            metrics.redis_available.set(0)
            metrics.consumer_health.set(0)
            metrics.redis_operations_total.labels(
                operation="ping", status="timeout"
            ).inc()
            sys.exit(1)
        except Exception as e:
            logger.error(
                "[ERROR] Unexpected Redis connection error: %s: %s",
                e.__class__.__name__,
                e,
            )
            metrics.redis_available.set(0)
            metrics.consumer_health.set(0)
            metrics.redis_operations_total.labels(
                operation="ping", status="error"
            ).inc()
            sys.exit(1)

    def _connect_kafka(self):
        """Connect to Kafka and create consumer."""
        try:
            topics_to_subscribe = []
            if config.CONSUMER_MODE == "primary":
                topics_to_subscribe = [config.KAFKA_TOPIC]
                logger.info("🔵 Running in PRIMARY mode - consuming only main traffic")
            elif config.CONSUMER_MODE == "retry":
                topics_to_subscribe = list(config.KAFKA_RETRY_TOPICS)
                logger.info(
                    "🟠 Running in RETRY mode - consuming tiered retry traffic: %s",
                    topics_to_subscribe,
                )
            else:
                logger.error(
                    "Invalid CONSUMER_MODE: %s. Must be 'primary' or 'retry'.",
                    config.CONSUMER_MODE,
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
                max_poll_interval_ms=300_000,
                **self._kafka_auth_kwargs(),
            )
            logger.info(
                "[SUCCESS] Connected to Kafka: %s", config.KAFKA_BOOTSTRAP_SERVERS
            )
            logger.info("[INFO] Subscribed to topics: %s", topics_to_subscribe)
            logger.info("[INFO] Consumer group: %s", config.KAFKA_GROUP_ID)
        except Exception as e:
            logger.error("Failed to connect to Kafka: %s", e)
            metrics.consumer_health.set(0)
            sys.exit(1)

    def _connect_kafka_producer(self):
        """Connect to Kafka producer for retry and Dead Letter Queue."""
        try:
            base_kwargs = dict(
                bootstrap_servers=config.KAFKA_BOOTSTRAP_SERVERS.split(","),
                value_serializer=lambda v: json.dumps(v).encode("utf-8"),
                key_serializer=lambda k: k.encode("utf-8") if k else None,
                acks="all",
                retries=3,
                max_in_flight_requests_per_connection=5,
                request_timeout_ms=3_500,
                compression_type="gzip",
                **self._kafka_auth_kwargs(),
            )
            extended_kwargs = {
                **base_kwargs,
                "enable_idempotence": True,
                "delivery_timeout_ms": 4_500,
            }
            try:
                self.kafka_producer = KafkaProducer(**extended_kwargs)
            except AssertionError as e:
                if "Unrecognized configs" in str(e):
                    # kafka-python 2.0.2 does not support enable_idempotence / delivery_timeout_ms
                    logger.warning(
                        "kafka-python does not support extended producer configs, using base config: %s", e
                    )
                    self.kafka_producer = KafkaProducer(**base_kwargs)
                else:
                    raise
            logger.info(
                "Producer connected for retry_tiers=%s, dlq=%s with idempotence enabled",
                config.KAFKA_RETRY_TOPICS,
                config.KAFKA_DLQ_TOPIC,
            )
        except Exception as e:
            logger.error("Failed to connect producer: %s", e, exc_info=True)
            metrics.consumer_health.set(0)
            sys.exit(1)

    def _kafka_auth_kwargs(self):
        """Build Kafka auth config from environment-driven settings."""
        auth_kwargs = {}
        normalized_security_protocol = (
            (config.KAFKA_SECURITY_PROTOCOL or "").strip().upper()
        )
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

    # ---------------------------------------------------------------------------
    # Delegation wrappers (backward compat with existing tests)
    # ---------------------------------------------------------------------------

    def update_features(self, event: Dict[str, Any]) -> ProcessingResult:
        """Delegation wrapper — kept for backward compat with tests.

        All logic lives in FeatureUpdateHandler.update_features.
        """
        return self._feature_update_handler.update_features(event)

    def _infer_category(self, item_id: str) -> str:
        """Delegation wrapper — kept for backward compat with tests calling this directly.

        All logic lives in FeatureUpdateHandler._infer_category.
        """
        return self._feature_update_handler._infer_category(item_id)

    def _process_message_internal(
        self, message, commit_immediately: bool = True
    ) -> Tuple[ProcessingResult, bool, bool]:
        """Delegation wrapper — kept for backward compat with tests that mock/call it.

        All logic lives in RetryRouter.process_message_internal.
        """
        return self._retry_router.process_message_internal(message, commit_immediately)

    def process_message(self, message) -> ProcessingResult:
        """Process one Kafka message with immediate offset commit (legacy behavior)."""
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

        with self.tracer.start_as_current_span(
            "event-consumer.process", **span_kwargs
        ) as span:
            result, _, _ = self._process_message_internal(
                message, commit_immediately=True
            )
            span.set_attribute("messaging.event.result", result.value)
            return result

    def run(self):
        """Main consumer loop — delegates to KafkaConsumerLoop, then closes."""
        try:
            self._kafka_loop.run()
        finally:
            self.close()

    # ---------------------------------------------------------------------------
    # Health check delegation wrappers
    # ---------------------------------------------------------------------------

    def _liveness_check(self) -> dict:
        return self._health_checker.liveness_check()

    def _health_check(self) -> dict:
        return self._health_checker.health_check()

    # ---------------------------------------------------------------------------
    # Trace context helpers (still needed for process_message)
    # ---------------------------------------------------------------------------

    def _extract_trace_context(self, message) -> Dict[str, Optional[str]]:
        return self._retry_router._extract_trace_context(message)

    def _build_trace_carrier(self, message) -> Dict[str, str]:
        trace_ctx = self._extract_trace_context(message)
        carrier: Dict[str, str] = {}
        if trace_ctx["traceparent"]:
            carrier[config.KAFKA_TRACEPARENT_HEADER] = trace_ctx["traceparent"]
        if trace_ctx["tracestate"]:
            carrier[config.KAFKA_TRACESTATE_HEADER] = trace_ctx["tracestate"]
        return carrier

    # ---------------------------------------------------------------------------
    # Partition resume / lag (delegated to KafkaConsumerLoop)
    # ---------------------------------------------------------------------------

    def _resume_ready_partitions(self):
        self._kafka_loop._resume_ready_partitions()

    def _update_kafka_lag(self):
        self._kafka_loop._update_kafka_lag()

    # ---------------------------------------------------------------------------
    # Shutdown
    # ---------------------------------------------------------------------------

    def close(self):
        """Cleanup resources."""
        logger.info("[SHUTDOWN] Shutting down consumer...")
        metrics.consumer_health.set(0)

        if self.kafka_producer:
            try:
                logger.info("Flushing Kafka producer...")
                self.kafka_producer.flush(timeout=5)
                self.kafka_producer.close(timeout=5)
            except Exception as e:
                logger.error("Error closing Kafka producer: %s", e)

        if self.consumer:
            try:
                self.consumer.close()
            except Exception as e:
                logger.error("Error closing consumer: %s", e)

        if self.redis_client:
            try:
                self.redis_client.close()
            except Exception as e:
                logger.error("Error closing Redis client: %s", e)

        if self.metrics_server:
            try:
                self.metrics_server.stop()
            except Exception as e:
                logger.error("Error stopping metrics server: %s", e)

        logger.info(
            "[SUCCESS] Shutdown complete. Total processed: %s, errors: %s, "
            "retry_routed: %s, dlq: %s",
            self.event_count,
            self.error_count,
            self.retry_routed_count,
            self.dlq_count,
        )


def main():
    """Entry point."""
    consumer = EventConsumer()
    consumer.run()


if __name__ == "__main__":
    main()
