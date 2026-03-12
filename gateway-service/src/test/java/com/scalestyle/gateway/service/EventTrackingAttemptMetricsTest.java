package com.scalestyle.gateway.service;

import io.micrometer.core.instrument.simple.SimpleMeterRegistry;
import org.junit.jupiter.api.BeforeEach;
import org.junit.jupiter.api.DisplayName;
import org.junit.jupiter.api.Test;

import java.util.concurrent.atomic.AtomicLong;

import static org.junit.jupiter.api.Assertions.assertEquals;
import static org.junit.jupiter.api.Assertions.assertFalse;
import static org.junit.jupiter.api.Assertions.assertTrue;

class EventTrackingAttemptMetricsTest {

    private EventTrackingAttemptMetrics metrics;
    private AtomicLong now;

    @BeforeEach
    void setUp() {
        now = new AtomicLong(1_000_000L);
        metrics = new EventTrackingAttemptMetrics(new SimpleMeterRegistry(), now::get);
    }

    @Test
    @DisplayName("Cold start: no async publish samples means not degraded")
    void coldStart_noSamples_notDegraded() {
        assertFalse(metrics.isAsyncPublishDegraded());
        assertEquals(0.0, metrics.getAsyncPublishDegradedGaugeValue());
    }

    @Test
    @DisplayName("Warm-up gate: below MIN_SAMPLES async publish failures do not degrade the pipeline")
    void belowMinSamples_failuresDoNotDegrade() {
        for (int i = 0; i < EventTrackingAttemptMetrics.MIN_SAMPLES - 1; i++) {
            metrics.recordKafkaPublishRejected();
        }

        assertFalse(metrics.isAsyncPublishDegraded());
        assertEquals(0.0, metrics.getAsyncPublishDegradedGaugeValue());
    }

    @Test
    @DisplayName("Async publish degradation activates once failure ratio crosses threshold")
    void asyncPublishDegradation_activatesAtThreshold() {
        for (int i = 0; i < EventTrackingAttemptMetrics.MIN_SAMPLES; i++) {
            metrics.recordKafkaPublishRejected();
        }

        assertTrue(metrics.isAsyncPublishDegraded());
        assertEquals(1.0, metrics.getAsyncPublishDegradedGaugeValue());
    }

    @Test
    @DisplayName("Degraded state clears after a success-filled async publish window")
    void asyncPublishDegradation_recoversAfterSuccesses() {
        for (int i = 0; i < EventTrackingAttemptMetrics.WINDOW_SIZE; i++) {
            metrics.recordKafkaPublishRejected();
        }
        assertTrue(metrics.isAsyncPublishDegraded());

        for (int i = 0; i < EventTrackingAttemptMetrics.WINDOW_SIZE; i++) {
            metrics.recordKafkaPublishAcknowledged();
        }

        assertFalse(metrics.isAsyncPublishDegraded());
    }

    @Test
    @DisplayName("Local queue saturation is independent from async publish degradation")
    void localQueueSaturation_isIndependent() {
        for (int i = 0; i < EventTrackingAttemptMetrics.LOCAL_QUEUE_SATURATION_THRESHOLD; i++) {
            metrics.recordLocalQueueRejected();
        }

        assertTrue(metrics.isLocalProcessingQueueSaturated());
        assertFalse(metrics.isAsyncPublishDegraded());
    }

    @Test
    @DisplayName("Kafka send blocked is independent from async publish degradation")
    void kafkaSendBlocked_isIndependent() {
        for (int i = 0; i < EventTrackingAttemptMetrics.KAFKA_SEND_BLOCK_THRESHOLD; i++) {
            metrics.recordKafkaSendBlocked();
        }

        assertTrue(metrics.isKafkaSendBlocked());
        assertFalse(metrics.isAsyncPublishDegraded());
    }

    @Test
    @DisplayName("Recent local queue rejections age out")
    void localQueueSaturation_agesOut() {
        for (int i = 0; i < EventTrackingAttemptMetrics.LOCAL_QUEUE_SATURATION_THRESHOLD; i++) {
            metrics.recordLocalQueueRejected();
        }
        assertTrue(metrics.isLocalProcessingQueueSaturated());

        now.addAndGet(EventTrackingAttemptMetrics.LOCAL_QUEUE_WINDOW_MS + 1);

        assertFalse(metrics.isLocalProcessingQueueSaturated());
    }
}
