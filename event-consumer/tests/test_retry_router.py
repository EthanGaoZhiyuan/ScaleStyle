"""Tests for RetryRouter."""
import os
import sys
import types
from pathlib import Path
from unittest.mock import MagicMock, Mock, patch

import pytest

os.environ.setdefault("KAFKA_BOOTSTRAP_SERVERS", "test-kafka:9093")
os.environ.setdefault("CONSUMER_MODE", "primary")

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

# Stub kafka if not installed
try:
    import kafka  # noqa: F401
except Exception:
    kafka_stub = types.ModuleType("kafka")

    class _TopicPartition:
        def __init__(self, topic, partition):
            self.topic = topic
            self.partition = partition

        def __eq__(self, other):
            return isinstance(other, _TopicPartition) and self.topic == other.topic and self.partition == other.partition

        def __hash__(self):
            return hash((self.topic, self.partition))

    class _OffsetAndMetadata:
        def __init__(self, offset, metadata, leader_epoch=None):
            self.offset = offset

    kafka_stub.TopicPartition = _TopicPartition
    kafka_stub.OffsetAndMetadata = _OffsetAndMetadata
    sys.modules["kafka"] = kafka_stub

from models import ProcessingResult
from retry_router import (
    RetryRouter,
    RetryPublishedCommitFailedError,
    DlqPublishedCommitFailedError,
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def _make_metrics_mock():
    m = MagicMock()
    m.events_processed_total.labels.return_value = m.events_processed_total
    m.events_failed_total.labels.return_value = m.events_failed_total
    m.events_retry_routed_total.labels.return_value = m.events_retry_routed_total
    m.events_retry_tier_routed_total.labels.return_value = (
        m.events_retry_tier_routed_total
    )
    m.events_dlq_sent_total.labels.return_value = m.events_dlq_sent_total
    m.events_commit_failed_total.labels.return_value = m.events_commit_failed_total
    m.redis_operations_total.labels.return_value = m.redis_operations_total
    m.kafka_produce_total.labels.return_value = m.kafka_produce_total
    m.retry_delay_seconds = MagicMock()
    m.retry_partitions_paused = MagicMock()
    return m


@pytest.fixture
def kafka_consumer():
    return Mock()


@pytest.fixture
def kafka_producer():
    kp = Mock()
    future = Mock()
    future.get.return_value = Mock(partition=0, offset=1)
    kp.send.return_value = future
    return kp


@pytest.fixture
def redis_client():
    r = Mock()
    r.exists.return_value = 0
    r.set.return_value = True
    return r


@pytest.fixture
def feature_handler():
    h = Mock()
    h.update_features.return_value = ProcessingResult.APPLIED
    return h


@pytest.fixture
def router(kafka_producer, redis_client, feature_handler, kafka_consumer):
    with patch("retry_router.metrics") as mock_metrics:
        _setup_metrics(mock_metrics)
        r = RetryRouter(
            kafka_producer=kafka_producer,
            redis_client=redis_client,
            feature_update_handler=feature_handler,
            kafka_consumer=kafka_consumer,
        )
        r._mock_metrics = mock_metrics
        yield r


def _setup_metrics(m):
    for attr in [
        "events_processed_total",
        "events_failed_total",
        "events_retry_routed_total",
        "events_retry_tier_routed_total",
        "events_dlq_sent_total",
        "events_commit_failed_total",
        "redis_operations_total",
        "kafka_produce_total",
        "retry_delay_seconds",
        "retry_partitions_paused",
    ]:
        mm = MagicMock()
        mm.labels.return_value = mm
        setattr(m, attr, mm)
    m.record_redis_success = Mock()
    m.record_redis_error = Mock()


def _message_of(event, topic="scalestyle.clicks", partition=0, offset=42, headers=None):
    msg = Mock()
    msg.value = event
    msg.topic = topic
    msg.partition = partition
    msg.offset = offset
    msg.key = event.get("event_id") if isinstance(event, dict) else None
    msg.headers = headers or []
    return msg


def _valid_event(**overrides):
    base = {
        "event_id": "evt-1",
        "user_id": "user-1",
        "item_id": "item-1",
        "timestamp": "2026-03-07T10:00:00Z",
        "session_id": "sess-1",
        "source": "search",
    }
    base.update(overrides)
    return base


# ---------------------------------------------------------------------------
# APPLIED → commit_safe=True
# ---------------------------------------------------------------------------


def test_applied_returns_commit_safe(router, feature_handler, kafka_consumer):
    feature_handler.update_features.return_value = ProcessingResult.APPLIED
    result, commit_safe, strict = router.process_message_internal(
        _message_of(_valid_event()), commit_immediately=True
    )
    assert result == ProcessingResult.APPLIED
    assert commit_safe is True
    assert strict is False
    kafka_consumer.commit.assert_called_once()


# ---------------------------------------------------------------------------
# DUPLICATE → commit_safe=True
# ---------------------------------------------------------------------------


def test_duplicate_returns_commit_safe(router, feature_handler, kafka_consumer):
    feature_handler.update_features.return_value = ProcessingResult.DUPLICATE
    result, commit_safe, strict = router.process_message_internal(
        _message_of(_valid_event()), commit_immediately=True
    )
    assert result == ProcessingResult.DUPLICATE
    assert commit_safe is True
    kafka_consumer.commit.assert_called_once()


# ---------------------------------------------------------------------------
# Transient failure → retry tier 1
# ---------------------------------------------------------------------------


def test_transient_failure_routes_to_retry_tier_1(
    router, feature_handler, kafka_producer, kafka_consumer
):
    feature_handler.update_features.return_value = ProcessingResult.TRANSIENT_FAILURE
    result, commit_safe, strict = router.process_message_internal(
        _message_of(_valid_event()), commit_immediately=True
    )
    assert result == ProcessingResult.TRANSIENT_FAILURE
    assert commit_safe is True
    assert strict is True
    kafka_producer.send.assert_called_once()
    topic_sent = kafka_producer.send.call_args.args[0]
    import config
    assert topic_sent == config.KAFKA_RETRY_TOPIC_1S
    kafka_consumer.commit.assert_called_once()


def test_transient_failure_retry_2_routes_to_tier_2(
    router, feature_handler, kafka_producer, kafka_consumer
):
    import config
    feature_handler.update_features.return_value = ProcessingResult.TRANSIENT_FAILURE
    msg = _message_of(
        _valid_event(),
        topic=config.KAFKA_RETRY_TOPIC_1S,
        headers=[(config.KAFKA_RETRY_HEADER, b"1")],
    )
    result, commit_safe, _ = router.process_message_internal(msg, commit_immediately=True)
    assert result == ProcessingResult.TRANSIENT_FAILURE
    topic_sent = kafka_producer.send.call_args.args[0]
    assert topic_sent == config.KAFKA_RETRY_TOPIC_10S


# ---------------------------------------------------------------------------
# Max retries exceeded → DLQ
# ---------------------------------------------------------------------------


def test_max_retries_exceeded_routes_to_dlq(
    router, feature_handler, kafka_producer, kafka_consumer
):
    import config
    feature_handler.update_features.return_value = ProcessingResult.TRANSIENT_FAILURE
    msg = _message_of(
        _valid_event(),
        topic=config.KAFKA_RETRY_TOPIC_60S,
        headers=[(config.KAFKA_RETRY_HEADER, str(config.MAX_RETRIES).encode())],
    )
    result, commit_safe, strict = router.process_message_internal(
        msg, commit_immediately=True
    )
    assert result == ProcessingResult.TRANSIENT_FAILURE
    assert commit_safe is True
    assert strict is True
    dlq_calls = [
        c for c in kafka_producer.send.call_args_list
        if c.args[0] == config.KAFKA_DLQ_TOPIC
    ]
    assert len(dlq_calls) == 1
    payload = dlq_calls[0].kwargs["value"]
    assert "max_retries_exceeded" in payload["dlq_reason"]


# ---------------------------------------------------------------------------
# Permanent failure → DLQ directly
# ---------------------------------------------------------------------------


def test_permanent_failure_routes_to_dlq(
    router, feature_handler, kafka_producer, kafka_consumer
):
    import config
    feature_handler.update_features.return_value = ProcessingResult.PERMANENT_FAILURE
    result, commit_safe, strict = router.process_message_internal(
        _message_of(_valid_event()), commit_immediately=True
    )
    assert result == ProcessingResult.PERMANENT_FAILURE
    assert commit_safe is True
    dlq_calls = [
        c for c in kafka_producer.send.call_args_list
        if c.args[0] == config.KAFKA_DLQ_TOPIC
    ]
    assert len(dlq_calls) == 1
    payload = dlq_calls[0].kwargs["value"]
    assert "permanent_failure" in payload["dlq_reason"]
    kafka_consumer.commit.assert_called_once()


# ---------------------------------------------------------------------------
# Retry publish failure → commit_safe=False, no raise
# ---------------------------------------------------------------------------


def test_retry_publish_failure_returns_not_commit_safe(
    router, feature_handler, kafka_producer, kafka_consumer
):
    feature_handler.update_features.return_value = ProcessingResult.TRANSIENT_FAILURE
    kafka_producer.send.side_effect = RuntimeError("broker unreachable")
    result, commit_safe, strict = router.process_message_internal(
        _message_of(_valid_event()), commit_immediately=True
    )
    assert result == ProcessingResult.TRANSIENT_FAILURE
    assert commit_safe is False
    kafka_consumer.commit.assert_not_called()


# ---------------------------------------------------------------------------
# Retry published but commit fails → RetryPublishedCommitFailedError
# ---------------------------------------------------------------------------


def test_retry_published_commit_failed_raises(
    router, feature_handler, kafka_producer, kafka_consumer
):
    feature_handler.update_features.return_value = ProcessingResult.TRANSIENT_FAILURE
    kafka_consumer.commit.side_effect = RuntimeError("coordinator unreachable")
    with pytest.raises(RetryPublishedCommitFailedError):
        router.process_message_internal(
            _message_of(_valid_event()), commit_immediately=True
        )
    kafka_producer.send.assert_called_once()
    kafka_consumer.commit.assert_called_once()


# ---------------------------------------------------------------------------
# DLQ published but commit fails → DlqPublishedCommitFailedError
# ---------------------------------------------------------------------------


def test_dlq_published_commit_failed_permanent_raises(
    router, feature_handler, kafka_producer, kafka_consumer
):
    feature_handler.update_features.return_value = ProcessingResult.PERMANENT_FAILURE
    kafka_consumer.commit.side_effect = RuntimeError("coordinator unreachable")
    with pytest.raises(DlqPublishedCommitFailedError):
        router.process_message_internal(
            _message_of(_valid_event()), commit_immediately=True
        )
    kafka_consumer.commit.assert_called_once()


# ---------------------------------------------------------------------------
# DLQ duplicate marker suppresses redundant send
# ---------------------------------------------------------------------------


def test_dlq_duplicate_suppression_commits_safely(
    router, feature_handler, kafka_producer, kafka_consumer, redis_client
):
    feature_handler.update_features.return_value = ProcessingResult.PERMANENT_FAILURE
    redis_client.exists.return_value = 1  # already dispatched
    result, commit_safe, _ = router.process_message_internal(
        _message_of(_valid_event()), commit_immediately=True
    )
    assert result == ProcessingResult.PERMANENT_FAILURE
    assert commit_safe is True
    # No Kafka send for DLQ (duplicate suppressed)
    for call in kafka_producer.send.call_args_list:
        import config
        assert call.args[0] != config.KAFKA_DLQ_TOPIC, "Should not send duplicate to DLQ"
    kafka_consumer.commit.assert_called_once()


# ---------------------------------------------------------------------------
# DLQ canonical schema fields
# ---------------------------------------------------------------------------


def test_dlq_payload_contains_canonical_fields(
    router, feature_handler, kafka_producer
):
    import config
    feature_handler.update_features.return_value = ProcessingResult.PERMANENT_FAILURE
    msg = _message_of(_valid_event(), topic="scalestyle.clicks", partition=3, offset=100)
    router.process_message_internal(msg, commit_immediately=False)
    dlq_calls = [
        c for c in kafka_producer.send.call_args_list
        if c.args[0] == config.KAFKA_DLQ_TOPIC
    ]
    assert dlq_calls, "Expected a DLQ send"
    payload = dlq_calls[0].kwargs["value"]
    assert "dlq_id" in payload
    assert payload["original_topic"] == "scalestyle.clicks"
    assert payload["original_partition"] == 3
    assert payload["original_offset"] == 100
    assert "dlq_reason" in payload
