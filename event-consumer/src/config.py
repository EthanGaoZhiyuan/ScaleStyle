"""Configuration for Event Consumer Service."""

import os
import math

# Kafka Configuration
KAFKA_BOOTSTRAP_SERVERS = os.getenv("KAFKA_BOOTSTRAP_SERVERS")
if not KAFKA_BOOTSTRAP_SERVERS:
    raise RuntimeError("KAFKA_BOOTSTRAP_SERVERS must be explicitly set")
KAFKA_TOPIC = os.getenv("KAFKA_TOPIC", "scalestyle.clicks")
KAFKA_AUTO_OFFSET_RESET = os.getenv("KAFKA_AUTO_OFFSET_RESET", "earliest")
KAFKA_DLQ_TOPIC = os.getenv(
    "KAFKA_DLQ_TOPIC", "scalestyle.clicks.dlq"
)  # Dead Letter Queue
KAFKA_RETRY_HEADER = os.getenv("KAFKA_RETRY_HEADER", "x-retry-count")
KAFKA_TRACEPARENT_HEADER = os.getenv("KAFKA_TRACEPARENT_HEADER", "traceparent")
KAFKA_TRACESTATE_HEADER = os.getenv("KAFKA_TRACESTATE_HEADER", "tracestate")
KAFKA_SECURITY_PROTOCOL = os.getenv("KAFKA_SECURITY_PROTOCOL", "PLAINTEXT")
KAFKA_SASL_MECHANISM = os.getenv("KAFKA_SASL_MECHANISM", "SCRAM-SHA-512")
KAFKA_USERNAME = os.getenv("KAFKA_USERNAME")
KAFKA_PASSWORD = os.getenv("KAFKA_PASSWORD")
KAFKA_SSL_CAFILE = os.getenv("KAFKA_SSL_CAFILE")
KAFKA_POLL_MAX_RECORDS = int(os.getenv("KAFKA_POLL_MAX_RECORDS", "100"))

# Tiered retry topology:
# - primary consumer routes transient failures into retry-1s
# - retry worker consumes all retry tiers
# - failures cascade retry-1s -> retry-10s -> retry-60s -> DLQ
KAFKA_RETRY_TOPIC_1S = os.getenv("KAFKA_RETRY_TOPIC_1S", "scalestyle.clicks.retry.1s")
KAFKA_RETRY_TOPIC_10S = os.getenv(
    "KAFKA_RETRY_TOPIC_10S", "scalestyle.clicks.retry.10s"
)
KAFKA_RETRY_TOPIC_60S = os.getenv(
    "KAFKA_RETRY_TOPIC_60S", "scalestyle.clicks.retry.60s"
)
# Backward-compat alias for code/tests that still refer to the "first" retry topic.
KAFKA_RETRY_TOPIC = KAFKA_RETRY_TOPIC_1S
KAFKA_RETRY_TIERS = [
    ("retry_1s", KAFKA_RETRY_TOPIC_1S, 1.0),
    ("retry_10s", KAFKA_RETRY_TOPIC_10S, 10.0),
    ("retry_60s", KAFKA_RETRY_TOPIC_60S, 60.0),
]
KAFKA_RETRY_TOPICS = [topic for _, topic, _ in KAFKA_RETRY_TIERS]

# Consumer Mode Configuration
# REQUIRED: Must be explicitly set to "primary" or "retry"
# - "primary": only consume from main topic (scalestyle.clicks)
# - "retry": consume all retry tiers (retry.1s / retry.10s / retry.60s)
# NO DEFAULT - forces explicit configuration to prevent accidental mixed-mode consumption
CONSUMER_MODE = os.getenv("CONSUMER_MODE")

if CONSUMER_MODE not in {"primary", "retry"}:
    raise RuntimeError(
        f"CONSUMER_MODE must be explicitly set to 'primary' or 'retry'. "
        f"Current value: {CONSUMER_MODE!r}. "
        f"Mixed-mode consumption ('both') is not supported for production safety."
    )

# Consumer Group ID
# Automatically derived based on mode to ensure isolation:
# - primary mode: "{base_group_id}-primary"
# - retry mode: "{base_group_id}-retry"
KAFKA_GROUP_ID_BASE = os.getenv("KAFKA_GROUP_ID", "event-consumer")

if CONSUMER_MODE == "primary":
    KAFKA_GROUP_ID = f"{KAFKA_GROUP_ID_BASE}-primary"
elif CONSUMER_MODE == "retry":
    KAFKA_GROUP_ID = f"{KAFKA_GROUP_ID_BASE}-retry"
else:
    # Should never reach here due to validation above
    raise RuntimeError(f"Invalid CONSUMER_MODE: {CONSUMER_MODE}")

# Redis Configuration
# Resource limits match inference service standards (L4 Top / P50 guard)
REDIS_HOST = os.getenv("REDIS_HOST", "localhost")
REDIS_PORT = int(os.getenv("REDIS_PORT", "6379"))
REDIS_DB = int(os.getenv("REDIS_DB", "0"))
# REDIS_TLS: set to "true" in production (EKS) to enforce TLS against ElastiCache.
# Defaults to false for local docker-compose where the Redis container has no TLS.
REDIS_TLS = os.getenv("REDIS_TLS", "false").lower() in ("1", "true", "yes")

# Connection Pool Configuration
# max_connections: Hard limit on concurrent Redis connections
# Similar to gateway's WebClient max_connections=100, scaled for event throughput
REDIS_MAX_CONNECTIONS = int(os.getenv("REDIS_MAX_CONNECTIONS", "50"))

# Timeout Configuration (defense-in-depth)
# socket_connect_timeout: TCP handshake timeout
# socket_timeout: Command execution timeout (read/write)
REDIS_SOCKET_CONNECT_TIMEOUT_SEC = float(
    os.getenv("REDIS_SOCKET_CONNECT_TIMEOUT", "0.5")
)
REDIS_SOCKET_TIMEOUT_SEC = float(os.getenv("REDIS_SOCKET_TIMEOUT", "0.2"))

# Health Check Configuration
# health_check_interval: How often to ping Redis to detect stale connections
REDIS_HEALTH_CHECK_INTERVAL_SEC = int(os.getenv("REDIS_HEALTH_CHECK_INTERVAL", "30"))

# Retry Configuration
# retry_on_timeout defaults to false to avoid implicit client retries that can
# blur timeout semantics ("applied but timed out") and inflate tail latency.
REDIS_RETRY_ON_TIMEOUT = os.getenv("REDIS_RETRY_ON_TIMEOUT", "False").lower() == "true"

# Reliability Configuration
DEDUPE_WINDOW_SECONDS = int(
    os.getenv("DEDUPE_WINDOW_SECONDS", "604800")
)  # 7 days (matches Kafka retention)
MAX_RETRIES = int(
    os.getenv("MAX_RETRIES", "3")
)  # Max retry attempts for transient failures
DLQ_DEDUPE_TTL_SECONDS = int(
    os.getenv("DLQ_DEDUPE_TTL_SECONDS", str(DEDUPE_WINDOW_SECONDS))
)  # Keep DLQ dedupe key for the same retention window

if MAX_RETRIES > len(KAFKA_RETRY_TIERS):
    raise RuntimeError(
        f"MAX_RETRIES={MAX_RETRIES} exceeds configured retry tiers ({len(KAFKA_RETRY_TIERS)}). "
        "Add another retry topic tier or lower MAX_RETRIES."
    )

# Retry delay enforcement (retry consumer only).
# Safe default: retry tier names must correspond to elapsed delay in all standard
# deployments. Immediate retry processing is allowed only as an explicit local/dev
# override because it can exhaust all retry tiers almost instantly.
RETRY_ENFORCE_DELAY = os.getenv("RETRY_ENFORCE_DELAY", "true").lower() in (
    "1",
    "true",
    "yes",
)
ALLOW_UNSAFE_IMMEDIATE_RETRY = os.getenv(
    "ALLOW_UNSAFE_IMMEDIATE_RETRY", "false"
).lower() in (
    "1",
    "true",
    "yes",
)

if (
    CONSUMER_MODE == "retry"
    and not RETRY_ENFORCE_DELAY
    and not ALLOW_UNSAFE_IMMEDIATE_RETRY
):
    raise RuntimeError(
        "Retry consumer cannot run with RETRY_ENFORCE_DELAY=false unless "
        "ALLOW_UNSAFE_IMMEDIATE_RETRY=true is also set. "
        "Immediate retry processing burns through retry tiers and DLQ almost instantly."
    )

# Processing Configuration
RECENT_CLICKS_MAX = int(os.getenv("RECENT_CLICKS_MAX", "100"))
SESSION_EXPIRE_SECONDS = int(os.getenv("SESSION_EXPIRE_SECONDS", "1800"))
LOG_INTERVAL = int(os.getenv("LOG_INTERVAL", "100"))

# Online feature decay configuration
#
# The consumer uses half-life based exponential decay:
#   decayed_score = previous_score * exp(-lambda * elapsed_seconds)
#   lambda = ln(2) / half_life_seconds
#
# Each feature stores both:
# - the decayed score as of the last write
# - the last update timestamp
#
# This is real time decay. Whole-key TTL is used only as optional state cleanup
# for user/session/item keys and is NOT the decay mechanism.
_LN2 = math.log(2.0)

CATEGORY_AFFINITY_HALF_LIFE_DAYS = float(
    os.getenv(
        "CATEGORY_AFFINITY_HALF_LIFE_DAYS",
        os.getenv("AFFINITY_DECAY_DAYS", "7"),
    )
)
ITEM_CLICK_HALF_LIFE_HOURS = float(os.getenv("ITEM_CLICK_HALF_LIFE_HOURS", "72"))
RECENT_ITEM_CLICK_HALF_LIFE_HOURS = float(
    os.getenv("RECENT_ITEM_CLICK_HALF_LIFE_HOURS", "24")
)
GLOBAL_POPULARITY_HALF_LIFE_HOURS = float(
    os.getenv("GLOBAL_POPULARITY_HALF_LIFE_HOURS", "48")
)
ONLINE_FEATURE_STATE_TTL_SECONDS = int(
    os.getenv("ONLINE_FEATURE_STATE_TTL_SECONDS", str(90 * 86400))
)

CATEGORY_AFFINITY_DECAY_LAMBDA = _LN2 / (CATEGORY_AFFINITY_HALF_LIFE_DAYS * 86400.0)
ITEM_CLICK_DECAY_LAMBDA = _LN2 / (ITEM_CLICK_HALF_LIFE_HOURS * 3600.0)
RECENT_ITEM_CLICK_DECAY_LAMBDA = _LN2 / (RECENT_ITEM_CLICK_HALF_LIFE_HOURS * 3600.0)
GLOBAL_POPULARITY_DECAY_LAMBDA = _LN2 / (GLOBAL_POPULARITY_HALF_LIFE_HOURS * 3600.0)

# Windowed popularity configuration
# POPULARITY_BUCKET_PREFIX: Redis key prefix for time-windowed popularity buckets.
#   MUST match inference-service RedisConfig.POPULARITY_BUCKET_PREFIX to ensure
#   the consumer writes to keys that the inference service reads from.
#   Default: "popularity:bucket" (matches inference service default)
POPULARITY_BUCKET_PREFIX = os.getenv("POPULARITY_BUCKET_PREFIX", "popularity:bucket")
POPULARITY_1H_BUCKET_SECONDS = int(
    os.getenv("POPULARITY_1H_BUCKET_SECONDS", "300")
)  # 5 min buckets
POPULARITY_24H_BUCKET_SECONDS = int(
    os.getenv("POPULARITY_24H_BUCKET_SECONDS", "3600")
)  # 1 hour buckets
POPULARITY_7D_BUCKET_SECONDS = int(
    os.getenv("POPULARITY_7D_BUCKET_SECONDS", "86400")
)  # 1 day buckets

POPULARITY_1H_BUCKET_TTL_SECONDS = int(
    os.getenv("POPULARITY_1H_BUCKET_TTL_SECONDS", "7200")
)
POPULARITY_24H_BUCKET_TTL_SECONDS = int(
    os.getenv("POPULARITY_24H_BUCKET_TTL_SECONDS", "172800")
)
POPULARITY_7D_BUCKET_TTL_SECONDS = int(
    os.getenv("POPULARITY_7D_BUCKET_TTL_SECONDS", str(14 * 86400))
)

# Observability Configuration
METRICS_PORT = int(os.getenv("METRICS_PORT", "8000"))  # HTTP metrics endpoint port

# Category LRU cache (eliminates per-event Redis round trip for item metadata)
#
# CATEGORY_CACHE_MAX_SIZE: Maximum number of item→category entries held in the
#   in-process LRU cache.  At 200 bytes per entry (item_id string + category
#   string + overhead), 10 000 entries ≈ 2 MiB — negligible for the consumer pod.
#   Raise if category_cache_ops_total{status="miss"} stays high at steady state.
#
# CATEGORY_CACHE_TTL_SECONDS: How long a cached entry is valid.  Item categories
#   are stable (products don't change category after ingestion), so 1 hour is
#   conservative.  "unknown" entries (items with no metadata in Redis) are cached
#   for the same TTL to prevent Redis hammering on cold or missing items.
CATEGORY_CACHE_MAX_SIZE = int(os.getenv("CATEGORY_CACHE_MAX_SIZE", "10000"))
CATEGORY_CACHE_TTL_SECONDS = int(os.getenv("CATEGORY_CACHE_TTL_SECONDS", "3600"))
