"""
MetricsRecorder — thin façade over the raw Prometheus metric objects in metrics.py.

All callers use these methods instead of calling .inc() / .observe() directly on
metric objects.  This centralises the label-set contracts in one place and makes
the EventConsumer (and its sub-components) testable without Prometheus imports.

The constructor accepts the metric objects so that metrics.py remains the single
owner of metric creation.  Callers pass in the objects at construction time; this
module never imports metrics itself.
"""

from __future__ import annotations

import logging
from typing import Any

logger = logging.getLogger(__name__)


class MetricsRecorder:
    """Wraps raw Prometheus metric objects with named, typed helper methods.

    Parameters mirror the public names in metrics.py.  All arguments are optional
    (default to None) so that tests can create a MetricsRecorder with only the
    metrics they need to verify, and a NullMetricsRecorder can be created for
    contexts where no metrics backend is available.
    """

    def __init__(
        self,
        *,
        events_processed_total=None,
        events_failed_total=None,
        events_retry_routed_total=None,
        events_retry_tier_routed_total=None,
        events_dlq_sent_total=None,
        events_duplicate_total=None,
        events_commit_failed_total=None,
        consumer_terminations_total=None,
        feature_failures_total=None,
        redis_operations_total=None,
        kafka_produce_total=None,
        event_processing_latency_seconds=None,
        redis_update_latency_seconds=None,
        retry_delay_seconds=None,
        consumer_health=None,
        redis_available=None,
        retry_partitions_paused=None,
        category_cache_ops_total=None,
        category_cache_size=None,
        record_redis_success_fn=None,
        record_redis_error_fn=None,
    ) -> None:
        self._events_processed_total = events_processed_total
        self._events_failed_total = events_failed_total
        self._events_retry_routed_total = events_retry_routed_total
        self._events_retry_tier_routed_total = events_retry_tier_routed_total
        self._events_dlq_sent_total = events_dlq_sent_total
        self._events_duplicate_total = events_duplicate_total
        self._events_commit_failed_total = events_commit_failed_total
        self._consumer_terminations_total = consumer_terminations_total
        self._feature_failures_total = feature_failures_total
        self._redis_operations_total = redis_operations_total
        self._kafka_produce_total = kafka_produce_total
        self._event_processing_latency_seconds = event_processing_latency_seconds
        self._redis_update_latency_seconds = redis_update_latency_seconds
        self._retry_delay_seconds = retry_delay_seconds
        self._consumer_health = consumer_health
        self._redis_available = redis_available
        self._retry_partitions_paused = retry_partitions_paused
        self._category_cache_ops_total = category_cache_ops_total
        self._category_cache_size = category_cache_size
        self._record_redis_success_fn = record_redis_success_fn
        self._record_redis_error_fn = record_redis_error_fn

    # ------------------------------------------------------------------
    # Redis streak helpers (delegate to module-level functions)
    # ------------------------------------------------------------------

    def record_redis_success(self) -> None:
        if self._record_redis_success_fn is not None:
            self._record_redis_success_fn()

    def record_redis_error(self) -> None:
        if self._record_redis_error_fn is not None:
            self._record_redis_error_fn()

    # ------------------------------------------------------------------
    # Event processing outcomes
    # ------------------------------------------------------------------

    def record_event_applied(self, latency_s: float) -> None:
        """Record a successfully applied event."""
        if self._events_processed_total is not None:
            self._events_processed_total.labels(result="applied").inc()
        if self._event_processing_latency_seconds is not None:
            self._event_processing_latency_seconds.observe(latency_s)

    def record_event_duplicate(self) -> None:
        """Record a duplicate event (idempotent skip)."""
        if self._events_duplicate_total is not None:
            self._events_duplicate_total.inc()
        if self._events_processed_total is not None:
            self._events_processed_total.labels(result="duplicate").inc()

    def record_event_permanent_failure(self) -> None:
        """Record a permanent processing failure."""
        if self._events_failed_total is not None:
            self._events_failed_total.labels(failure_type="permanent").inc()

    def record_event_transient_failure(self) -> None:
        """Record a transient processing failure."""
        if self._events_failed_total is not None:
            self._events_failed_total.labels(failure_type="transient").inc()

    def record_event_dlq_failed(self) -> None:
        """Record that a DLQ send attempt failed."""
        if self._events_failed_total is not None:
            self._events_failed_total.labels(failure_type="dlq_failed").inc()

    # ------------------------------------------------------------------
    # Redis operation outcomes
    # ------------------------------------------------------------------

    def record_redis_op(self, operation: str, status: str) -> None:
        """Record any Redis operation outcome."""
        if self._redis_operations_total is not None:
            self._redis_operations_total.labels(
                operation=operation, status=status
            ).inc()

    def record_redis_update_latency(self, latency_s: float) -> None:
        """Record Redis Lua script execution latency."""
        if self._redis_update_latency_seconds is not None:
            self._redis_update_latency_seconds.observe(latency_s)

    # ------------------------------------------------------------------
    # Retry / DLQ routing
    # ------------------------------------------------------------------

    def record_retry_routed(self, retry_count: int, retry_tier: str) -> None:
        """Record a message routed to a retry tier."""
        if self._events_retry_routed_total is not None:
            self._events_retry_routed_total.labels(retry_count=str(retry_count)).inc()
        if self._events_retry_tier_routed_total is not None:
            self._events_retry_tier_routed_total.labels(
                retry_count=str(retry_count), retry_tier=retry_tier
            ).inc()

    def record_kafka_produce(self, topic: str, status: str) -> None:
        """Record a Kafka produce operation."""
        if self._kafka_produce_total is not None:
            self._kafka_produce_total.labels(topic=topic, status=status).inc()

    def record_dlq_sent(self, reason: str) -> None:
        """Record a message dispatched to the DLQ."""
        if self._events_dlq_sent_total is not None:
            self._events_dlq_sent_total.labels(reason=reason).inc()

    def record_commit_failed(self, reason: str) -> None:
        """Record an offset commit failure after a downstream write succeeded."""
        if self._events_commit_failed_total is not None:
            self._events_commit_failed_total.labels(reason=reason).inc()

    # ------------------------------------------------------------------
    # Retry delay (partition pause/resume)
    # ------------------------------------------------------------------

    def record_retry_delay(self, delay_s: float) -> None:
        """Record how long a retry message actually waited."""
        if self._retry_delay_seconds is not None:
            self._retry_delay_seconds.observe(delay_s)

    def set_paused_partitions(self, count: int) -> None:
        """Update the paused partitions gauge."""
        if self._retry_partitions_paused is not None:
            self._retry_partitions_paused.set(count)

    # ------------------------------------------------------------------
    # Category cache
    # ------------------------------------------------------------------

    def record_category_cache_op(self, status: str) -> None:
        """Record a category LRU cache operation (hit / miss / expired)."""
        if self._category_cache_ops_total is not None:
            self._category_cache_ops_total.labels(status=status).inc()

    def set_category_cache_size(self, size: int) -> None:
        """Update the category cache size gauge."""
        if self._category_cache_size is not None:
            self._category_cache_size.set(size)

    def record_category_affinity_failure(self) -> None:
        """Record that category affinity could not be determined (unknown)."""
        if self._feature_failures_total is not None:
            self._feature_failures_total.labels(feature="category_affinity").inc()

    # ------------------------------------------------------------------
    # Consumer lifecycle
    # ------------------------------------------------------------------

    def set_consumer_health(self, value: int) -> None:
        """Set consumer health gauge (1=healthy, 0=unhealthy)."""
        if self._consumer_health is not None:
            self._consumer_health.set(value)

    def record_termination(self, reason: str) -> None:
        """Record a consumer process termination."""
        if self._consumer_terminations_total is not None:
            self._consumer_terminations_total.labels(reason=reason).inc()


def build_from_metrics_module(metrics_module: Any) -> "MetricsRecorder":
    """Build a fully-wired MetricsRecorder from the metrics.py module object.

    Centralises the wiring so that EventConsumer.__init__ has one call site.
    """
    return MetricsRecorder(
        events_processed_total=getattr(metrics_module, "events_processed_total", None),
        events_failed_total=getattr(metrics_module, "events_failed_total", None),
        events_retry_routed_total=getattr(
            metrics_module, "events_retry_routed_total", None
        ),
        events_retry_tier_routed_total=getattr(
            metrics_module, "events_retry_tier_routed_total", None
        ),
        events_dlq_sent_total=getattr(metrics_module, "events_dlq_sent_total", None),
        events_duplicate_total=getattr(metrics_module, "events_duplicate_total", None),
        events_commit_failed_total=getattr(
            metrics_module, "events_commit_failed_total", None
        ),
        consumer_terminations_total=getattr(
            metrics_module, "consumer_terminations_total", None
        ),
        feature_failures_total=getattr(metrics_module, "feature_failures_total", None),
        redis_operations_total=getattr(metrics_module, "redis_operations_total", None),
        kafka_produce_total=getattr(metrics_module, "kafka_produce_total", None),
        event_processing_latency_seconds=getattr(
            metrics_module, "event_processing_latency_seconds", None
        ),
        redis_update_latency_seconds=getattr(
            metrics_module, "redis_update_latency_seconds", None
        ),
        retry_delay_seconds=getattr(metrics_module, "retry_delay_seconds", None),
        consumer_health=getattr(metrics_module, "consumer_health", None),
        redis_available=getattr(metrics_module, "redis_available", None),
        retry_partitions_paused=getattr(
            metrics_module, "retry_partitions_paused", None
        ),
        category_cache_ops_total=getattr(
            metrics_module, "category_cache_ops_total", None
        ),
        category_cache_size=getattr(metrics_module, "category_cache_size", None),
        record_redis_success_fn=getattr(metrics_module, "record_redis_success", None),
        record_redis_error_fn=getattr(metrics_module, "record_redis_error", None),
    )
