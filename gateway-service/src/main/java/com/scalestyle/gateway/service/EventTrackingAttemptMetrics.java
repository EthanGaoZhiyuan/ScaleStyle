package com.scalestyle.gateway.service;

import io.micrometer.core.instrument.Counter;
import io.micrometer.core.instrument.Gauge;
import io.micrometer.core.instrument.MeterRegistry;
import io.micrometer.core.instrument.Tags;
import org.springframework.beans.factory.annotation.Autowired;
import org.springframework.stereotype.Component;

import java.util.concurrent.atomic.AtomicInteger;
import java.util.concurrent.atomic.AtomicIntegerArray;
import java.util.concurrent.atomic.AtomicLongArray;
import java.util.function.LongSupplier;

/**
 * Observability and admission-control metrics for broker-acknowledged click tracking.
 *
 * <p>Admission to the local executor and Kafka broker acknowledgment are tracked
 * separately. A successful HTTP response still requires broker acknowledgment,
 * while local admission metrics show whether the async bulkhead is saturated.
 */
@Component
public class EventTrackingAttemptMetrics {

    static final int WINDOW_SIZE = 100;
    static final long DEGRADED_WINDOW_MS = 30_000L;
    static final int MIN_SAMPLES = 20;
    static final double FAILURE_RATIO_THRESHOLD = 0.5;

    static final int LOCAL_QUEUE_WINDOW_SIZE = 10;
    static final long LOCAL_QUEUE_WINDOW_MS = 30_000L;
    static final int LOCAL_QUEUE_SATURATION_THRESHOLD = 3;

    static final int KAFKA_SEND_BLOCK_WINDOW_SIZE = 10;
    static final long KAFKA_SEND_BLOCK_WINDOW_MS = 30_000L;
    static final int KAFKA_SEND_BLOCK_THRESHOLD = 3;

    private final Counter localAcceptedCounter;
    private final Counter kafkaAckCounter;
    private final Counter kafkaRejectCounter;
    private final Counter localQueueRejectedCounter;
    private final Counter kafkaSendBlockedCounter;

    private final AtomicIntegerArray recentPublishOutcomes;
    private final AtomicLongArray recentPublishOutcomeTimes;
    private final AtomicInteger nextPublishOutcomeIndex;

    private final AtomicLongArray localQueueRejectedTimes;
    private final AtomicInteger nextLocalQueueRejectedIndex;

    private final AtomicLongArray kafkaSendBlockedTimes;
    private final AtomicInteger nextKafkaSendBlockedIndex;

    private final LongSupplier currentTimeMillis;

    @Autowired
    public EventTrackingAttemptMetrics(MeterRegistry registry) {
        this(registry, System::currentTimeMillis);
    }

    EventTrackingAttemptMetrics(MeterRegistry registry, LongSupplier currentTimeMillis) {
        this.currentTimeMillis = currentTimeMillis;
        Tags tags = Tags.of("service", "gateway", "pipeline", "click_tracking");

        this.localAcceptedCounter = Counter.builder("click_tracking_local_processing_accepted_total")
                .description("Total click tracking requests accepted for a local async processing attempt")
                .tags(tags)
                .register(registry);
        this.kafkaAckCounter = Counter.builder("click_tracking_kafka_ack_total")
                .description("Total async click tracking publishes acknowledged by Kafka")
                .tags(tags)
                .register(registry);
        this.kafkaRejectCounter = Counter.builder("click_tracking_kafka_rejected_total")
                .description("Total async click tracking publishes rejected or failed before Kafka acknowledgment")
                .tags(tags)
                .register(registry);
        this.localQueueRejectedCounter = Counter.builder("click_tracking_local_queue_rejected_total")
                .description("Total click tracking requests rejected because the local async executor was saturated")
                .tags(tags)
                .register(registry);
        this.kafkaSendBlockedCounter = Counter.builder("click_tracking_kafka_send_blocked_total")
                .description("Total click tracking publishes where KafkaTemplate.send() threw synchronously")
                .tags(tags)
                .register(registry);

        this.recentPublishOutcomes = new AtomicIntegerArray(WINDOW_SIZE);
        this.recentPublishOutcomeTimes = new AtomicLongArray(WINDOW_SIZE);
        this.nextPublishOutcomeIndex = new AtomicInteger(0);

        this.localQueueRejectedTimes = new AtomicLongArray(LOCAL_QUEUE_WINDOW_SIZE);
        this.nextLocalQueueRejectedIndex = new AtomicInteger(0);

        this.kafkaSendBlockedTimes = new AtomicLongArray(KAFKA_SEND_BLOCK_WINDOW_SIZE);
        this.nextKafkaSendBlockedIndex = new AtomicInteger(0);

        Gauge.builder("click_tracking_async_publish_degraded", this, EventTrackingAttemptMetrics::getAsyncPublishDegradedGaugeValue)
                .description("1 when recent async Kafka publish outcomes are degraded")
                .tags(tags)
                .register(registry);
        Gauge.builder("click_tracking_local_queue_saturated", this, m -> m.isLocalProcessingQueueSaturated() ? 1.0 : 0.0)
                .description("1 when the local async click-tracking executor has rejected too many tasks recently")
                .tags(tags)
                .register(registry);
        Gauge.builder("click_tracking_kafka_send_blocked", this, m -> m.isKafkaSendBlocked() ? 1.0 : 0.0)
                .description("1 when KafkaTemplate.send() is throwing synchronously at an elevated rate")
                .tags(tags)
                .register(registry);
    }

    public void recordLocalProcessingAccepted() {
        localAcceptedCounter.increment();
    }

    public void recordKafkaPublishAcknowledged() {
        kafkaAckCounter.increment();
        recordPublishOutcome(1);
    }

    public void recordKafkaPublishRejected() {
        kafkaRejectCounter.increment();
        recordPublishOutcome(0);
    }

    public void recordLocalQueueRejected() {
        localQueueRejectedCounter.increment();
        int idx = Math.abs(nextLocalQueueRejectedIndex.getAndIncrement() % LOCAL_QUEUE_WINDOW_SIZE);
        localQueueRejectedTimes.set(idx, currentTimeMillis.getAsLong());
    }

    public void recordKafkaSendBlocked() {
        kafkaSendBlockedCounter.increment();
        int idx = Math.abs(nextKafkaSendBlockedIndex.getAndIncrement() % KAFKA_SEND_BLOCK_WINDOW_SIZE);
        kafkaSendBlockedTimes.set(idx, currentTimeMillis.getAsLong());
    }

    public double getAsyncPublishDegradedGaugeValue() {
        long cutoff = currentTimeMillis.getAsLong() - DEGRADED_WINDOW_MS;
        int samples = 0;
        int successes = 0;
        for (int i = 0; i < WINDOW_SIZE; i++) {
            if (recentPublishOutcomeTimes.get(i) <= cutoff) {
                continue;
            }
            samples++;
            successes += recentPublishOutcomes.get(i);
        }
        if (samples < MIN_SAMPLES) {
            return 0.0;
        }
        double failureRate = (samples - successes) / (double) samples;
        return failureRate >= FAILURE_RATIO_THRESHOLD ? 1.0 : 0.0;
    }

    public boolean isAsyncPublishDegraded() {
        return getAsyncPublishDegradedGaugeValue() >= 1.0;
    }

    public boolean isLocalProcessingQueueSaturated() {
        return countRecent(localQueueRejectedTimes, LOCAL_QUEUE_WINDOW_MS) >= LOCAL_QUEUE_SATURATION_THRESHOLD;
    }

    public boolean isKafkaSendBlocked() {
        return countRecent(kafkaSendBlockedTimes, KAFKA_SEND_BLOCK_WINDOW_MS) >= KAFKA_SEND_BLOCK_THRESHOLD;
    }

    private void recordPublishOutcome(int success) {
        int idx = Math.abs(nextPublishOutcomeIndex.getAndIncrement() % WINDOW_SIZE);
        recentPublishOutcomes.set(idx, success);
        recentPublishOutcomeTimes.set(idx, currentTimeMillis.getAsLong());
    }

    private int countRecent(AtomicLongArray timestamps, long windowMs) {
        long cutoff = currentTimeMillis.getAsLong() - windowMs;
        int count = 0;
        for (int i = 0; i < timestamps.length(); i++) {
            if (timestamps.get(i) > cutoff) {
                count++;
            }
        }
        return count;
    }
}
