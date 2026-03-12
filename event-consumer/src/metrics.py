"""Production-grade Metrics for Event Consumer using Prometheus native instrumentation

Exposes HTTP endpoints for observability and Kubernetes probes:
- GET /metrics  — Prometheus exposition format
- GET /live     — Kubernetes livenessProbe: in-process health only (loop alive, poll freshness)
- GET /ready    — Kubernetes readinessProbe: full dependency health (Kafka, Redis, assignment)
- GET /health   — Backward-compat alias for /ready

Liveness vs readiness split rationale
--------------------------------------
Liveness (/live) checks only in-process state.  It must never touch external
dependencies, because a transient Kafka rebalance or Redis blip should NOT
trigger a pod restart.  Unnecessary restarts amplify rebalances into storms.

Readiness (/ready) checks whether the consumer can actually process traffic.
A NotReady pod is excluded from rolling-deployment gates and PodDisruptionBudget
windows, but is NOT restarted.  Failing readiness during a rebalance is safe.

Redis readiness design choice (see RUNBOOK.md)
---------------------------------------------
Redis failures do NOT flip readiness.  The consumer stays Ready and relies on
retry/DLQ for Redis errors.  Operators must use redis_consecutive_errors,
redis_unavailable_duration_seconds, and rate(events_retry_routed_total) to
detect "ready but all traffic in retry" scenarios.

Metrics exposed:
- Counters: events_processed_total, events_failed_total, events_retry_routed_total, etc.
- Histograms: event_processing_latency_seconds, redis_update_latency_seconds
- Gauges: consumer_health, redis_available, redis_consecutive_errors, redis_unavailable_duration_seconds
"""

import json
import logging
import threading
import time
from http.server import BaseHTTPRequestHandler, HTTPServer
from typing import Callable, Optional

from prometheus_client import (
    Counter,
    Gauge,
    Histogram,
    CONTENT_TYPE_LATEST,
    REGISTRY,
    generate_latest,
)

logger = logging.getLogger(__name__)

# ==========================================
# Counter Metrics
# ==========================================

# Event processing outcomes
events_processed_total = Counter(
    "events_processed_total",
    "Total events successfully processed and committed",
    ["result"],  # labels: applied, duplicate
)

events_failed_total = Counter(
    "events_failed_total",
    "Total events that failed processing",
    ["failure_type"],  # labels: transient, permanent, validation
)

events_retry_routed_total = Counter(
    "events_retry_routed_total",
    "Total events routed to retry topic due to transient failures",
    ["retry_count"],  # labels: "1", "2", "3", etc.
)

events_retry_tier_routed_total = Counter(
    "events_retry_tier_routed_total",
    "Total events routed to each retry tier",
    ["retry_count", "retry_tier"],
)

events_dlq_sent_total = Counter(
    "events_dlq_sent_total",
    "Total events sent to Dead Letter Queue",
    ["reason"],  # labels: max_retries_exceeded, permanent_failure, validation_error
)

events_duplicate_total = Counter(
    "events_duplicate_total",
    "Total duplicate events skipped via idempotency check",
)

events_commit_failed_total = Counter(
    "events_commit_failed_total",
    "Total Kafka offset commit failures after a downstream write already succeeded",
    # labels:
    #   retry_routed_terminating — retry was published then commit failed; process terminates
    #   dlq_send_success         — DLQ was written then commit failed; process terminates
    ["reason"],
)

consumer_terminations_total = Counter(
    "consumer_terminations_total",
    "Total event-consumer process terminations by terminal reason",
    ["reason"],  # labels: downstream_commit_uncertain, fatal_loop_error
)

# Feature-level metrics (failures only - success tracked via events_processed_total)
feature_failures_total = Counter(
    "feature_update_failures_total",
    "Total feature update failures by feature type",
    ["feature"],
)

# Redis operations
redis_operations_total = Counter(
    "redis_operations_total",
    "Total Redis operations",
    ["operation", "status"],  # operation: lua_decay_upsert, category_lookup, dlq_mark; status: success, error
)

# Kafka operations
kafka_produce_total = Counter(
    "kafka_produce_total",
    "Total Kafka produce operations",
    ["topic", "status"],  # topic: retry, dlq; status: success, error
)

# ==========================================
# Histogram Metrics
# ==========================================

# Event processing latency (end-to-end)
event_processing_latency_seconds = Histogram(
    "event_processing_latency_seconds",
    "Event processing latency from consume to commit",
    buckets=(0.001, 0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1.0, 2.5, 5.0, 10.0),
)

# Redis update latency
redis_update_latency_seconds = Histogram(
    "redis_update_latency_seconds",
    "Redis Lua script execution latency",
    buckets=(0.0001, 0.0005, 0.001, 0.0025, 0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5),
)

# Retry delay (how long message waited before retry)
retry_delay_seconds = Histogram(
    "retry_delay_seconds",
    "Actual delay between failure and retry consumption",
    buckets=(0.1, 0.5, 1.0, 2.0, 5.0, 10.0, 30.0, 60.0, 120.0, 300.0),
)

# ==========================================
# Gauge Metrics
# ==========================================

# Consumer health (1 = healthy, 0 = unhealthy)
consumer_health = Gauge(
    "consumer_health",
    "Consumer health status (1 = healthy, 0 = unhealthy)",
)

# Redis availability (1 = available, 0 = unavailable)
redis_available = Gauge(
    "redis_available",
    "Redis availability status (1 = available, 0 = unavailable)",
)

# Redis error streak (for "ready but traffic in retry" detection)
# Updated by record_redis_success/record_redis_error.  Used with alerting when
# Redis is intentionally excluded from readiness — see RUNBOOK.md.
redis_consecutive_errors = Gauge(
    "redis_consecutive_errors",
    "Consecutive Redis errors (reset on first success). Use with alerts when Redis is excluded from readiness.",
)

redis_unavailable_duration_seconds = Gauge(
    "redis_unavailable_duration_seconds",
    "Seconds since current Redis error streak started (0 if healthy). Use with alerts — see RUNBOOK.md.",
)

# Thread-safe state for Redis error streak
_redis_streak_lock = threading.Lock()
_redis_consecutive_errors_value = 0
_redis_error_streak_started_at: Optional[float] = None


def record_redis_success() -> None:
    """Record a successful Redis operation. Resets consecutive error count and unavailable duration."""
    global _redis_consecutive_errors_value, _redis_error_streak_started_at
    with _redis_streak_lock:
        _redis_consecutive_errors_value = 0
        _redis_error_streak_started_at = None
        redis_consecutive_errors.set(0)
        redis_unavailable_duration_seconds.set(0)
        redis_available.set(1)


def record_redis_error() -> None:
    """Record a Redis error. Increments consecutive count and tracks streak start."""
    global _redis_consecutive_errors_value, _redis_error_streak_started_at
    with _redis_streak_lock:
        if _redis_consecutive_errors_value == 0:
            _redis_error_streak_started_at = time.time()
        _redis_consecutive_errors_value += 1
        redis_consecutive_errors.set(_redis_consecutive_errors_value)
        redis_available.set(0)


def refresh_redis_unavailable_duration() -> None:
    """Update redis_unavailable_duration_seconds. Call from /ready handler or periodically."""
    with _redis_streak_lock:
        if _redis_error_streak_started_at is None:
            redis_unavailable_duration_seconds.set(0)
        else:
            redis_unavailable_duration_seconds.set(time.time() - _redis_error_streak_started_at)


# Kafka consumer lag (updated periodically if available)
kafka_consumer_lag = Gauge(
    "kafka_consumer_lag",
    "Kafka consumer lag (messages behind)",
    ["topic", "partition"],
)

# Consumer uptime
consumer_uptime_seconds = Gauge(
    "consumer_uptime_seconds",
    "Consumer uptime in seconds",
)

# Processing rate (updated periodically)
processing_rate_events_per_sec = Gauge(
    "processing_rate_events_per_sec",
    "Current event processing rate",
)

# Retry partition pause status
retry_partitions_paused = Gauge(
    "retry_partitions_paused",
    "Number of retry partitions currently paused for backoff",
)

# ==========================================
# Category LRU Cache Metrics
# ==========================================

category_cache_ops_total = Counter(
    "category_cache_ops_total",
    "Category LRU cache operations by outcome.  "
    "hit=served from cache; miss=not in cache; expired=was in cache but TTL elapsed.",
    ["status"],  # hit | miss | expired
)

category_cache_size = Gauge(
    "category_cache_size",
    "Current number of entries in the in-process category LRU cache.  "
    "Compare with CATEGORY_CACHE_MAX_SIZE to detect eviction pressure.",
)


class MetricsServer:
    """
    Metrics and Kubernetes probe server.

    Serves four endpoints on a single port:
    - GET /metrics  — Prometheus exposition format (unchanged)
    - GET /live     — Kubernetes livenessProbe  (in-process state only)
    - GET /ready    — Kubernetes readinessProbe (dependency-sensitive state)
    - GET /health   — Backward-compat alias for /ready

    Each probe endpoint delegates to an injected callable so the consumer can
    report its own state without creating a circular import dependency.

    Probe semantics
    ---------------
    /live  checks only whether the process main loop is still running and has
           polled recently enough.  It never touches Kafka or Redis.  A 503
           here should trigger a pod restart.

    /ready checks full processing readiness including Kafka partition assignment
           and poll freshness.  A 503 here marks the pod NotReady (excluded from
           rolling-deployment gates) but does NOT restart it.  This is the safe
           failure mode for Kafka rebalances and transient dependency outages.
    """

    def __init__(self, port: int = 8000):
        self.port = port
        self.start_time: Optional[float] = None
        self._liveness_check_fn: Optional[Callable[[], dict]] = None
        self._readiness_check_fn: Optional[Callable[[], dict]] = None
        self._server: Optional[HTTPServer] = None

    def set_liveness_check(self, fn: Callable[[], dict]) -> None:
        """Inject liveness callable: fn() → {'live': bool, 'checks': {...}}

        Must only inspect in-process state (loop flags, timestamps).
        Must NOT call Kafka, Redis, or any external dependency.
        """
        self._liveness_check_fn = fn

    def set_readiness_check(self, fn: Callable[[], dict]) -> None:
        """Inject readiness callable: fn() → {'healthy': bool, 'checks': {...}}"""
        self._readiness_check_fn = fn

    def set_health_check(self, fn: Callable[[], dict]) -> None:
        """Backward-compat alias for set_readiness_check."""
        self._readiness_check_fn = fn

    def start(self) -> None:
        import time

        self.start_time = time.time()
        consumer_health.set(1)
        # redis_available/consecutive_errors are set by record_redis_success/error

        liveness_fn = self._liveness_check_fn
        readiness_fn = self._readiness_check_fn

        class _Handler(BaseHTTPRequestHandler):
            def do_GET(self):
                if self.path == "/metrics":
                    output = generate_latest(REGISTRY)
                    self.send_response(200)
                    self.send_header("Content-Type", CONTENT_TYPE_LATEST)
                    self.end_headers()
                    self.wfile.write(output)

                elif self.path == "/live":
                    # Kubernetes livenessProbe — in-process checks only.
                    # 503 here causes a pod restart; must be conservative.
                    if liveness_fn is not None:
                        result = liveness_fn()
                    else:
                        result = {"live": True, "checks": {}}
                    status = 200 if result.get("live") else 503
                    body = json.dumps(result).encode()
                    self.send_response(status)
                    self.send_header("Content-Type", "application/json")
                    self.end_headers()
                    self.wfile.write(body)

                elif self.path in ("/ready", "/health"):
                    # Kubernetes readinessProbe (/ready) and backward-compat /health.
                    # 503 marks pod NotReady — safe for transient dependency failures.
                    if readiness_fn is not None:
                        result = readiness_fn()
                    else:
                        result = {"healthy": True, "checks": {}}
                    status = 200 if result.get("healthy") else 503
                    body = json.dumps(result).encode()
                    self.send_response(status)
                    self.send_header("Content-Type", "application/json")
                    self.end_headers()
                    self.wfile.write(body)

                else:
                    self.send_response(404)
                    self.end_headers()

            def log_message(self, fmt, *args):  # suppress access log noise
                pass

        self._server = HTTPServer(("", self.port), _Handler)
        thread = threading.Thread(target=self._server.serve_forever, daemon=True)
        thread.start()
        logger.info(f"[SUCCESS] Metrics server started on port {self.port}")
        logger.info(f"[INFO] Prometheus:  http://0.0.0.0:{self.port}/metrics")
        logger.info(f"[INFO] Liveness:    http://0.0.0.0:{self.port}/live")
        logger.info(f"[INFO] Readiness:   http://0.0.0.0:{self.port}/ready")
        logger.info(f"[INFO] Health(compat): http://0.0.0.0:{self.port}/health")

    def update_uptime(self) -> None:
        import time

        if self.start_time:
            consumer_uptime_seconds.set(time.time() - self.start_time)

    def set_processing_rate(self, rate: float) -> None:
        processing_rate_events_per_sec.set(rate)

    def stop(self) -> None:
        consumer_health.set(0)
        if self._server:
            self._server.shutdown()
        logger.info("[SHUTDOWN] Metrics server stopped (health set to 0)")
