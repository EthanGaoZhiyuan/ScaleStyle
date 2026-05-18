"""
RetryRouter — routes processed Kafka messages through the tiered retry topology
and Dead-Letter Queue.

Extracted from EventConsumer._process_message_internal and the supporting
helper methods.  All retry-tier counts, topic names, delay values, DLQ
semantics, offset-commit contracts, and sys.exit behaviour are preserved
exactly from the original consumer.py implementation.

Retry topology (preserved verbatim):
  primary → retry-1s → retry-10s → retry-60s → DLQ

DLQ semantics (preserved verbatim):
  - at-least-once delivery
  - Kafka broker ack is source-of-truth for "sent"
  - Redis dlq:sent marker is advisory only
  - marker failure after broker ack MUST NOT downgrade send status
  - replay/triage tooling MUST suppress duplicates by dlq_id

Commit semantics (preserved verbatim):
  - APPLIED / DUPLICATE → commit_safe=True, strict_commit_required=False
  - PERMANENT_FAILURE: DLQ sent/dup → commit_safe=True, strict=True
  - PERMANENT_FAILURE: DLQ failed   → commit_safe=False, strict=False
  - TRANSIENT_FAILURE: retry sent   → commit_safe=True, strict=True
  - TRANSIENT_FAILURE: retry failed → commit_safe=False, strict=False
  - TRANSIENT_FAILURE: max retries exceeded + DLQ sent → commit_safe=True, strict=True
  - Deferred (tier delay not met)  → commit_safe=False, strict=False
"""

from __future__ import annotations

import logging
import time
from typing import Any, Dict, Optional, Tuple

import redis

import config
import metrics
from kafka_utils import make_offset_and_metadata
from models import ProcessingResult

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Terminal-state exception classes (re-exported for backward compat)
# ---------------------------------------------------------------------------


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
    safely idempotent: the retry consumer's event_id Lua duplicate suppression
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


# ---------------------------------------------------------------------------
# RetryRouter
# ---------------------------------------------------------------------------


class RetryRouter:
    """Routes a processed message through retry / DLQ and manages offset commits.

    Constructor parameters
    ----------------------
    kafka_producer :
        KafkaProducer used for retry-tier and DLQ writes.
    redis_client :
        redis.Redis instance used for DLQ advisory marker (dlq:sent:*).
    feature_update_handler :
        Object with an ``update_features(event) -> ProcessingResult`` method.
    kafka_consumer :
        KafkaConsumer used for immediate per-message offset commits.
    """

    def __init__(
        self,
        *,
        kafka_producer,
        redis_client,
        feature_update_handler,
        kafka_consumer,
    ) -> None:
        self._kafka_producer = kafka_producer
        self._redis_client = redis_client
        self._feature_update_handler = feature_update_handler
        self._kafka_consumer = kafka_consumer

        # Derived lookup tables from config (preserved from consumer.py __init__)
        self._retry_topic_to_delay_seconds: Dict[str, float] = {
            topic: delay_seconds for _, topic, delay_seconds in config.KAFKA_RETRY_TIERS
        }
        self._retry_topic_to_tier_name: Dict[str, str] = {
            topic: tier_name for tier_name, topic, _ in config.KAFKA_RETRY_TIERS
        }

        # Paused partitions dict — shared with KafkaConsumerLoop via reference.
        # EventConsumer exposes self.paused_partitions which points here.
        self.paused_partitions: Dict[Any, float] = {}

    # ------------------------------------------------------------------
    # Public routing entry point
    # ------------------------------------------------------------------

    def process_message_internal(
        self, message, commit_immediately: bool
    ) -> Tuple[ProcessingResult, bool, bool]:
        """Process one Kafka message with proper error handling and offset management.

        Strategy:
        - APPLIED/DUPLICATE: safe to commit source offset
        - PERMANENT_FAILURE: safe to commit only if DLQ send is acknowledged
        - TRANSIENT_FAILURE: safe to commit only if retry copy (or DLQ fallback) is acknowledged

        Does NOT block the main loop with sleep() on transient failures.

        Args:
            message: Kafka message
            commit_immediately: if True, commit the source offset inline rather
                than deferring to the batch-commit path.

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

        # Check retry tier delay — if not ready, only this retry-tier partition is paused.
        if self._respect_retry_tier_delay(message, event, retry_count):
            return ProcessingResult.TRANSIENT_FAILURE, False, False

        result = self._feature_update_handler.update_features(event)

        if result == ProcessingResult.APPLIED:
            # Success — commit offset and clear retry tracking.
            if commit_immediately:
                try:
                    self._commit_message(message)
                except Exception as e:
                    logger.error(
                        "CRITICAL: Successfully processed but commit failed. "
                        "Will reprocess: topic=%s, partition=%s, offset=%s, error=%s",
                        message.topic,
                        message.partition,
                        message.offset,
                        e,
                    )
                    return result, False, False

            return result, True, False

        elif result == ProcessingResult.DUPLICATE:
            if commit_immediately:
                try:
                    self._commit_message(message)
                except Exception as e:
                    logger.warning(
                        "Duplicate detected but commit failed: topic=%s, "
                        "partition=%s, offset=%s, error=%s",
                        message.topic,
                        message.partition,
                        message.offset,
                        e,
                    )
            logger.debug(
                "Duplicate event processed topic=%s partition=%s offset=%s",
                message.topic,
                message.partition,
                message.offset,
            )
            return result, True, False

        elif result == ProcessingResult.PERMANENT_FAILURE:
            event_id = (
                event.get("event_id", "unknown")
                if isinstance(event, dict)
                else "unknown"
            )
            logger.error(
                "🚨 Permanent failure detected: topic=%s, partition=%s, "
                "offset=%s, event_id=%s, trace_id=%s",
                message.topic,
                message.partition,
                message.offset,
                event_id,
                trace_ctx["trace_id"],
            )
            metrics.events_failed_total.labels(failure_type="permanent").inc()
            dlq_result = self._send_to_dlq(
                message, "permanent_failure", "Invalid schema or data format"
            )
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
                        metrics.events_commit_failed_total.labels(
                            reason="dlq_send_success"
                        ).inc()
                        raise DlqPublishedCommitFailedError(
                            f"DLQ published but source commit failed (permanent_failure): "
                            f"topic={message.topic} partition={message.partition} "
                            f"offset={message.offset}"
                        ) from e
                return result, True, True
            else:
                logger.error(
                    "DLQ send failed; offset not committed topic=%s partition=%s offset=%s",
                    message.topic,
                    message.partition,
                    message.offset,
                )
                metrics.events_failed_total.labels(failure_type="dlq_failed").inc()
            return result, False, False

        elif result == ProcessingResult.TRANSIENT_FAILURE:
            next_retry = retry_count + 1

            if next_retry <= config.MAX_RETRIES:
                event_id = (
                    event.get("event_id", "unknown")
                    if isinstance(event, dict)
                    else "unknown"
                )
                try:
                    self._send_to_retry_topic(
                        message,
                        retry_count=next_retry,
                        reason="transient_failure",
                    )
                except Exception as e:
                    retry_tier_name, retry_topic, _ = self._retry_tier_for_count(
                        next_retry
                    )
                    logger.error(
                        "Retry routing failed topic=%s partition=%s offset=%s "
                        "retry_count=%s retry_topic=%s retry_tier=%s trace_id=%s err=%s",
                        message.topic,
                        message.partition,
                        message.offset,
                        next_retry,
                        retry_topic,
                        retry_tier_name,
                        trace_ctx["trace_id"],
                        e,
                    )
                    metrics.kafka_produce_total.labels(
                        topic=retry_tier_name, status="error"
                    ).inc()
                    metrics.events_failed_total.labels(failure_type="transient").inc()
                    return result, False, False

                if commit_immediately:
                    try:
                        self._commit_message(message)
                    except Exception as e:
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
                    "Transient failure rerouted topic=%s partition=%s offset=%s "
                    "event_id=%s retry_count=%s max_retries=%s trace_id=%s",
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
                # Max retries exceeded → DLQ
                event_id = (
                    event.get("event_id", "unknown")
                    if isinstance(event, dict)
                    else "unknown"
                )
                logger.error(
                    "🚨 Max retries exceeded (%s): topic=%s, partition=%s, offset=%s, "
                    "event_id=%s, trace_id=%s",
                    config.MAX_RETRIES,
                    message.topic,
                    message.partition,
                    message.offset,
                    event_id,
                    trace_ctx["trace_id"],
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
                            metrics.events_commit_failed_total.labels(
                                reason="dlq_send_success"
                            ).inc()
                            raise DlqPublishedCommitFailedError(
                                f"DLQ published but source commit failed (max_retries_exceeded): "
                                f"topic={message.topic} partition={message.partition} "
                                f"offset={message.offset}"
                            ) from e
                    return result, True, True
                else:
                    logger.error(
                        "DLQ send failed after max retries; offset not committed "
                        "topic=%s partition=%s offset=%s",
                        message.topic,
                        message.partition,
                        message.offset,
                    )
                    metrics.kafka_produce_total.labels(
                        topic="dlq", status="error"
                    ).inc()
                    metrics.events_failed_total.labels(failure_type="dlq_failed").inc()
                return result, False, False

        return result, False, False

    # ------------------------------------------------------------------
    # Retry tier helpers
    # ------------------------------------------------------------------

    def _retry_tier_for_count(self, retry_count: int) -> tuple:
        if retry_count <= 0 or retry_count > len(config.KAFKA_RETRY_TIERS):
            raise ValueError(f"retry_count {retry_count} out of configured tier range")
        return config.KAFKA_RETRY_TIERS[retry_count - 1]

    def _send_to_retry_topic(self, message, retry_count: int, reason: str) -> None:
        """Requeue message into the next retry tier topic.

        Blocks until broker ack is received to avoid committing source offset
        before retry message is durably written.
        """
        retry_tier_name, retry_topic, delay_seconds = self._retry_tier_for_count(
            retry_count
        )
        base_event = (
            message.value
            if isinstance(message.value, dict)
            else {"payload": message.value}
        )
        event = dict(base_event)
        event["_retry_meta"] = {
            "retry_count": retry_count,
            "retry_tier": retry_tier_name,
            "retry_topic": retry_topic,
            "delay_seconds": delay_seconds,
            "routed_at_ts": time.time(),
            "reason": reason,
        }
        retry_key = (
            event.get("event_id")
            or message.key
            or f"{message.topic}:{message.partition}:{message.offset}"
        )
        headers = self._forward_trace_headers(message)
        headers.append((config.KAFKA_RETRY_HEADER, str(retry_count).encode("utf-8")))
        future = self._kafka_producer.send(
            retry_topic,
            key=retry_key,
            value=event,
            headers=headers,
        )
        metadata = future.get(timeout=5)
        metrics.kafka_produce_total.labels(
            topic=retry_tier_name, status="success"
        ).inc()
        metrics.events_retry_routed_total.labels(retry_count=str(retry_count)).inc()
        metrics.events_retry_tier_routed_total.labels(
            retry_count=str(retry_count),
            retry_tier=retry_tier_name,
        ).inc()
        logger.warning(
            "Routed to retry retry_tier=%s retry_topic=%s retry_count=%s "
            "delay_seconds=%.1f reason=%s topic=%s partition=%s offset=%s "
            "retry_partition=%s retry_offset=%s",
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

    # ------------------------------------------------------------------
    # DLQ helpers
    # ------------------------------------------------------------------

    def _send_to_dlq(self, message, reason: str, error: str) -> str:
        """Send poisoned message to Dead Letter Queue with stable dlq_id key.

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
                (
                    (message.value or {}).get("event_id")
                    if isinstance(message.value, dict)
                    else None
                ),
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

            future = self._kafka_producer.send(
                config.KAFKA_DLQ_TOPIC,
                key=dlq_id,
                value=dlq_payload,
                headers=self._forward_trace_headers(message),
            )
            metadata = future.get(timeout=5)

            logger.info(
                "DLQ sent dlq_id=%s dlq_partition=%s dlq_offset=%s "
                "topic=%s partition=%s offset=%s",
                dlq_id,
                metadata.partition,
                metadata.offset,
                message.topic,
                message.partition,
                message.offset,
            )

            metrics.kafka_produce_total.labels(topic="dlq", status="success").inc()
            if not self._mark_dlq_dispatched(dlq_id):
                logger.warning(
                    "DLQ marker persistence failed after broker ack; proceeding "
                    "with at-least-once semantics dlq_id=%s topic=%s partition=%s offset=%s",
                    dlq_id,
                    message.topic,
                    message.partition,
                    message.offset,
                )
            metrics.events_dlq_sent_total.labels(reason=reason).inc()
            return "sent"
        except Exception as e:
            logger.error("Failed to send message to DLQ dlq_id=%s err=%s", dlq_id, e)
            metrics.kafka_produce_total.labels(topic="dlq", status="error").inc()
            return "failed"

    def _build_dlq_id(self, message) -> str:
        """Build stable id used as DLQ idempotency key."""
        event = message.value or {}
        event_id = event.get("event_id") if isinstance(event, dict) else None
        if event_id:
            return str(event_id)
        return f"{message.topic}:{message.partition}:{message.offset}"

    def _is_dlq_dispatched(self, dlq_id: str) -> bool:
        """Check whether advisory DLQ marker already exists (fail-open)."""
        try:
            exists = bool(self._redis_client.exists(f"dlq:sent:{dlq_id}"))
            metrics.record_redis_success()
            metrics.redis_operations_total.labels(
                operation="dlq_check", status="success"
            ).inc()
            return exists
        except redis.TimeoutError as e:
            metrics.record_redis_error()
            logger.warning(
                "Redis timeout during DLQ check: dlq_id=%s timeout=%ss error=%s",
                dlq_id,
                config.REDIS_SOCKET_TIMEOUT_SEC,
                e,
            )
            metrics.redis_operations_total.labels(
                operation="dlq_check", status="timeout"
            ).inc()
            return False
        except redis.ConnectionError as e:
            metrics.record_redis_error()
            logger.warning(
                "Redis connection error during DLQ check: dlq_id=%s error=%s",
                dlq_id,
                e,
            )
            metrics.redis_operations_total.labels(
                operation="dlq_check", status="connection_error"
            ).inc()
            return False
        except Exception as e:
            metrics.record_redis_error()
            logger.error(
                "DLQ marker key read failed dlq_id=%s error_type=%s err=%s",
                dlq_id,
                e.__class__.__name__,
                e,
            )
            metrics.redis_operations_total.labels(
                operation="dlq_check", status="error"
            ).inc()
            return False

    def _mark_dlq_dispatched(self, dlq_id: str) -> bool:
        """Persist advisory DLQ dispatch marker in Redis (NX).

        Returns True only for first dispatch attempt.  Best-effort; never blocks
        DLQ delivery.
        """
        dedupe_key = f"dlq:sent:{dlq_id}"
        try:
            marked = self._redis_client.set(
                dedupe_key,
                "1",
                nx=True,
                ex=config.DLQ_DEDUPE_TTL_SECONDS,
            )
            metrics.record_redis_success()
            if marked:
                metrics.redis_operations_total.labels(
                    operation="dlq_mark", status="success"
                ).inc()
            else:
                metrics.redis_operations_total.labels(
                    operation="dlq_mark", status="already_exists"
                ).inc()
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
            metrics.redis_operations_total.labels(
                operation="dlq_mark", status="timeout"
            ).inc()
            return False
        except redis.ConnectionError as e:
            metrics.record_redis_error()
            logger.error(
                "Redis connection error marking DLQ sent: dlq_id=%s error=%s",
                dlq_id,
                e,
            )
            metrics.redis_operations_total.labels(
                operation="dlq_mark", status="connection_error"
            ).inc()
            return False
        except Exception as e:
            metrics.record_redis_error()
            logger.error(
                "DLQ marker key set failed dlq_id=%s error_type=%s err=%s",
                dlq_id,
                e.__class__.__name__,
                e,
            )
            metrics.redis_operations_total.labels(
                operation="dlq_mark", status="error"
            ).inc()
            return False

    # ------------------------------------------------------------------
    # Retry delay enforcement
    # ------------------------------------------------------------------

    def _respect_retry_tier_delay(
        self, message, event: Dict[str, Any], retry_count: int
    ) -> bool:
        """Honor the fixed delay associated with a retry topic tier.

        Returns True if message should be skipped (deferred, not yet ready),
        False if message is ready to process.
        """
        from kafka import TopicPartition

        if not config.RETRY_ENFORCE_DELAY:
            return False

        if message.topic not in self._retry_topic_to_delay_seconds:
            return False

        delay_seconds = self._retry_topic_to_delay_seconds[message.topic]
        retry_meta = (
            event.get("_retry_meta")
            if isinstance(event.get("_retry_meta"), dict)
            else {}
        )
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
        pause_duration = min(remaining, 60.0)
        resume_at = now + pause_duration

        try:
            if tp not in self.paused_partitions:
                self._kafka_consumer.pause(tp)
                self.paused_partitions[tp] = resume_at
                metrics.retry_partitions_paused.set(len(self.paused_partitions))
                logger.info(
                    "Paused retry partition topic=%s tier=%s partition=%s offset=%s "
                    "retry=%s resume_in=%.1fs (total_paused=%d)",
                    message.topic,
                    self._retry_topic_to_tier_name.get(message.topic, "unknown"),
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
                "Failed to defer retry message topic=%s tier=%s partition=%s "
                "offset=%s err=%s",
                message.topic,
                self._retry_topic_to_tier_name.get(message.topic, "unknown"),
                message.partition,
                message.offset,
                e,
            )
            return False

        metrics.retry_delay_seconds.observe(remaining)
        return True

    # ------------------------------------------------------------------
    # Trace context helpers
    # ------------------------------------------------------------------

    def _extract_trace_context(self, message) -> Dict[str, Optional[str]]:
        """Read W3C trace context headers from Kafka message."""
        traceparent = self._extract_header_value(
            message, config.KAFKA_TRACEPARENT_HEADER
        )
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

    def _forward_trace_headers(self, message) -> list:
        """Build Kafka headers to preserve trace context on retry/DLQ hops."""
        headers = []
        trace_ctx = self._extract_trace_context(message)
        if trace_ctx["traceparent"]:
            headers.append(
                (
                    config.KAFKA_TRACEPARENT_HEADER,
                    trace_ctx["traceparent"].encode("utf-8"),
                )
            )
        if trace_ctx["tracestate"]:
            headers.append(
                (
                    config.KAFKA_TRACESTATE_HEADER,
                    trace_ctx["tracestate"].encode("utf-8"),
                )
            )
        return headers

    def _extract_retry_count(
        self, message, event: Optional[Dict[str, Any]] = None
    ) -> int:
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
        retry_meta = (
            payload.get("_retry_meta")
            if isinstance(payload.get("_retry_meta"), dict)
            else {}
        )
        try:
            retry_from_payload = retry_meta.get("retry_count")
            return int(retry_from_payload) if retry_from_payload is not None else 0
        except Exception:
            return 0

    # ------------------------------------------------------------------
    # Offset commit helpers
    # ------------------------------------------------------------------

    def _commit_message(self, message) -> None:
        """Commit the offset for this specific message.

        Raises exception on commit failure so the caller can handle it.
        """
        from kafka import TopicPartition

        tp = TopicPartition(message.topic, message.partition)
        offsets = {tp: make_offset_and_metadata(message.offset + 1)}
        try:
            self._kafka_consumer.commit(offsets=offsets)
            logger.debug(
                "Committed offset: topic=%s, partition=%s, offset=%s",
                message.topic,
                message.partition,
                message.offset + 1,
            )
        except Exception as e:
            logger.error(
                "Offset commit FAILED: topic=%s, partition=%s, offset=%s, error=%s",
                message.topic,
                message.partition,
                message.offset,
                e,
            )
            raise

    # ------------------------------------------------------------------
    # Terminal commit-uncertainty logging
    # ------------------------------------------------------------------

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
