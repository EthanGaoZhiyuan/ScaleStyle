"""
KafkaConsumerLoop — the main poll-process-commit loop.

Extracted from EventConsumer.run() to allow independent testing and to keep
consumer.py focused on dependency wiring.

All offset-commit semantics, batch processing logic, partition pause/resume,
fail-fast behaviour on RetryPublishedCommitFailedError /
DlqPublishedCommitFailedError, and sys.exit() semantics are preserved exactly
from the original consumer.py implementation.

Critical invariants (preserved verbatim):
  - Commit called once per poll batch, only if any partition had commit_safe=True.
  - Commit covers only the highest safe offset per partition; unsafe offsets are
    excluded so we never skip a failed message.
  - strict_commit_required: if any message in the batch set this flag and the
    batch commit fails, the loop raises RetryPublishedCommitFailedError so the
    process exits and Kubernetes restarts it cleanly.
  - RetryPublishedCommitFailedError / DlqPublishedCommitFailedError propagate out
    of the inner for-loop and are caught in the outer try/except which calls
    sys.exit(1) after marking loop_alive=False.
  - KeyboardInterrupt causes a graceful shutdown (no sys.exit).
"""

from __future__ import annotations

import logging
import sys
import time
from typing import Any, Callable, Dict, Optional

from kafka import TopicPartition, OffsetAndMetadata

import config
import metrics
from kafka_utils import make_offset_and_metadata
from retry_router import RetryPublishedCommitFailedError, DlqPublishedCommitFailedError

logger = logging.getLogger(__name__)


class KafkaConsumerLoop:
    """Drives the Kafka poll-process-commit loop.

    Constructor parameters
    ----------------------
    kafka_consumer :
        KafkaConsumer instance.
    retry_router :
        RetryRouter (or any object with a ``process_message_internal`` method
        matching the (message, commit_immediately) → (result, bool, bool) contract).
    paused_partitions : dict
        Shared reference to the paused-partition dict owned by RetryRouter.
        The loop uses it for resume checks.
    get_loop_alive : Callable[[], bool]
        Returns the current loop_alive flag (owned by EventConsumer).
    set_loop_alive : Callable[[bool], None]
        Sets the loop_alive flag on EventConsumer.
    get_event_count : Callable[[], int]
    add_event_count : Callable[[int], None]
    get_start_time : Callable[[], float]
    set_last_poll_ts : Callable[[float], None]
    process_message_fn : Callable | None
        If provided, called instead of ``retry_router.process_message_internal``.
        EventConsumer passes ``self._process_message_internal`` here so that tests
        which assign ``consumer._process_message_internal = mock`` automatically
        take effect in the loop without extra patching.
    metrics_server :
        MetricsServer instance (may be None in tests).
    """

    def __init__(
        self,
        *,
        kafka_consumer,
        retry_router,
        paused_partitions: Dict[Any, float],
        get_loop_alive: Callable[[], bool],
        set_loop_alive: Callable[[bool], None],
        get_event_count: Callable[[], int],
        add_event_count: Callable[[int], None],
        get_start_time: Callable[[], float],
        set_last_poll_ts: Callable[[float], None],
        process_message_fn: Optional[Callable] = None,
        metrics_server=None,
    ) -> None:
        self._consumer = kafka_consumer
        self._retry_router = retry_router
        self._process_message_fn = process_message_fn
        self._paused_partitions = paused_partitions
        self._get_loop_alive = get_loop_alive
        self._set_loop_alive = set_loop_alive
        self._get_event_count = get_event_count
        self._add_event_count = add_event_count
        self._get_start_time = get_start_time
        self._set_last_poll_ts = set_last_poll_ts
        self._metrics_server = metrics_server

    # ------------------------------------------------------------------
    # Main loop
    # ------------------------------------------------------------------

    def run(self) -> None:
        """Main consumer loop with explicit processing state handling."""
        if config.CONSUMER_MODE == "primary":
            actual_topics = [config.KAFKA_TOPIC]
        else:
            actual_topics = list(config.KAFKA_RETRY_TOPICS)

        logger.info("[START] Event Consumer started")
        logger.info("[INFO] Mode: %s", config.CONSUMER_MODE)
        logger.info("[INFO] Consuming from: %s", actual_topics)
        logger.info(
            "[INFO] Updating Redis: %s:%s", config.REDIS_HOST, config.REDIS_PORT
        )

        if config.KAFKA_AUTO_OFFSET_RESET == "latest":
            logger.warning(
                "[WARN] KAFKA_AUTO_OFFSET_RESET=latest: new consumer groups will skip "
                "historical messages. This may cause data loss on first deployment or "
                "after offset deletion. Consider using 'earliest' for production deployments."
            )

        logger.info("-" * 60)

        message_count = 0
        last_resume_check = time.time()
        resume_check_interval = 1.0
        last_metrics_log = time.time()
        metrics_log_interval = 60.0
        last_lag_update = time.time()
        lag_update_interval_s = 30.0

        try:
            while True:
                message_batch = self._consumer.poll(
                    timeout_ms=int(resume_check_interval * 1000),
                    max_records=config.KAFKA_POLL_MAX_RECORDS,
                )
                now = time.time()
                self._set_last_poll_ts(now)

                if now - last_resume_check >= resume_check_interval:
                    self._resume_ready_partitions()
                    last_resume_check = now

                if now - last_lag_update >= lag_update_interval_s:
                    self._update_kafka_lag()
                    last_lag_update = now

                if (
                    now - last_metrics_log >= metrics_log_interval
                    and self._paused_partitions
                ):
                    logger.info(
                        "Retry backoff status: %d partition(s) currently paused",
                        len(self._paused_partitions),
                    )
                    last_metrics_log = now

                # Process messages and track the highest safe offset per partition.
                pending_commits: Dict[TopicPartition, OffsetAndMetadata] = {}
                strict_commit_required = False
                for tp, messages in message_batch.items():
                    highest_safe_offset = None
                    for message in messages:
                        try:
                            _process_fn = (
                                self._process_message_fn
                                if self._process_message_fn is not None
                                else self._retry_router.process_message_internal
                            )
                            _, commit_safe, strict_required = _process_fn(
                                message,
                                commit_immediately=False,
                            )
                            if strict_required:
                                strict_commit_required = True
                            if commit_safe:
                                highest_safe_offset = message.offset
                            else:
                                # Stop at first unsafe offset per partition.
                                break
                            message_count += 1
                            self._add_event_count(1)
                            event_count = self._get_event_count()
                            if event_count % config.LOG_INTERVAL == 0:
                                elapsed = time.time() - self._get_start_time()
                                rate = event_count / elapsed if elapsed > 0 else 0
                                logger.info(
                                    "📈 Stats: processed=%s, rate=%.1f events/sec, uptime=%.1fs",
                                    event_count,
                                    rate,
                                    elapsed,
                                )
                                if self._metrics_server is not None:
                                    self._metrics_server.update_uptime()
                                    self._metrics_server.set_processing_rate(rate)
                        except (
                            RetryPublishedCommitFailedError,
                            DlqPublishedCommitFailedError,
                        ):
                            # Do NOT swallow: propagate so the process terminates.
                            raise
                        except Exception as e:
                            logger.error("Error processing message: %s", e)
                            break
                    if highest_safe_offset is not None:
                        pending_commits[tp] = make_offset_and_metadata(
                            highest_safe_offset + 1
                        )

                if pending_commits:
                    try:
                        self._consumer.commit(offsets=pending_commits)
                    except Exception as e:
                        logger.critical(
                            "CRITICAL: Batch source offset commit failed offsets=%s err=%s",
                            {
                                f"{tp.topic}:{tp.partition}": meta.offset
                                for tp, meta in pending_commits.items()
                            },
                            e,
                        )
                        metrics.events_commit_failed_total.labels(
                            reason="batch_commit_failed"
                        ).inc()
                        if strict_commit_required:
                            raise RetryPublishedCommitFailedError(
                                "Batch commit failed after retry/DLQ publish; "
                                "terminating for clean restart"
                            ) from e
                        raise

        except KeyboardInterrupt:
            logger.info("Shutting down gracefully...")
        except (RetryPublishedCommitFailedError, DlqPublishedCommitFailedError) as e:
            logger.critical(
                "CRITICAL: offset commit failed after broker-acked downstream write; "
                "intentional fail-fast exit reason=downstream_commit_uncertain err=%s",
                e,
                exc_info=True,
            )
            self._set_loop_alive(False)
            metrics.consumer_health.set(0)
            metrics.consumer_terminations_total.labels(
                reason="downstream_commit_uncertain"
            ).inc()
            sys.exit(1)
        except Exception as e:
            logger.error("Fatal error in consumer loop: %s", e, exc_info=True)
            self._set_loop_alive(False)
            metrics.consumer_health.set(0)
            metrics.consumer_terminations_total.labels(reason="fatal_loop_error").inc()
            sys.exit(1)
        finally:
            if self._paused_partitions:
                logger.info(
                    "Resuming %d paused partition(s) before shutdown",
                    len(self._paused_partitions),
                )
                try:
                    self._consumer.resume(*list(self._paused_partitions.keys()))
                except Exception as e:
                    logger.warning("Failed to resume partitions during shutdown: %s", e)
            # Delegate close to consumer (EventConsumer.close())
            self._close()

    # ------------------------------------------------------------------
    # Partition resume / lag helpers (moved from EventConsumer)
    # ------------------------------------------------------------------

    def _resume_ready_partitions(self) -> None:
        """Resume partitions whose retry backoff has expired."""
        if not self._paused_partitions:
            return

        now = time.time()
        to_resume = [
            tp
            for tp, resume_at in list(self._paused_partitions.items())
            if now >= resume_at
        ]

        if to_resume:
            try:
                self._consumer.resume(*to_resume)
                for tp in to_resume:
                    del self._paused_partitions[tp]
                    logger.info(
                        "Resumed partition topic=%s partition=%s (remaining_paused=%d)",
                        tp.topic,
                        tp.partition,
                        len(self._paused_partitions),
                    )
                metrics.retry_partitions_paused.set(len(self._paused_partitions))
            except Exception as e:
                logger.error("Failed to resume partitions %s: %s", to_resume, e)
                for tp in to_resume:
                    self._paused_partitions.pop(tp, None)
                metrics.retry_partitions_paused.set(len(self._paused_partitions))

    def _update_kafka_lag(self) -> None:
        """Update Kafka consumer lag metrics."""
        try:
            for tp in self._consumer.assignment():
                committed = self._consumer.committed(tp)
                if committed is None:
                    continue
                end_offsets = self._consumer.end_offsets([tp])
                end_offset = end_offsets.get(tp, 0)
                lag = end_offset - committed
                metrics.kafka_consumer_lag.labels(
                    topic=tp.topic, partition=str(tp.partition)
                ).set(max(0, lag))
        except Exception as e:
            logger.warning("Failed to update Kafka lag metrics: %s", e)

    def _close(self) -> None:
        """Placeholder — EventConsumer.close() is called by the delegation wrapper."""
        pass
