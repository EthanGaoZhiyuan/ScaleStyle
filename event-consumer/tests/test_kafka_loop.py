"""Tests for KafkaConsumerLoop."""

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
    from kafka import TopicPartition
except Exception:
    kafka_stub = types.ModuleType("kafka")

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
        def __init__(self, offset, metadata, leader_epoch=None):
            self.offset = offset

    kafka_stub.TopicPartition = _TopicPartition
    kafka_stub.OffsetAndMetadata = _OffsetAndMetadata
    sys.modules["kafka"] = kafka_stub
    from kafka import TopicPartition

from models import ProcessingResult
from retry_router import RetryPublishedCommitFailedError
from kafka_loop import KafkaConsumerLoop

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _message_of(event, topic="scalestyle.clicks", partition=0, offset=10):
    msg = Mock()
    msg.value = event
    msg.topic = topic
    msg.partition = partition
    msg.offset = offset
    msg.headers = []
    return msg


def _valid_event(event_id="evt-1"):
    return {
        "event_id": event_id,
        "user_id": "user-1",
        "item_id": "item-1",
        "timestamp": "2026-03-07T10:00:00Z",
    }


def _make_loop(kafka_consumer, retry_router):
    loop_alive = [True]
    event_count = [0]
    start_time = [0.0]
    last_poll_ts = [None]

    return (
        KafkaConsumerLoop(
            kafka_consumer=kafka_consumer,
            retry_router=retry_router,
            paused_partitions={},
            get_loop_alive=lambda: loop_alive[0],
            set_loop_alive=lambda v: loop_alive.__setitem__(0, v),
            get_event_count=lambda: event_count[0],
            add_event_count=lambda n: event_count.__setitem__(0, event_count[0] + n),
            get_start_time=lambda: start_time[0],
            set_last_poll_ts=lambda ts: last_poll_ts.__setitem__(0, ts),
            metrics_server=None,
        ),
        loop_alive,
    )


def _make_metrics_mock():
    m = MagicMock()
    m.events_commit_failed_total.labels.return_value = m.events_commit_failed_total
    m.consumer_terminations_total.labels.return_value = m.consumer_terminations_total
    m.kafka_consumer_lag.labels.return_value = m.kafka_consumer_lag
    m.retry_partitions_paused = MagicMock()
    m.consumer_health = MagicMock()
    return m


# ---------------------------------------------------------------------------
# commit called when commit_safe=True
# ---------------------------------------------------------------------------


def test_commit_called_when_commit_safe(tmp_path):
    kafka_consumer = Mock()
    retry_router = Mock()

    msg = _message_of(_valid_event(), offset=10)
    tp = TopicPartition(msg.topic, msg.partition)

    kafka_consumer.poll.side_effect = [
        {tp: [msg]},
        KeyboardInterrupt(),
    ]
    kafka_consumer.assignment.return_value = set()

    retry_router.process_message_internal.return_value = (
        ProcessingResult.APPLIED,
        True,
        False,
    )

    loop, _ = _make_loop(kafka_consumer, retry_router)

    with patch("kafka_loop.metrics") as mock_metrics:
        mock_metrics.events_commit_failed_total.labels.return_value = (
            mock_metrics.events_commit_failed_total
        )
        mock_metrics.consumer_terminations_total.labels.return_value = (
            mock_metrics.consumer_terminations_total
        )
        mock_metrics.kafka_consumer_lag.labels.return_value = (
            mock_metrics.kafka_consumer_lag
        )
        mock_metrics.retry_partitions_paused = MagicMock()
        mock_metrics.consumer_health = MagicMock()
        loop.run()

    kafka_consumer.commit.assert_called_once()
    committed = kafka_consumer.commit.call_args.kwargs["offsets"]
    assert tp in committed
    assert committed[tp].offset == 11  # msg.offset + 1


# ---------------------------------------------------------------------------
# no commit when commit_safe=False
# ---------------------------------------------------------------------------


def test_no_commit_when_commit_safe_false():
    kafka_consumer = Mock()
    retry_router = Mock()

    msg = _message_of(_valid_event(), offset=10)
    tp = TopicPartition(msg.topic, msg.partition)

    kafka_consumer.poll.side_effect = [
        {tp: [msg]},
        KeyboardInterrupt(),
    ]
    kafka_consumer.assignment.return_value = set()

    retry_router.process_message_internal.return_value = (
        ProcessingResult.TRANSIENT_FAILURE,
        False,
        False,
    )

    loop, _ = _make_loop(kafka_consumer, retry_router)

    with patch("kafka_loop.metrics") as mock_metrics:
        mock_metrics.events_commit_failed_total.labels.return_value = (
            mock_metrics.events_commit_failed_total
        )
        mock_metrics.consumer_terminations_total.labels.return_value = (
            mock_metrics.consumer_terminations_total
        )
        mock_metrics.kafka_consumer_lag.labels.return_value = (
            mock_metrics.kafka_consumer_lag
        )
        mock_metrics.retry_partitions_paused = MagicMock()
        mock_metrics.consumer_health = MagicMock()
        loop.run()

    kafka_consumer.commit.assert_not_called()


# ---------------------------------------------------------------------------
# Commit stops at first unsafe offset
# ---------------------------------------------------------------------------


def test_commit_stops_at_first_unsafe_offset():
    kafka_consumer = Mock()
    retry_router = Mock()

    msg1 = _message_of(_valid_event("evt-1"), offset=10)
    msg2 = _message_of(_valid_event("evt-2"), offset=11)
    tp = TopicPartition(msg1.topic, msg1.partition)

    kafka_consumer.poll.side_effect = [
        {tp: [msg1, msg2]},
        KeyboardInterrupt(),
    ]
    kafka_consumer.assignment.return_value = set()

    retry_router.process_message_internal.side_effect = [
        (ProcessingResult.APPLIED, True, False),  # msg1: safe
        (ProcessingResult.TRANSIENT_FAILURE, False, False),  # msg2: unsafe
    ]

    loop, _ = _make_loop(kafka_consumer, retry_router)

    with patch("kafka_loop.metrics") as mock_metrics:
        mock_metrics.events_commit_failed_total.labels.return_value = (
            mock_metrics.events_commit_failed_total
        )
        mock_metrics.consumer_terminations_total.labels.return_value = (
            mock_metrics.consumer_terminations_total
        )
        mock_metrics.kafka_consumer_lag.labels.return_value = (
            mock_metrics.kafka_consumer_lag
        )
        mock_metrics.retry_partitions_paused = MagicMock()
        mock_metrics.consumer_health = MagicMock()
        loop.run()

    kafka_consumer.commit.assert_called_once()
    committed = kafka_consumer.commit.call_args.kwargs["offsets"]
    assert committed[tp].offset == 11  # msg1.offset + 1, not msg2


# ---------------------------------------------------------------------------
# shutdown on fatal error (RetryPublishedCommitFailedError)
# ---------------------------------------------------------------------------


def test_shutdown_on_fatal_error():
    kafka_consumer = Mock()
    retry_router = Mock()

    msg = _message_of(_valid_event(), offset=10)
    tp = TopicPartition(msg.topic, msg.partition)

    kafka_consumer.poll.side_effect = [{tp: [msg]}]
    kafka_consumer.assignment.return_value = set()

    retry_router.process_message_internal.side_effect = RetryPublishedCommitFailedError(
        "retry published but commit failed"
    )

    loop, loop_alive = _make_loop(kafka_consumer, retry_router)

    with patch("kafka_loop.metrics") as mock_metrics:
        mock_metrics.events_commit_failed_total.labels.return_value = (
            mock_metrics.events_commit_failed_total
        )
        mock_metrics.consumer_terminations_total.labels.return_value = (
            mock_metrics.consumer_terminations_total
        )
        mock_metrics.kafka_consumer_lag.labels.return_value = (
            mock_metrics.kafka_consumer_lag
        )
        mock_metrics.retry_partitions_paused = MagicMock()
        mock_metrics.consumer_health = MagicMock()
        with pytest.raises(SystemExit) as exc_info:
            loop.run()

    assert exc_info.value.code == 1
    assert loop_alive[0] is False


# ---------------------------------------------------------------------------
# Two partitions: highest safe offset per partition
# ---------------------------------------------------------------------------


def test_highest_safe_offset_per_partition():
    kafka_consumer = Mock()
    retry_router = Mock()

    msg1 = _message_of(_valid_event("e1"), offset=10)
    msg2 = _message_of(_valid_event("e2"), offset=11)
    msg3 = _message_of(_valid_event("e3"), offset=7)
    msg3.partition = 1

    tp0 = TopicPartition("scalestyle.clicks", 0)
    tp1 = TopicPartition("scalestyle.clicks", 1)

    kafka_consumer.poll.side_effect = [
        {tp0: [msg1, msg2], tp1: [msg3]},
        KeyboardInterrupt(),
    ]
    kafka_consumer.assignment.return_value = set()

    retry_router.process_message_internal.side_effect = [
        (ProcessingResult.APPLIED, True, False),
        (ProcessingResult.APPLIED, True, False),
        (ProcessingResult.APPLIED, True, False),
    ]

    loop, _ = _make_loop(kafka_consumer, retry_router)

    with patch("kafka_loop.metrics") as mock_metrics:
        mock_metrics.events_commit_failed_total.labels.return_value = (
            mock_metrics.events_commit_failed_total
        )
        mock_metrics.consumer_terminations_total.labels.return_value = (
            mock_metrics.consumer_terminations_total
        )
        mock_metrics.kafka_consumer_lag.labels.return_value = (
            mock_metrics.kafka_consumer_lag
        )
        mock_metrics.retry_partitions_paused = MagicMock()
        mock_metrics.consumer_health = MagicMock()
        loop.run()

    kafka_consumer.commit.assert_called_once()
    committed = kafka_consumer.commit.call_args.kwargs["offsets"]
    assert committed[tp0].offset == 12  # msg2.offset + 1
    assert committed[tp1].offset == 8  # msg3.offset + 1
