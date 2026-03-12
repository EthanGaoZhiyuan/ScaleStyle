"""Behavior tests for current EventConsumer semantics."""

import os
import sys
import types
from pathlib import Path
from unittest.mock import MagicMock, Mock, patch

import pytest

# Required at config import time.
os.environ.setdefault("KAFKA_BOOTSTRAP_SERVERS", "test-kafka:9093")
os.environ.setdefault("CONSUMER_MODE", "primary")

# Add src to path
sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

try:
    import kafka  # noqa: F401
except Exception:
    kafka_stub = types.ModuleType("kafka")

    class _KafkaConsumer:
        pass

    class _KafkaProducer:
        pass

    class _TopicPartition:
        def __init__(self, topic, partition):
            self.topic = topic
            self.partition = partition

        def __eq__(self, other):
            return (
                isinstance(other, _TopicPartition)
                and self.topic == other.topic
                and self.partition == other.partition
            )

        def __hash__(self):
            return hash((self.topic, self.partition))

    class _OffsetAndMetadata:
        def __init__(self, offset, metadata):
            self.offset = offset
            self.metadata = metadata

    kafka_stub.KafkaConsumer = _KafkaConsumer
    kafka_stub.KafkaProducer = _KafkaProducer
    kafka_stub.TopicPartition = _TopicPartition
    kafka_stub.OffsetAndMetadata = _OffsetAndMetadata
    sys.modules["kafka"] = kafka_stub

from consumer import (
    EventConsumer,
    ProcessingResult,
    RetryPublishedCommitFailedError,
    DlqPublishedCommitFailedError,
)


@pytest.fixture
def valid_event():
    return {
        "event_id": "evt-123",
        "event_type": "click",
        "user_id": "user-1",
        "item_id": "item-1",
        "timestamp": "2026-03-07T10:30:45.123Z",
        "session_id": "sess-1",
        "source": "search",
    }


@pytest.fixture
def consumer_ctx():
    """Create EventConsumer with current constructor path mocked safely."""
    tracer = Mock()
    span_context = MagicMock()
    span_context.__enter__.return_value = Mock()
    span_context.__exit__.return_value = False
    tracer.start_as_current_span.return_value = span_context

    with patch("consumer.observability.setup_tracing", return_value=tracer) as mock_setup_tracing, patch(
        "consumer.MetricsServer"
    ) as mock_metrics_server_cls, patch(
        "consumer.redis.Redis"
    ) as mock_redis_cls, patch("consumer.KafkaConsumer") as mock_kafka_cls, patch(
        "consumer.KafkaProducer"
    ) as mock_kafka_producer_cls:
        redis_client = Mock()
        redis_client.ping.return_value = True
        redis_client.hget.return_value = "dress"
        redis_client.exists.return_value = 0

        lua_script = Mock()
        redis_client.register_script.return_value = lua_script
        mock_redis_cls.return_value = redis_client

        kafka_consumer = Mock()
        mock_kafka_cls.return_value = kafka_consumer

        kafka_producer = Mock()
        mock_kafka_producer_cls.return_value = kafka_producer

        metrics_server = Mock()
        mock_metrics_server_cls.return_value = metrics_server

        consumer = EventConsumer()
        consumer.tracer = mock_setup_tracing.return_value
        consumer.consumer = kafka_consumer
        consumer.kafka_producer = kafka_producer
        consumer.redis_client = redis_client
        consumer.lua_upsert_script = lua_script

        yield (
            consumer,
            kafka_consumer,
            kafka_producer,
            redis_client,
            lua_script,
            metrics_server,
        )


def _message_of(event):
    msg = Mock()
    msg.value = event
    msg.topic = "scalestyle.clicks"
    msg.partition = 0
    msg.offset = 42
    msg.key = event.get("event_id") if isinstance(event, dict) else None
    msg.headers = []
    return msg


def test_success_commits_offset(consumer_ctx, valid_event):
    """1) success -> commit"""
    consumer, kafka_consumer, _, _, lua_script, _ = consumer_ctx
    lua_script.return_value = "OK"

    result = consumer.process_message(_message_of(valid_event))

    assert result == ProcessingResult.APPLIED
    kafka_consumer.commit.assert_called_once()


def test_duplicate_commits_offset(consumer_ctx, valid_event):
    """2) duplicate -> commit"""
    consumer, kafka_consumer, _, _, lua_script, _ = consumer_ctx
    lua_script.return_value = "DUPLICATE"

    result = consumer.process_message(_message_of(valid_event))

    assert result == ProcessingResult.DUPLICATE
    kafka_consumer.commit.assert_called_once()


def test_transient_failure_routes_retry_and_commits_offset(consumer_ctx, valid_event):
    """3) transient failure -> route retry topic and commit original offset"""
    consumer, kafka_consumer, kafka_producer, _, lua_script, _ = consumer_ctx
    lua_script.side_effect = RuntimeError("lua failed")

    result = consumer.process_message(_message_of(valid_event))

    assert result == ProcessingResult.TRANSIENT_FAILURE
    kafka_producer.send.assert_called_once()
    kafka_consumer.commit.assert_called_once()


def test_malformed_event_not_materialized(consumer_ctx):
    """4) malformed event -> not materialized"""
    consumer, kafka_consumer, kafka_producer, _, lua_script, _ = consumer_ctx
    malformed = {
        "user_id": "user-1",
        "item_id": "item-1",
        # missing event_id
    }

    result = consumer.process_message(_message_of(malformed))

    assert result == ProcessingResult.PERMANENT_FAILURE
    lua_script.assert_not_called()
    kafka_producer.send.assert_called_once()
    kafka_consumer.commit.assert_called_once()


def test_kafka_auth_kwargs_plaintext_omits_sasl_and_ssl_fields(consumer_ctx):
    consumer, _, _, _, _, _ = consumer_ctx

    with patch("consumer.config.KAFKA_SECURITY_PROTOCOL", "PLAINTEXT"), \
         patch("consumer.config.KAFKA_SASL_MECHANISM", "SCRAM-SHA-512"), \
         patch("consumer.config.KAFKA_USERNAME", "user"), \
         patch("consumer.config.KAFKA_PASSWORD", "pass"), \
         patch("consumer.config.KAFKA_SSL_CAFILE", "/tmp/ca.pem"):
        auth_kwargs = consumer._kafka_auth_kwargs()

    assert auth_kwargs == {"security_protocol": "PLAINTEXT"}


def test_missing_or_unknown_category_does_not_update_affinity_incorrectly(
    consumer_ctx, valid_event
):
    """5) missing/unknown category should pass unknown to Lua (affinity update guarded in script)."""
    consumer, kafka_consumer, _, redis_client, lua_script, _ = consumer_ctx

    # Missing category metadata => _infer_category returns "unknown"
    redis_client.hget.return_value = None
    lua_script.return_value = "OK"

    result = consumer.process_message(_message_of(valid_event))

    assert result == ProcessingResult.APPLIED
    kafka_consumer.commit.assert_called_once()

    # Lua ARGV[6] is category; unknown ensures script skips category_affinity increment
    called_args = lua_script.call_args.kwargs["args"]
    assert called_args[5] == "unknown"


def test_retry_send_failure_does_not_commit_offset(consumer_ctx, valid_event):
    """6) retry publish failure -> do not commit original offset"""
    consumer, kafka_consumer, kafka_producer, _, lua_script, _ = consumer_ctx
    lua_script.side_effect = RuntimeError("transient redis error")
    kafka_producer.send.side_effect = RuntimeError("retry topic unavailable")

    result = consumer.process_message(_message_of(valid_event))

    assert result == ProcessingResult.TRANSIENT_FAILURE
    kafka_consumer.commit.assert_not_called()


def test_retry_published_commit_failure_raises_RetryPublishedCommitFailedError(
    consumer_ctx, valid_event
):
    """
    8) retry publish SUCCEEDS but source offset commit FAILS
       → must raise RetryPublishedCommitFailedError, not return silently.

    Rationale:
    Continuing after this state would leave the source offset uncommitted.
    On the next restart or rebalance the broker re-delivers the source message
    and the consumer routes it to retry again, producing a duplicate retry
    entry.  Raising forces the process to terminate so Kubernetes restarts
    cleanly and the group rebalances with an explicit state transfer.

    The duplicate retry produced on restart is idempotent because the retry
    consumer's event_id dedupe (Redis Lua) returns DUPLICATE and commits
    without applying the feature update twice.
    """
    consumer, kafka_consumer, kafka_producer, _, lua_script, _ = consumer_ctx

    # Transient failure triggers the retry path.
    lua_script.side_effect = RuntimeError("transient redis error")

    # Retry publish succeeds — message is durably on the retry topic.
    # (kafka_producer.send returns a Mock whose .get() also returns a Mock
    # with Mock attributes for partition/offset; no exception raised.)

    # Source offset commit then fails.
    kafka_consumer.commit.side_effect = RuntimeError("coordinator unreachable")

    with pytest.raises(RetryPublishedCommitFailedError):
        consumer.process_message(_message_of(valid_event))

    # Verify the retry was attempted (that's what creates the terminal state).
    kafka_producer.send.assert_called_once()
    # Verify commit was attempted before the exception was raised.
    kafka_consumer.commit.assert_called_once()


def test_retry_published_commit_failure_logs_structured_terminal_context(
    consumer_ctx, valid_event, caplog
):
    consumer, kafka_consumer, _, _, lua_script, _ = consumer_ctx
    import logging

    traceparent = "00-0123456789abcdef0123456789abcdef-0123456789abcdef-01"
    retry_msg = _message_of(valid_event)
    retry_msg.headers = [("traceparent", traceparent.encode("utf-8"))]
    lua_script.side_effect = RuntimeError("transient redis error")
    kafka_consumer.commit.side_effect = RuntimeError("coordinator unreachable")

    with caplog.at_level(logging.CRITICAL):
        with pytest.raises(RetryPublishedCommitFailedError):
            consumer.process_message(retry_msg)

    assert "reason=transient_failure" in caplog.text
    assert "downstream_action=retry_sent" in caplog.text
    assert "event_id=evt-123" in caplog.text
    assert "trace_id=0123456789abcdef0123456789abcdef" in caplog.text
    assert "topic=scalestyle.clicks" in caplog.text
    assert "partition=0" in caplog.text
    assert "offset=42" in caplog.text
    assert "retry_count=1" in caplog.text


def test_retry_published_commit_failure_propagates_through_run_loop(
    consumer_ctx, valid_event
):
    """
    9) RetryPublishedCommitFailedError must escape the run() inner loop and
       trigger fail-fast process termination.

    The outer loop handler (which sets loop_alive=False) must receive it so
    the process terminates and Kubernetes can restart the pod.
    """
    consumer, kafka_consumer, kafka_producer, _, lua_script, _ = consumer_ctx

    lua_script.side_effect = RuntimeError("transient redis error")
    kafka_consumer.commit.side_effect = RuntimeError("coordinator unreachable")

    # Wire consumer.poll() to return exactly one message so run() enters the
    # inner loop, then return empty batches so we don't spin.
    msg = _message_of(valid_event)
    from kafka import TopicPartition

    tp = TopicPartition(msg.topic, msg.partition)
    kafka_consumer.poll.side_effect = [
        {tp: [msg]},   # first call: one message → triggers the failing path
        {},            # never reached; run() terminates before second poll
    ]
    kafka_consumer.assignment.return_value = set()

    # run() converts this terminal state into SystemExit(1) after marking the
    # loop unhealthy, so the pod restarts immediately instead of waiting for a
    # liveness timeout.
    with pytest.raises(SystemExit) as exc_info:
        consumer.run()

    assert exc_info.value.code == 1
    assert consumer.loop_alive is False


def test_run_batch_commit_stops_at_first_unsafe_offset(consumer_ctx, valid_event):
    """
    When a message cannot be committed (e.g. retry send failed), batch commit
    advances only up to the last safe offset; partition stops at first failure.
    """
    consumer, kafka_consumer, _, _, _, _ = consumer_ctx
    from kafka import TopicPartition

    msg1 = _message_of(valid_event)
    msg1.partition = 0
    msg1.offset = 10

    event2 = dict(valid_event)
    event2["event_id"] = "evt-124"
    msg2 = _message_of(event2)
    msg2.partition = 0
    msg2.offset = 11

    tp0 = TopicPartition(msg1.topic, 0)
    kafka_consumer.poll.side_effect = [
        {tp0: [msg1, msg2]},
        KeyboardInterrupt(),
    ]
    kafka_consumer.assignment.return_value = set()

    # msg1 safe, msg2 unsafe (e.g. retry send failed)
    def mock_process(m, **_):
        return (
            (ProcessingResult.APPLIED, True, False) if m.offset == 10
            else (ProcessingResult.TRANSIENT_FAILURE, False, False)
        )
    consumer._process_message_internal = mock_process

    consumer.run()

    assert kafka_consumer.commit.called
    committed = kafka_consumer.commit.call_args.kwargs["offsets"]
    assert tp0 in committed
    assert committed[tp0].offset == 11  # msg1.offset + 1, not msg2


def test_run_batches_partition_commits_to_highest_safe_offset(consumer_ctx, valid_event):
    """run() commits one offset map per poll batch."""
    consumer, kafka_consumer, _, _, _, _ = consumer_ctx
    from kafka import TopicPartition
    import config

    msg1 = _message_of(valid_event)
    msg1.partition = 0
    msg1.offset = 10

    event2 = dict(valid_event)
    event2["event_id"] = "evt-124"
    msg2 = _message_of(event2)
    msg2.partition = 0
    msg2.offset = 11

    event3 = dict(valid_event)
    event3["event_id"] = "evt-125"
    msg3 = _message_of(event3)
    msg3.partition = 1
    msg3.offset = 7

    tp0 = TopicPartition(msg1.topic, 0)
    tp1 = TopicPartition(msg1.topic, 1)
    kafka_consumer.poll.side_effect = [
        {tp0: [msg1, msg2], tp1: [msg3]},
        KeyboardInterrupt(),
    ]
    kafka_consumer.assignment.return_value = set()

    consumer._process_message_internal = Mock(
        side_effect=[
            (ProcessingResult.APPLIED, True, False),
            (ProcessingResult.APPLIED, True, False),
            (ProcessingResult.APPLIED, True, False),
        ]
    )

    consumer.run()

    kafka_consumer.commit.assert_called_once()
    committed = kafka_consumer.commit.call_args.kwargs["offsets"]
    assert committed[tp0].offset == 12
    assert committed[tp1].offset == 8
    assert kafka_consumer.poll.call_args_list[0].kwargs["max_records"] == config.KAFKA_POLL_MAX_RECORDS


def test_dlq_marker_persist_failure_commits_offset_with_at_least_once_semantics(consumer_ctx):
    """7) DLQ ack success but marker persist failure -> commit source offset."""
    consumer, kafka_consumer, kafka_producer, redis_client, _, _ = consumer_ctx
    redis_client.set.return_value = None
    malformed = {
        "user_id": "user-1",
        "item_id": "item-1",
    }

    result = consumer.process_message(_message_of(malformed))

    assert result == ProcessingResult.PERMANENT_FAILURE
    kafka_producer.send.assert_called_once()
    kafka_consumer.commit.assert_called_once()


def test_dlq_published_commit_failure_permanent_raises_DlqPublishedCommitFailedError(
    consumer_ctx,
):
    """
    PERMANENT_FAILURE -> DLQ sent/duplicate -> source commit fails
    must raise DlqPublishedCommitFailedError (fail-fast, same semantics as retry path).
    """
    consumer, kafka_consumer, kafka_producer, redis_client, _, _ = consumer_ctx
    malformed = {"user_id": "user-1", "item_id": "item-1"}  # missing event_id
    msg = _message_of(malformed)

    redis_client.exists.return_value = 0
    redis_client.set.return_value = True  # DLQ marker persisted
    kafka_consumer.commit.side_effect = RuntimeError("coordinator unreachable")

    with pytest.raises(DlqPublishedCommitFailedError):
        consumer.process_message(msg)

    kafka_producer.send.assert_called_once()
    kafka_consumer.commit.assert_called_once()


def test_dlq_published_commit_failure_max_retries_raises_DlqPublishedCommitFailedError(
    consumer_ctx, valid_event
):
    """
    MAX_RETRIES_EXCEEDED -> DLQ sent/duplicate -> source commit fails
    must raise DlqPublishedCommitFailedError (fail-fast, same semantics as retry path).
    """
    consumer, kafka_consumer, kafka_producer, redis_client, lua_script, _ = consumer_ctx
    import config

    msg = _message_of(valid_event)
    msg.topic = config.KAFKA_RETRY_TOPIC_60S
    msg.headers = [(config.KAFKA_RETRY_HEADER, b"3")]
    msg.value["_retry_meta"] = {
        "retry_count": 3,
        "retry_tier": "retry_60s",
        "retry_topic": config.KAFKA_RETRY_TOPIC_60S,
        "delay_seconds": 60.0,
        "routed_at_ts": 0,
        "reason": "transient",
    }
    lua_script.side_effect = RuntimeError("redis down")
    redis_client.exists.return_value = 0
    redis_client.set.return_value = True
    kafka_consumer.commit.side_effect = RuntimeError("coordinator unreachable")

    with pytest.raises(DlqPublishedCommitFailedError):
        consumer.process_message(msg)

    kafka_producer.send.assert_called_once()
    assert kafka_producer.send.call_args.args[0] == config.KAFKA_DLQ_TOPIC
    kafka_consumer.commit.assert_called_once()


# ==========================================
# Consumer Mode Isolation Tests
# ==========================================


@patch.dict("os.environ", {"CONSUMER_MODE": "primary"}, clear=False)
def test_primary_mode_subscribes_only_to_main_topic():
    """Primary mode should only subscribe to scalestyle.clicks"""
    import importlib
    import config
    
    # Reload config to pick up env var
    importlib.reload(config)
    
    with patch("consumer.MetricsServer"), \
         patch("consumer.redis.Redis") as mock_redis_cls, \
         patch("consumer.KafkaConsumer") as mock_kafka_cls, \
         patch("consumer.KafkaProducer"):
        
        redis_client = Mock()
        redis_client.ping.return_value = True
        redis_client.register_script.return_value = Mock()
        mock_redis_cls.return_value = redis_client
        
        EventConsumer()
        
        # Verify KafkaConsumer was called with ONLY the primary topic
        assert mock_kafka_cls.called
        call_args = mock_kafka_cls.call_args
        subscribed_topics = call_args[0]  # positional args
        
        assert len(subscribed_topics) == 1
        assert subscribed_topics[0] == config.KAFKA_TOPIC
        assert config.KAFKA_RETRY_TOPIC not in subscribed_topics


@patch.dict("os.environ", {"CONSUMER_MODE": "retry"}, clear=False)
def test_retry_mode_subscribes_only_to_retry_topic():
    """Retry mode should subscribe to all tiered retry topics"""
    import importlib
    import config
    
    # Reload config to pick up env var
    importlib.reload(config)
    
    with patch("consumer.MetricsServer"), \
         patch("consumer.redis.Redis") as mock_redis_cls, \
         patch("consumer.KafkaConsumer") as mock_kafka_cls, \
         patch("consumer.KafkaProducer"):
        
        redis_client = Mock()
        redis_client.ping.return_value = True
        redis_client.register_script.return_value = Mock()
        mock_redis_cls.return_value = redis_client
        
        EventConsumer()
        
        # Verify KafkaConsumer was called with ONLY the retry topic
        assert mock_kafka_cls.called
        call_args = mock_kafka_cls.call_args
        subscribed_topics = call_args[0]  # positional args
        
        assert tuple(subscribed_topics) == tuple(config.KAFKA_RETRY_TOPICS)
        assert config.KAFKA_TOPIC not in subscribed_topics


@patch.dict("os.environ", {}, clear=False)
def test_missing_consumer_mode_raises_error():
    """Missing CONSUMER_MODE should raise RuntimeError at config load time"""
    import importlib
    
    # Remove CONSUMER_MODE if set
    import os
    if "CONSUMER_MODE" in os.environ:
        del os.environ["CONSUMER_MODE"]
    
    with pytest.raises(RuntimeError, match="CONSUMER_MODE must be explicitly set"):
        import config
        importlib.reload(config)


@patch.dict("os.environ", {"CONSUMER_MODE": "primary"}, clear=False)
def test_primary_mode_uses_isolated_consumer_group():
    """Primary mode should use event-consumer-primary group"""
    import importlib
    import config
    
    # Reload config to pick up env var
    importlib.reload(config)
    
    with patch("consumer.MetricsServer"), \
         patch("consumer.redis.Redis") as mock_redis_cls, \
         patch("consumer.KafkaConsumer") as mock_kafka_cls, \
         patch("consumer.KafkaProducer"):
        
        redis_client = Mock()
        redis_client.ping.return_value = True
        redis_client.register_script.return_value = Mock()
        mock_redis_cls.return_value = redis_client
        
        EventConsumer()
        
        # Verify consumer group ID has -primary suffix
        call_kwargs = mock_kafka_cls.call_args[1]
        assert call_kwargs["group_id"] == "event-consumer-primary"


@patch.dict("os.environ", {"CONSUMER_MODE": "retry"}, clear=False)
def test_retry_mode_uses_isolated_consumer_group():
    """Retry mode should use event-consumer-retry group"""
    import importlib
    import config
    
    # Reload config to pick up env var
    importlib.reload(config)
    
    with patch("consumer.MetricsServer"), \
         patch("consumer.redis.Redis") as mock_redis_cls, \
         patch("consumer.KafkaConsumer") as mock_kafka_cls, \
         patch("consumer.KafkaProducer"):
        
        redis_client = Mock()
        redis_client.ping.return_value = True
        redis_client.register_script.return_value = Mock()
        mock_redis_cls.return_value = redis_client
        
        EventConsumer()
        
        # Verify consumer group ID has -retry suffix
        call_kwargs = mock_kafka_cls.call_args[1]
        assert call_kwargs["group_id"] == "event-consumer-retry"


def test_internal_producer_publish_budget_is_bounded_to_future_get_timeout():
    with patch("consumer.observability.setup_tracing", return_value=Mock()), \
         patch("consumer.MetricsServer") as mock_metrics_server_cls, \
         patch("consumer.redis.Redis") as mock_redis_cls, \
         patch("consumer.KafkaConsumer") as mock_kafka_cls, \
         patch("consumer.KafkaProducer") as mock_kafka_producer_cls:

        redis_client = Mock()
        redis_client.ping.return_value = True
        redis_client.register_script.return_value = Mock()
        mock_redis_cls.return_value = redis_client
        mock_kafka_cls.return_value = Mock()
        mock_metrics_server_cls.return_value = Mock()

        EventConsumer()

        producer_kwargs = mock_kafka_producer_cls.call_args.kwargs
        assert producer_kwargs["delivery_timeout_ms"] <= 5_000
        assert producer_kwargs["request_timeout_ms"] < producer_kwargs["delivery_timeout_ms"]
        assert producer_kwargs["request_timeout_ms"] == 3_500
        assert producer_kwargs["delivery_timeout_ms"] == 4_500


# ==========================================
# Retry State Machine Critical Tests
# ==========================================


@patch.dict("os.environ", {"RETRY_ENFORCE_DELAY": "true"}, clear=False)
def test_retry_message_before_tier_delay_pauses_partition_without_processing(consumer_ctx, valid_event):
    """
    When RETRY_ENFORCE_DELAY=true: retry message before tier delay defers (pauses partition).

    Assertions:
    - Redis is NOT updated
    - Offset is NOT committed
    - Message is NOT sent to DLQ
    - Partition is paused for backoff
    """
    import importlib
    import config
    importlib.reload(config)

    consumer, kafka_consumer, kafka_producer, redis_client, lua_script, _ = consumer_ctx
    import time

    routed_at_ts = time.time()
    retry_event = dict(valid_event)
    retry_event["_retry_meta"] = {
        "retry_count": 1,
        "retry_tier": "retry_1s",
        "retry_topic": config.KAFKA_RETRY_TOPIC_1S,
        "delay_seconds": 1.0,
        "routed_at_ts": routed_at_ts,
        "reason": "transient_error",
    }

    msg = _message_of(retry_event)
    msg.topic = config.KAFKA_RETRY_TOPIC_1S
    msg.headers = [(config.KAFKA_RETRY_HEADER, b"1")]

    result = consumer.process_message(msg)

    assert result == ProcessingResult.TRANSIENT_FAILURE
    lua_script.assert_not_called()
    kafka_consumer.commit.assert_not_called()
    kafka_producer.send.assert_not_called()

    from kafka import TopicPartition

    tp = TopicPartition(msg.topic, msg.partition)
    assert tp in consumer.paused_partitions
    assert consumer.paused_partitions[tp] > time.time()


@patch.dict("os.environ", {"CONSUMER_MODE": "retry", "RETRY_ENFORCE_DELAY": "false"}, clear=False)
def test_retry_mode_refuses_immediate_processing_without_explicit_unsafe_override():
    """Retry worker must fail fast if delay semantics are disabled implicitly."""
    import importlib
    import config

    with pytest.raises(RuntimeError, match="ALLOW_UNSAFE_IMMEDIATE_RETRY=true"):
        importlib.reload(config)


@patch.dict(
    "os.environ",
    {
        "CONSUMER_MODE": "retry",
        "RETRY_ENFORCE_DELAY": "false",
        "ALLOW_UNSAFE_IMMEDIATE_RETRY": "true",
    },
    clear=False,
)
def test_retry_message_processed_immediately_when_delay_disabled(consumer_ctx, valid_event):
    """
    When RETRY_ENFORCE_DELAY=false with explicit unsafe opt-in: retry messages process immediately.

    No pause/seek; tier topology provides isolation only.
    """
    import importlib
    import config

    importlib.reload(config)

    consumer, kafka_consumer, kafka_producer, redis_client, lua_script, _ = consumer_ctx

    retry_event = dict(valid_event)
    retry_event["_retry_meta"] = {
        "retry_count": 1,
        "retry_tier": "retry_1s",
        "retry_topic": config.KAFKA_RETRY_TOPIC_1S,
        "delay_seconds": 1.0,
        "routed_at_ts": 0,
        "reason": "transient_error",
    }

    msg = _message_of(retry_event)
    msg.topic = config.KAFKA_RETRY_TOPIC_1S
    msg.headers = [(config.KAFKA_RETRY_HEADER, b"1")]

    lua_script.return_value = "OK"

    result = consumer.process_message(msg)

    assert result == ProcessingResult.APPLIED
    lua_script.assert_called_once()
    kafka_consumer.commit.assert_called_once()
    kafka_producer.send.assert_not_called()


@patch.dict("os.environ", {"RETRY_ENFORCE_DELAY": "true"}, clear=False)
def test_retry_run_loop_waits_until_tier_delay_elapses_before_processing(consumer_ctx, valid_event):
    """Run loop must defer a retry message until its tier delay has actually elapsed."""
    import importlib
    import config
    from kafka import TopicPartition

    importlib.reload(config)

    consumer, kafka_consumer, kafka_producer, redis_client, lua_script, _ = consumer_ctx

    fake_now = [1000.0]
    retry_event = dict(valid_event)
    retry_event["_retry_meta"] = {
        "retry_count": 1,
        "retry_tier": "retry_1s",
        "retry_topic": config.KAFKA_RETRY_TOPIC_1S,
        "delay_seconds": 1.0,
        "routed_at_ts": fake_now[0],
        "reason": "transient_error",
    }

    msg = _message_of(retry_event)
    msg.topic = config.KAFKA_RETRY_TOPIC_1S
    msg.partition = 0
    msg.offset = 100
    msg.headers = [(config.KAFKA_RETRY_HEADER, b"1")]

    tp = TopicPartition(msg.topic, msg.partition)
    lua_script.return_value = "OK"

    poll_count = {"value": 0}

    def _poll(*args, **kwargs):
        poll_count["value"] += 1
        if poll_count["value"] == 1:
            return {tp: [msg]}
        if poll_count["value"] == 2:
            fake_now[0] = 1000.4
            return {}
        if poll_count["value"] == 3:
            fake_now[0] = 1001.1
            return {}
        if poll_count["value"] == 4:
            fake_now[0] = 1001.2
            return {tp: [msg]}
        fake_now[0] = 1001.3
        raise KeyboardInterrupt()

    kafka_consumer.poll.side_effect = _poll
    kafka_consumer.assignment.return_value = {tp}

    with patch("consumer.time.time", side_effect=lambda: fake_now[0]):
        consumer.run()

    kafka_consumer.pause.assert_called_once_with(tp)
    kafka_consumer.resume.assert_any_call(tp)
    lua_script.assert_called_once()
    kafka_producer.send.assert_not_called()
    committed = kafka_consumer.commit.call_args.kwargs["offsets"]
    assert committed[tp].offset == msg.offset + 1


def test_retry_message_after_tier_delay_processes_normally(consumer_ctx, valid_event):
    """
    Critical test 2: Retry-tier message after its fixed tier delay should process normally.
    
    Assertions:
    - Redis update IS executed
    - Offset IS committed on success
    - Message is NOT re-routed to retry topic (retry cycle ends)
    """
    consumer, kafka_consumer, kafka_producer, redis_client, lua_script, _ = consumer_ctx
    
    import config
    import time
    
    # Create retry-1s message routed in the past so its fixed tier delay is already elapsed.
    past_ts = time.time() - 10.0
    retry_event = dict(valid_event)
    retry_event["_retry_meta"] = {
        "retry_count": 1,
        "retry_tier": "retry_1s",
        "retry_topic": config.KAFKA_RETRY_TOPIC_1S,
        "delay_seconds": 1.0,
        "routed_at_ts": past_ts,
        "reason": "transient_error"
    }
    
    msg = _message_of(retry_event)
    msg.topic = config.KAFKA_RETRY_TOPIC_1S
    msg.headers = [(config.KAFKA_RETRY_HEADER, b"1")]
    
    # Lua script succeeds
    lua_script.return_value = "OK"
    
    # Process message
    result = consumer.process_message(msg)
    
    # Assert: Successfully applied
    assert result == ProcessingResult.APPLIED
    
    # Assert: Redis WAS updated (Lua script called)
    lua_script.assert_called_once()
    
    # Assert: Offset WAS committed
    kafka_consumer.commit.assert_called_once()
    
    # Assert: NOT sent to retry topic again (success ends retry cycle)
    kafka_producer.send.assert_not_called()


@patch.dict("os.environ", {"RETRY_ENFORCE_DELAY": "true"}, clear=False)
def test_delayed_retry_60s_partition_does_not_starve_ready_retry_1s_partition(consumer_ctx, valid_event):
    """
    When RETRY_ENFORCE_DELAY=true: a delayed retry-60s partition must not block
    a ready retry-1s partition in the same poll batch. Ready partition commits.
    """
    import importlib
    import config
    importlib.reload(config)

    consumer, kafka_consumer, kafka_producer, redis_client, lua_script, _ = consumer_ctx
    import time
    from kafka import TopicPartition

    delayed_event = dict(valid_event)
    delayed_event["event_id"] = "evt-delayed"
    delayed_event["_retry_meta"] = {
        "retry_count": 3,
        "retry_tier": "retry_60s",
        "retry_topic": config.KAFKA_RETRY_TOPIC_60S,
        "delay_seconds": 60.0,
        "routed_at_ts": time.time(),
        "reason": "transient_failure",
    }
    delayed_msg = _message_of(delayed_event)
    delayed_msg.topic = config.KAFKA_RETRY_TOPIC_60S
    delayed_msg.partition = 0
    delayed_msg.offset = 100
    delayed_msg.headers = [(config.KAFKA_RETRY_HEADER, b"3")]

    ready_event = dict(valid_event)
    ready_event["event_id"] = "evt-ready"
    ready_event["_retry_meta"] = {
        "retry_count": 1,
        "retry_tier": "retry_1s",
        "retry_topic": config.KAFKA_RETRY_TOPIC_1S,
        "delay_seconds": 1.0,
        "routed_at_ts": 0,
        "reason": "transient_failure",
    }
    ready_msg = _message_of(ready_event)
    ready_msg.topic = config.KAFKA_RETRY_TOPIC_1S
    ready_msg.partition = 1
    ready_msg.offset = 200
    ready_msg.headers = [(config.KAFKA_RETRY_HEADER, b"1")]

    lua_script.return_value = "OK"
    tp_delayed = TopicPartition(delayed_msg.topic, delayed_msg.partition)
    tp_ready = TopicPartition(ready_msg.topic, ready_msg.partition)
    kafka_consumer.poll.side_effect = [
        {tp_delayed: [delayed_msg], tp_ready: [ready_msg]},
        KeyboardInterrupt(),
    ]
    kafka_consumer.assignment.return_value = {tp_delayed, tp_ready}

    consumer.run()

    committed = kafka_consumer.commit.call_args.kwargs["offsets"]
    assert tp_ready in committed
    assert committed[tp_ready].offset == ready_msg.offset + 1
    assert tp_delayed not in committed
    lua_script.assert_called_once()


def test_transient_failure_succeeds_on_retry(consumer_ctx, valid_event):
    """
    Transient failure -> route to retry tier -> retry consumer processes successfully.

    Simulates full flow: primary fails, retry message produced; retry consumer
    receives and applies. No pause/seek when RETRY_ENFORCE_DELAY=false.
    """
    consumer, kafka_consumer, kafka_producer, redis_client, lua_script, _ = consumer_ctx
    import config

    msg = _message_of(valid_event)
    lua_script.side_effect = [RuntimeError("redis blip"), "OK"]

    result1 = consumer.process_message(msg)
    assert result1 == ProcessingResult.TRANSIENT_FAILURE
    assert kafka_producer.send.call_count == 1
    retry_call = kafka_producer.send.call_args
    assert retry_call.args[0] == config.KAFKA_RETRY_TOPIC_1S
    kafka_consumer.commit.reset_mock()

    # Simulate retry consumer receiving the message (same payload producer sent).
    # routed_at_ts=0 ensures message is ready regardless of RETRY_ENFORCE_DELAY.
    retry_event = dict(valid_event)
    retry_meta = retry_call.kwargs["value"].get("_retry_meta", {})
    retry_meta["routed_at_ts"] = 0
    retry_event["_retry_meta"] = retry_meta
    retry_msg = _message_of(retry_event)
    retry_msg.topic = config.KAFKA_RETRY_TOPIC_1S
    retry_msg.headers = [(config.KAFKA_RETRY_HEADER, b"1")]

    result2 = consumer.process_message(retry_msg)
    assert result2 == ProcessingResult.APPLIED
    assert lua_script.call_count == 2
    kafka_consumer.commit.assert_called_once()


def test_poison_message_reaches_dlq_after_bounded_retries(consumer_ctx, valid_event):
    """
    Poison message (persistent transient failure) exhausts retries and reaches DLQ.
    """
    consumer, kafka_consumer, kafka_producer, redis_client, lua_script, _ = consumer_ctx
    import config

    lua_script.side_effect = RuntimeError("redis persistently down")
    redis_client.exists.return_value = 0
    redis_client.set.return_value = True

    msg1 = _message_of(valid_event)
    msg1.headers = []
    assert consumer.process_message(msg1) == ProcessingResult.TRANSIENT_FAILURE

    msg2 = _message_of(valid_event)
    msg2.topic = config.KAFKA_RETRY_TOPIC_1S
    msg2.headers = [(config.KAFKA_RETRY_HEADER, b"1")]
    msg2.value["_retry_meta"] = {
        "retry_count": 1,
        "retry_tier": "retry_1s",
        "routed_at_ts": 0,
        "reason": "transient",
    }
    assert consumer.process_message(msg2) == ProcessingResult.TRANSIENT_FAILURE

    msg3 = _message_of(valid_event)
    msg3.topic = config.KAFKA_RETRY_TOPIC_10S
    msg3.headers = [(config.KAFKA_RETRY_HEADER, b"2")]
    msg3.value["_retry_meta"] = {
        "retry_count": 2,
        "retry_tier": "retry_10s",
        "routed_at_ts": 0,
        "reason": "transient",
    }
    assert consumer.process_message(msg3) == ProcessingResult.TRANSIENT_FAILURE

    msg4 = _message_of(valid_event)
    msg4.topic = config.KAFKA_RETRY_TOPIC_60S
    msg4.headers = [(config.KAFKA_RETRY_HEADER, b"3")]
    msg4.value["_retry_meta"] = {
        "retry_count": 3,
        "retry_tier": "retry_60s",
        "routed_at_ts": 0,
        "reason": "transient",
    }
    result4 = consumer.process_message(msg4)
    assert result4 == ProcessingResult.TRANSIENT_FAILURE
    dlq_send = [c for c in kafka_producer.send.call_args_list if c.args[0] == config.KAFKA_DLQ_TOPIC]
    assert len(dlq_send) == 1
    assert "max_retries_exceeded" in dlq_send[0].kwargs["value"]["dlq_reason"]


def test_retry_count_increments_across_failures_until_dlq(consumer_ctx, valid_event):
    """
    Critical test 3: Retry count increments across failures and persists across topics.
    
    Assertions:
    - 1st failure -> retry_count=1
    - 2nd failure -> retry_count=2
    - 3rd failure -> retry_count=3
    - 4th failure -> exceeds MAX_RETRIES, routes to DLQ
    """
    consumer, kafka_consumer, kafka_producer, redis_client, lua_script, _ = consumer_ctx
    
    import config
    
    # === First failure: retry_count should be 1 ===
    msg1 = _message_of(valid_event)
    msg1.headers = []  # No retry header yet (original message)
    lua_script.side_effect = RuntimeError("transient failure 1")
    
    result1 = consumer.process_message(msg1)
    assert result1 == ProcessingResult.TRANSIENT_FAILURE
    
    # Assert: Sent to retry-1s tier with retry_count=1
    kafka_producer.send.assert_called_once()
    retry_call_1 = kafka_producer.send.call_args
    assert retry_call_1.args[0] == config.KAFKA_RETRY_TOPIC_1S
    assert retry_call_1.kwargs["headers"] == [(config.KAFKA_RETRY_HEADER, b"1")]
    assert retry_call_1.kwargs["value"]["_retry_meta"]["retry_count"] == 1
    assert retry_call_1.kwargs["value"]["_retry_meta"]["retry_tier"] == "retry_1s"
    kafka_producer.send.reset_mock()
    
    # === Second failure: retry_count should be 2 ===
    msg2 = _message_of(valid_event)
    msg2.topic = config.KAFKA_RETRY_TOPIC_1S
    msg2.headers = [(config.KAFKA_RETRY_HEADER, b"1")]
    msg2.value["_retry_meta"] = {
        "retry_count": 1,
        "retry_tier": "retry_1s",
        "retry_topic": config.KAFKA_RETRY_TOPIC_1S,
        "delay_seconds": 1.0,
        "routed_at_ts": 0,
        "reason": "transient failure 1"
    }
    lua_script.side_effect = RuntimeError("transient failure 2")
    
    result2 = consumer.process_message(msg2)
    assert result2 == ProcessingResult.TRANSIENT_FAILURE
    
    # Assert: Sent to retry-10s tier with retry_count=2
    kafka_producer.send.assert_called_once()
    retry_call_2 = kafka_producer.send.call_args
    assert retry_call_2.args[0] == config.KAFKA_RETRY_TOPIC_10S
    assert retry_call_2.kwargs["headers"] == [(config.KAFKA_RETRY_HEADER, b"2")]
    assert retry_call_2.kwargs["value"]["_retry_meta"]["retry_count"] == 2
    assert retry_call_2.kwargs["value"]["_retry_meta"]["retry_tier"] == "retry_10s"
    kafka_producer.send.reset_mock()
    
    # === Third failure: retry_count should be 3 ===
    msg3 = _message_of(valid_event)
    msg3.topic = config.KAFKA_RETRY_TOPIC_10S
    msg3.headers = [(config.KAFKA_RETRY_HEADER, b"2")]
    msg3.value["_retry_meta"] = {
        "retry_count": 2,
        "retry_tier": "retry_10s",
        "retry_topic": config.KAFKA_RETRY_TOPIC_10S,
        "delay_seconds": 10.0,
        "routed_at_ts": 0,
        "reason": "transient failure 2"
    }
    lua_script.side_effect = RuntimeError("transient failure 3")
    
    result3 = consumer.process_message(msg3)
    assert result3 == ProcessingResult.TRANSIENT_FAILURE
    
    # Assert: Sent to retry-60s tier with retry_count=3
    kafka_producer.send.assert_called_once()
    retry_call_3 = kafka_producer.send.call_args
    assert retry_call_3.args[0] == config.KAFKA_RETRY_TOPIC_60S
    assert retry_call_3.kwargs["headers"] == [(config.KAFKA_RETRY_HEADER, b"3")]
    assert retry_call_3.kwargs["value"]["_retry_meta"]["retry_count"] == 3
    assert retry_call_3.kwargs["value"]["_retry_meta"]["retry_tier"] == "retry_60s"
    kafka_producer.send.reset_mock()
    
    # === Fourth failure: retry_count=4 exceeds MAX_RETRIES (3), should go to DLQ ===
    msg4 = _message_of(valid_event)
    msg4.topic = config.KAFKA_RETRY_TOPIC_60S
    msg4.headers = [(config.KAFKA_RETRY_HEADER, b"3")]
    msg4.value["_retry_meta"] = {
        "retry_count": 3,
        "retry_tier": "retry_60s",
        "retry_topic": config.KAFKA_RETRY_TOPIC_60S,
        "delay_seconds": 60.0,
        "routed_at_ts": 0,
        "reason": "transient failure 3"
    }
    lua_script.side_effect = RuntimeError("transient failure 4 - exceeds max retries")
    
    # Mock DLQ operations
    redis_client.exists.return_value = 0  # Not already in DLQ
    redis_client.set.return_value = True  # Marker persisted successfully
    
    result4 = consumer.process_message(msg4)
    assert result4 == ProcessingResult.TRANSIENT_FAILURE
    
    # Assert: Sent to DLQ (not retry topic)
    kafka_producer.send.assert_called_once()
    dlq_call = kafka_producer.send.call_args
    assert dlq_call.args[0] == config.KAFKA_DLQ_TOPIC
    assert "max_retries_exceeded" in dlq_call.kwargs["value"]["dlq_reason"]
    # retry_count in DLQ payload is current count (3), not next_retry (4)
    assert dlq_call.kwargs["value"]["retry_count"] == 3


def test_dlq_duplicate_prevents_redundant_send_and_commits_safely(consumer_ctx):
    """
    Critical test 4: DLQ duplicate detection prevents redundant sends.
    
    Assertions:
    - When dlq_id already exists, DLQ send is skipped
    - Redis dedupe marker is not re-persisted
    - Original offset IS committed safely (idempotent operation)
    """
    consumer, kafka_consumer, kafka_producer, redis_client, lua_script, _ = consumer_ctx
    
    malformed_event = {
        "user_id": "user-1",
        "item_id": "item-1",
        # missing event_id - permanent failure
    }
    
    msg = _message_of(malformed_event)
    
    # Mock: DLQ already dispatched (Redis returns True for duplicate check)
    redis_client.exists.return_value = 1  # dlq:sent:{dlq_id} already exists
    
    # Process message
    result = consumer.process_message(msg)
    
    # Assert: Permanent failure detected
    assert result == ProcessingResult.PERMANENT_FAILURE
    
    # Assert: DLQ send was SKIPPED (duplicate detected before send)
    kafka_producer.send.assert_not_called()
    
    # Assert: Redis dedupe marker NOT re-persisted (already exists)
    redis_client.set.assert_not_called()
    
    # Assert: Offset WAS committed safely (idempotent operation)
    kafka_consumer.commit.assert_called_once()


def test_trace_headers_are_forwarded_to_retry_and_dlq(consumer_ctx, valid_event):
    """Trace context should survive retry/DLQ hops for cross-service correlation."""
    consumer, kafka_consumer, kafka_producer, redis_client, lua_script, _ = consumer_ctx
    import config

    traceparent = "00-0123456789abcdef0123456789abcdef-0123456789abcdef-01"
    tracestate = "vendor=value"

    retry_msg = _message_of(valid_event)
    retry_msg.headers = [
        (config.KAFKA_TRACEPARENT_HEADER, traceparent.encode("utf-8")),
        (config.KAFKA_TRACESTATE_HEADER, tracestate.encode("utf-8")),
    ]
    lua_script.side_effect = RuntimeError("transient redis error")
    result_retry = consumer.process_message(retry_msg)
    assert result_retry == ProcessingResult.TRANSIENT_FAILURE
    retry_headers = kafka_producer.send.call_args.kwargs["headers"]
    assert (config.KAFKA_TRACEPARENT_HEADER, traceparent.encode("utf-8")) in retry_headers
    assert (config.KAFKA_TRACESTATE_HEADER, tracestate.encode("utf-8")) in retry_headers
    kafka_producer.send.reset_mock()
    lua_script.side_effect = None

    malformed = {"user_id": "user-1", "item_id": "item-1"}
    dlq_msg = _message_of(malformed)
    dlq_msg.headers = [
        (config.KAFKA_TRACEPARENT_HEADER, traceparent.encode("utf-8")),
        (config.KAFKA_TRACESTATE_HEADER, tracestate.encode("utf-8")),
    ]
    redis_client.exists.return_value = 0
    redis_client.set.return_value = True
    result_dlq = consumer.process_message(dlq_msg)
    assert result_dlq == ProcessingResult.PERMANENT_FAILURE
    dlq_payload = kafka_producer.send.call_args.kwargs["value"]
    assert dlq_payload["trace_id"] == "0123456789abcdef0123456789abcdef"
    assert dlq_payload["traceparent"] == traceparent
    assert dlq_payload["tracestate"] == tracestate


def test_process_message_extracts_parent_context_before_starting_span(
    consumer_ctx, valid_event
):
    consumer, _, _, _, lua_script, _ = consumer_ctx
    lua_script.return_value = "OK"

    traceparent = "00-0123456789abcdef0123456789abcdef-0123456789abcdef-01"
    tracestate = "vendor=value"
    msg = _message_of(valid_event)
    msg.headers = [
        ("traceparent", traceparent.encode("utf-8")),
        ("tracestate", tracestate.encode("utf-8")),
    ]

    parent_context = object()
    span = Mock()
    span_context = MagicMock()
    span_context.__enter__.return_value = span
    span_context.__exit__.return_value = False
    tracer = Mock()
    tracer.start_as_current_span.return_value = span_context
    consumer.tracer = tracer

    with patch("consumer.observability.extract_context", return_value=parent_context) as extract_context:
        result = consumer.process_message(msg)

    assert result == ProcessingResult.APPLIED
    extract_context.assert_called_once_with(
        {
            "traceparent": traceparent,
            "tracestate": tracestate,
        }
    )
    tracer.start_as_current_span.assert_called_once()
    call = tracer.start_as_current_span.call_args
    assert call.args[0] == "event-consumer.process"
    assert call.kwargs["context"] is parent_context
    assert call.kwargs["attributes"]["messaging.destination.name"] == "scalestyle.clicks"
    assert call.kwargs["attributes"]["messaging.kafka.partition"] == 0
    assert call.kwargs["attributes"]["messaging.kafka.offset"] == 42
    import consumer as consumer_module

    if consumer_module.observability.SPAN_KIND_CONSUMER is None:
        assert "kind" not in call.kwargs
    else:
        assert call.kwargs["kind"] == consumer_module.observability.SPAN_KIND_CONSUMER
    span.set_attribute.assert_called_once_with("messaging.event.result", "applied")


def test_process_message_without_trace_headers_uses_empty_carrier(consumer_ctx, valid_event):
    consumer, _, _, _, lua_script, _ = consumer_ctx
    lua_script.return_value = "OK"

    msg = _message_of(valid_event)
    span_context = MagicMock()
    span_context.__enter__.return_value = Mock()
    span_context.__exit__.return_value = False
    tracer = Mock()
    tracer.start_as_current_span.return_value = span_context
    consumer.tracer = tracer

    with patch("consumer.observability.extract_context", return_value=None) as extract_context:
        result = consumer.process_message(msg)

    assert result == ProcessingResult.APPLIED
    extract_context.assert_called_once_with({})


def test_redis_connection_error_during_update_routes_to_retry(consumer_ctx, valid_event):
    """
    Regression: Redis ConnectionError during Lua upsert -> TRANSIENT_FAILURE, routes to retry.

    Redis unavailable must be classified as transient so the message is routed to retry
    topic, not DLQ. Protects retry tier / DLQ safety contract.
    """
    consumer, kafka_consumer, kafka_producer, _, lua_script, _ = consumer_ctx
    import redis

    lua_script.side_effect = redis.ConnectionError("Connection refused")

    result = consumer.process_message(_message_of(valid_event))

    assert result == ProcessingResult.TRANSIENT_FAILURE
    kafka_producer.send.assert_called_once()
    kafka_consumer.commit.assert_called_once()


def test_dlq_payload_contains_canonical_schema_fields(consumer_ctx):
    """
    Regression: DLQ payload includes canonical fields for triage and structured logging.

    Protects docs/LOGGING_SCHEMA.md contract: dlq_id, original_topic, original_partition,
    original_offset (and dlq_reason) must be present for replay/triage tooling.
    """
    consumer, kafka_consumer, kafka_producer, redis_client, lua_script, _ = consumer_ctx
    import config

    malformed = {"user_id": "u1", "item_id": "i1"}
    msg = _message_of(malformed)
    msg.topic = "scalestyle.clicks"
    msg.partition = 3
    msg.offset = 100
    msg.key = b"key-99"
    redis_client.exists.return_value = 0
    redis_client.set.return_value = True

    result = consumer.process_message(msg)

    assert result == ProcessingResult.PERMANENT_FAILURE
    assert kafka_producer.send.call_args.args[0] == config.KAFKA_DLQ_TOPIC
    payload = kafka_producer.send.call_args.kwargs["value"]
    assert "dlq_id" in payload
    assert payload["original_topic"] == "scalestyle.clicks"
    assert payload["original_partition"] == 3
    assert payload["original_offset"] == 100
    assert "dlq_reason" in payload


def test_retry_reroute_log_uses_canonical_snake_case_fields(consumer_ctx, valid_event, caplog):
    consumer, kafka_consumer, kafka_producer, _, lua_script, _ = consumer_ctx
    import logging

    traceparent = "00-0123456789abcdef0123456789abcdef-0123456789abcdef-01"
    retry_msg = _message_of(valid_event)
    retry_msg.headers = [("traceparent", traceparent.encode("utf-8"))]
    lua_script.side_effect = RuntimeError("transient redis error")

    with caplog.at_level(logging.WARNING):
        result = consumer.process_message(retry_msg)

    assert result == ProcessingResult.TRANSIENT_FAILURE
    # Canonical snake_case fields (docs/LOGGING_SCHEMA.md)
    assert "event_id=evt-123" in caplog.text
    assert "trace_id=0123456789abcdef0123456789abcdef" in caplog.text
    assert "topic=scalestyle.clicks" in caplog.text
    assert "partition=0" in caplog.text
    assert "offset=42" in caplog.text
    assert "retry_count=" in caplog.text


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
