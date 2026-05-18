"""Tests for MetricsRecorder."""
import os
import sys
from pathlib import Path
from unittest.mock import MagicMock

import pytest

os.environ.setdefault("KAFKA_BOOTSTRAP_SERVERS", "test-kafka:9093")
os.environ.setdefault("CONSUMER_MODE", "primary")

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

from metrics_recorder import MetricsRecorder, build_from_metrics_module


def _make_counter():
    m = MagicMock()
    m.labels.return_value = m
    return m


def _make_histogram():
    m = MagicMock()
    return m


# ---------------------------------------------------------------------------
# Counters / labels
# ---------------------------------------------------------------------------


def test_record_event_applied_increments_processed_and_latency():
    proc = _make_counter()
    lat = _make_histogram()
    rec = MetricsRecorder(events_processed_total=proc, event_processing_latency_seconds=lat)
    rec.record_event_applied(0.042)
    proc.labels.assert_called_once_with(result="applied")
    proc.labels().inc.assert_called_once()
    lat.observe.assert_called_once_with(0.042)


def test_record_event_duplicate_increments_both_counters():
    dup = _make_counter()
    proc = _make_counter()
    rec = MetricsRecorder(events_duplicate_total=dup, events_processed_total=proc)
    rec.record_event_duplicate()
    dup.inc.assert_called_once()
    proc.labels.assert_called_once_with(result="duplicate")
    proc.labels().inc.assert_called_once()


def test_record_event_permanent_failure_uses_correct_label():
    failed = _make_counter()
    rec = MetricsRecorder(events_failed_total=failed)
    rec.record_event_permanent_failure()
    failed.labels.assert_called_once_with(failure_type="permanent")
    failed.labels().inc.assert_called_once()


def test_record_event_transient_failure_uses_correct_label():
    failed = _make_counter()
    rec = MetricsRecorder(events_failed_total=failed)
    rec.record_event_transient_failure()
    failed.labels.assert_called_once_with(failure_type="transient")


def test_record_event_dlq_failed_uses_correct_label():
    failed = _make_counter()
    rec = MetricsRecorder(events_failed_total=failed)
    rec.record_event_dlq_failed()
    failed.labels.assert_called_once_with(failure_type="dlq_failed")


def test_record_retry_routed_increments_both_tier_counters():
    retry_total = _make_counter()
    tier_total = _make_counter()
    rec = MetricsRecorder(
        events_retry_routed_total=retry_total,
        events_retry_tier_routed_total=tier_total,
    )
    rec.record_retry_routed(retry_count=2, retry_tier="retry_10s")
    retry_total.labels.assert_called_once_with(retry_count="2")
    tier_total.labels.assert_called_once_with(retry_count="2", retry_tier="retry_10s")


def test_record_dlq_sent_uses_reason_label():
    dlq = _make_counter()
    rec = MetricsRecorder(events_dlq_sent_total=dlq)
    rec.record_dlq_sent("max_retries_exceeded")
    dlq.labels.assert_called_once_with(reason="max_retries_exceeded")
    dlq.labels().inc.assert_called_once()


def test_record_kafka_produce_uses_topic_and_status():
    produce = _make_counter()
    rec = MetricsRecorder(kafka_produce_total=produce)
    rec.record_kafka_produce("dlq", "success")
    produce.labels.assert_called_once_with(topic="dlq", status="success")
    produce.labels().inc.assert_called_once()


def test_record_commit_failed_uses_reason_label():
    commit_failed = _make_counter()
    rec = MetricsRecorder(events_commit_failed_total=commit_failed)
    rec.record_commit_failed("retry_routed_terminating")
    commit_failed.labels.assert_called_once_with(reason="retry_routed_terminating")


def test_record_redis_update_latency_observes_value():
    hist = _make_histogram()
    rec = MetricsRecorder(redis_update_latency_seconds=hist)
    rec.record_redis_update_latency(0.005)
    hist.observe.assert_called_once_with(0.005)


def test_record_retry_delay_observes_value():
    hist = _make_histogram()
    rec = MetricsRecorder(retry_delay_seconds=hist)
    rec.record_retry_delay(3.5)
    hist.observe.assert_called_once_with(3.5)


def test_category_cache_op_increments_with_status():
    cache_ops = _make_counter()
    rec = MetricsRecorder(category_cache_ops_total=cache_ops)
    rec.record_category_cache_op("hit")
    cache_ops.labels.assert_called_once_with(status="hit")
    cache_ops.labels().inc.assert_called_once()


def test_set_category_cache_size_sets_gauge():
    size_gauge = MagicMock()
    rec = MetricsRecorder(category_cache_size=size_gauge)
    rec.set_category_cache_size(42)
    size_gauge.set.assert_called_once_with(42)


def test_set_paused_partitions_sets_gauge():
    gauge = MagicMock()
    rec = MetricsRecorder(retry_partitions_paused=gauge)
    rec.set_paused_partitions(3)
    gauge.set.assert_called_once_with(3)


def test_record_redis_success_calls_fn():
    success_fn = MagicMock()
    rec = MetricsRecorder(record_redis_success_fn=success_fn)
    rec.record_redis_success()
    success_fn.assert_called_once()


def test_record_redis_error_calls_fn():
    error_fn = MagicMock()
    rec = MetricsRecorder(record_redis_error_fn=error_fn)
    rec.record_redis_error()
    error_fn.assert_called_once()


def test_noop_when_metric_is_none():
    """MetricsRecorder with no metrics set must not raise."""
    rec = MetricsRecorder()
    rec.record_event_applied(0.1)
    rec.record_event_duplicate()
    rec.record_event_permanent_failure()
    rec.record_event_transient_failure()
    rec.record_retry_routed(1, "retry_1s")
    rec.record_dlq_sent("permanent_failure")
    rec.record_redis_success()
    rec.record_redis_error()


def test_build_from_metrics_module_wires_all_metrics():
    """build_from_metrics_module returns a properly wired MetricsRecorder."""
    import types

    mod = types.SimpleNamespace(
        events_processed_total=_make_counter(),
        events_failed_total=_make_counter(),
        events_retry_routed_total=_make_counter(),
        events_retry_tier_routed_total=_make_counter(),
        events_dlq_sent_total=_make_counter(),
        events_duplicate_total=_make_counter(),
        events_commit_failed_total=_make_counter(),
        consumer_terminations_total=_make_counter(),
        feature_failures_total=_make_counter(),
        redis_operations_total=_make_counter(),
        kafka_produce_total=_make_counter(),
        event_processing_latency_seconds=_make_histogram(),
        redis_update_latency_seconds=_make_histogram(),
        retry_delay_seconds=_make_histogram(),
        consumer_health=MagicMock(),
        redis_available=MagicMock(),
        retry_partitions_paused=MagicMock(),
        category_cache_ops_total=_make_counter(),
        category_cache_size=MagicMock(),
        record_redis_success=MagicMock(),
        record_redis_error=MagicMock(),
    )
    rec = build_from_metrics_module(mod)
    rec.record_event_applied(0.01)
    mod.events_processed_total.labels.assert_called()
    rec.record_redis_success()
    mod.record_redis_success.assert_called_once()
