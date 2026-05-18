package com.scalestyle.gateway.service;

import com.scalestyle.gateway.common.DegradationReason;
import io.micrometer.core.instrument.Counter;
import io.micrometer.core.instrument.Gauge;
import io.micrometer.core.instrument.MeterRegistry;
import io.micrometer.core.instrument.Timer;
import org.springframework.stereotype.Component;

import java.time.Duration;
import java.util.concurrent.atomic.AtomicReference;

/**
 * Prometheus metrics for the recommendation serving path.
 *
 * Follows the same pattern as {@link EventTrackingAttemptMetrics}: a dedicated
 * {@code @Component} that owns metric registration, so the service constructor
 * stays focused on wiring business dependencies.
 *
 * Metric names and label sets are identical to what was previously registered
 * inline in {@code RecommendationService}'s constructor.
 */
@Component
public class RecommendationMetrics {

    static final double EMA_ALPHA = 0.2;

    private final Counter degradedTotalCounter;
    private final Counter raySuccessCounter;
    private final Counter rayFailureCounter;
    private final Counter metadataMissCounter;
    private final Counter requestedCandidatesCounter;
    private final Counter returnedCandidatesCounter;
    private final Timer queueWaitTimer;
    private final Timer inferenceHttpTimer;
    private final Timer metadataEnrichmentTimer;
    private final Timer fallbackTimer;
    private final AtomicReference<Double> missRatioEma = new AtomicReference<>(0.0);
    private final MeterRegistry meterRegistry;

    public RecommendationMetrics(MeterRegistry meterRegistry) {
        this.meterRegistry = meterRegistry;

        this.degradedTotalCounter = Counter.builder("recommendation_degraded_total")
                .description("Total count of degraded recommendations (fallback to Redis)")
                .tag("service", "gateway")
                .register(meterRegistry);
        this.raySuccessCounter = Counter.builder("recommendation_ray_success_total")
                .description("Total count of successful Ray inference calls")
                .tag("service", "gateway")
                .register(meterRegistry);
        this.rayFailureCounter = Counter.builder("recommendation_ray_failure_total")
                .description("Total count of failed Ray inference calls")
                .tag("service", "gateway")
                .register(meterRegistry);
        this.metadataMissCounter = Counter.builder("recommendation_metadata_miss_total")
                .description("Product items excluded from recommendations due to missing metadata in Redis")
                .tag("service", "gateway")
                .register(meterRegistry);
        this.requestedCandidatesCounter = Counter.builder("recommendation_requested_candidates_total")
                .description("Total candidate IDs looked up for product metadata (denominator for miss ratio by requested)")
                .tag("service", "gateway")
                .register(meterRegistry);
        this.returnedCandidatesCounter = Counter.builder("recommendation_returned_candidates_total")
                .description("Total candidates with metadata included in result set (denominator for miss ratio by returned)")
                .tag("service", "gateway")
                .register(meterRegistry);
        this.queueWaitTimer = Timer.builder("recommendation_phase_duration_seconds")
                .description("Recommendation request phase duration")
                .tag("service", "gateway")
                .tag("phase", "admission_queue_wait")
                .publishPercentileHistogram()
                .register(meterRegistry);
        this.inferenceHttpTimer = Timer.builder("recommendation_phase_duration_seconds")
                .description("Recommendation request phase duration")
                .tag("service", "gateway")
                .tag("phase", "inference_http")
                .publishPercentileHistogram()
                .register(meterRegistry);
        this.metadataEnrichmentTimer = Timer.builder("recommendation_phase_duration_seconds")
                .description("Recommendation request phase duration")
                .tag("service", "gateway")
                .tag("phase", "metadata_enrichment")
                .publishPercentileHistogram()
                .register(meterRegistry);
        this.fallbackTimer = Timer.builder("recommendation_phase_duration_seconds")
                .description("Recommendation request phase duration")
                .tag("service", "gateway")
                .tag("phase", "fallback")
                .publishPercentileHistogram()
                .register(meterRegistry);
        Gauge.builder("recommendation_metadata_miss_ratio", missRatioEma, AtomicReference::get)
                .description("Exponential moving average (alpha=0.2) of per-request metadata miss ratio (0.0–1.0). "
                        + "Alert when sustained value exceeds bootstrap-gap threshold.")
                .tag("service", "gateway")
                .register(meterRegistry);
    }

    public void incrementDegradedTotal() {
        degradedTotalCounter.increment();
    }

    public void incrementRaySuccess() {
        raySuccessCounter.increment();
    }

    public void incrementRayFailure() {
        rayFailureCounter.increment();
    }

    public void incrementMetadataMiss(double count) {
        metadataMissCounter.increment(count);
    }

    public void incrementRequestedCandidates(double count) {
        requestedCandidatesCounter.increment(count);
    }

    public void incrementReturnedCandidates(double count) {
        returnedCandidatesCounter.increment(count);
    }

    public void recordQueueWait(Duration d) {
        queueWaitTimer.record(d);
    }

    public void recordInferenceHttp(Duration d) {
        inferenceHttpTimer.record(d);
    }

    public void recordMetadataEnrichment(Duration d) {
        metadataEnrichmentTimer.record(d);
    }

    public void recordFallback(Duration d) {
        fallbackTimer.record(d);
    }

    /**
     * Updates the EMA of per-request metadata miss ratio.
     * EMA formula: new = alpha * sample + (1 - alpha) * previous
     */
    public void updateMissRatioEma(double missRatio) {
        missRatioEma.updateAndGet(prev -> EMA_ALPHA * missRatio + (1 - EMA_ALPHA) * prev);
    }

    public void recordDegradedReason(DegradationReason reason) {
        meterRegistry.counter(
                "recommendation_degraded_reason_total",
                "service", "gateway",
                "reason", reason.name()
        ).increment();
    }

    public void recordFallbackPopularityWindow(String window, String outcome) {
        meterRegistry.counter(
                "recommendation_fallback_popularity_window_total",
                "service", "gateway",
                "window", window,
                "outcome", outcome
        ).increment();
    }
}
