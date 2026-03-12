"""
Personalization feature-read metrics.

Defines a single Counter that tracks every Redis read attempt in FeatureReader,
labelled by feature name and outcome.  This makes degradation a first-class
observable signal instead of a silent log message.

Label schema
------------
feature : str
    One of: recent_clicks | category_affinity | item_category |
            item_categories_batch | session_clicks | popularity_signals
status : str
    success          — data returned normally (may be empty key / key miss)
    timeout          — redis.TimeoutError within the 10 ms socket budget
    connection_error — redis.ConnectionError (pool exhausted, network blip)
    error            — any other unexpected exception

Example PromQL queries
----------------------
# Degradation rate over last 5 min across all features:
rate(personalization_feature_read_total{status!="success"}[5m])

# Fraction of rerank requests that lost recent-clicks due to Redis failures:
rate(personalization_feature_read_total{feature="recent_clicks",status!="success"}[5m])
/ rate(personalization_feature_read_total{feature="recent_clicks"}[5m])
"""

from src.utils.metrics import counter, gauge, histogram

feature_read_total = counter(
    "personalization_feature_read_total",
    "Total Redis read attempts in FeatureReader, labelled by feature and outcome",
    ["feature", "status"],
)

feature_read_latency_seconds = histogram(
    "personalization_feature_read_latency_seconds",
    "Latency of Redis feature reads in FeatureReader",
    ["feature"],
    buckets=(0.001, 0.0025, 0.005, 0.01, 0.02, 0.05, 0.1),
)

snapshot_load_total = counter(
    "personalization_snapshot_load_total",
    "Total personalization snapshot load attempts by outcome",
    ["status"],  # success | degraded | partial
)

snapshot_load_latency_seconds = histogram(
    "personalization_snapshot_load_latency_seconds",
    "Latency of request-scoped personalization snapshot loads",
    [],
    buckets=(0.001, 0.0025, 0.005, 0.01, 0.02, 0.05, 0.1, 0.25),
)

snapshot_materialization_latency_seconds = histogram(
    "personalization_snapshot_materialization_latency_seconds",
    "Latency of rolling popularity window materialization during snapshot load",
    [],
    buckets=(0.001, 0.0025, 0.005, 0.01, 0.02, 0.05, 0.1, 0.25),
)

snapshot_materialization_total = counter(
    "personalization_snapshot_materialization_total",
    "Popularity window materialization outcomes during snapshot load",
    ["outcome"],  # local_cache_hit | redis_ttl_hit | rebuilt
)

snapshot_redis_round_trips = histogram(
    "personalization_snapshot_redis_round_trips",
    "Redis round trips per personalization snapshot load",
    [],
    buckets=(0, 1, 2, 3, 4, 5, 6),
)

snapshot_degraded_total = counter(
    "personalization_snapshot_degraded_total",
    "Total degraded personalization snapshot loads by reason",
    ["reason"],
)

personalization_fallback_total = counter(
    "personalization_fallback_total",
    "Total times optional personalization fell back to NullFeatureReader",
    ["reason"],
)

personalization_fallback_active = gauge(
    "personalization_fallback_active",
    "Whether this replica is currently using NullFeatureReader (1=yes, 0=no)",
)

personalization_request_mode_total = counter(
    "personalization_request_mode_total",
    "Requests by personalization mode",
    [
        "mode"
    ],  # normal | disabled | degraded_init_fallback | degraded_runtime_boost_failure
)
