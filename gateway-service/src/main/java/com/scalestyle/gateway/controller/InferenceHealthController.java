package com.scalestyle.gateway.controller;

import io.github.resilience4j.circuitbreaker.CircuitBreaker;
import io.github.resilience4j.circuitbreaker.CircuitBreakerRegistry;
import io.micrometer.core.instrument.Counter;
import io.micrometer.core.instrument.Gauge;
import io.micrometer.core.instrument.MeterRegistry;
import lombok.extern.slf4j.Slf4j;
import org.springframework.beans.factory.annotation.Qualifier;
import org.springframework.beans.factory.annotation.Value;
import org.springframework.scheduling.concurrent.ThreadPoolTaskExecutor;
import org.springframework.web.bind.annotation.GetMapping;
import org.springframework.web.bind.annotation.RestController;

import io.micrometer.core.instrument.Meter;

import java.util.LinkedHashMap;
import java.util.Map;
import java.util.concurrent.Executor;
import java.util.concurrent.ThreadPoolExecutor;

/**
 * Exposes inference service resilience metrics for monitoring.
 *
 * Response sections:
 *  - circuitBreaker   Live Resilience4j sliding-window state (failures, slow calls, rejects)
 *  - threadPool       Live executor state including cumulative task/rejection estimates
 *  - httpClient       Reactor Netty connection pool runtime (active, max, saturation)
 *  - runtimeCounters  Micrometer counters since pod start (Ray calls, degradations, misses)
 *  - productionGuarantees  Static configuration limits for capacity reasoning
 *  - status           Derived overall health signal
 *
 * HTTP client saturation is read from Reactor Netty gauges (reactor.netty.connection.provider.*).
 * With pendingAcquireMaxCount=0 there is no pending-acquire depth; pool exhaustion shows as
 * active connections near max and/or circuit-breaker/fallback activity.
 *
 * Example: curl http://localhost:8080/api/inference/health
 */
@Slf4j
@RestController
public class InferenceHealthController {

    private final CircuitBreakerRegistry circuitBreakerRegistry;
    private final MeterRegistry meterRegistry;
    private final Executor inferenceExecutorBean;

    public InferenceHealthController(
            CircuitBreakerRegistry circuitBreakerRegistry,
            MeterRegistry meterRegistry,
            @Qualifier("inferenceExecutor") Executor inferenceExecutorBean) {
        this.circuitBreakerRegistry = circuitBreakerRegistry;
        this.meterRegistry = meterRegistry;
        this.inferenceExecutorBean = inferenceExecutorBean;
    }

    @Value("${inference.http.max-connections:100}")
    private int maxConnections;

    /** Pool name must match InferenceClientConfig ConnectionProvider.builder(name). */
    private static final String INFERENCE_POOL_NAME = "inference-pool";
    
    @GetMapping("/api/inference/health")
    public Map<String, Object> inferenceHealth() {
        Map<String, Object> health = new LinkedHashMap<>();
        
        // Circuit breaker state — all fields are the sliding-window snapshot, not all-time totals.
        CircuitBreaker cb = circuitBreakerRegistry.circuitBreaker("ray");
        Map<String, Object> circuitBreakerMetrics = new LinkedHashMap<>();
        circuitBreakerMetrics.put("state", cb.getState().name());
        circuitBreakerMetrics.put("failureRate", String.format("%.2f%%", cb.getMetrics().getFailureRate()));
        circuitBreakerMetrics.put("slowCallRate", String.format("%.2f%%", cb.getMetrics().getSlowCallRate()));
        circuitBreakerMetrics.put("numberOfSuccessfulCalls", cb.getMetrics().getNumberOfSuccessfulCalls());
        circuitBreakerMetrics.put("numberOfFailedCalls", cb.getMetrics().getNumberOfFailedCalls());
        // Calls blocked by an OPEN circuit — non-zero means Ray is currently excluded.
        circuitBreakerMetrics.put("numberOfNotPermittedCalls", cb.getMetrics().getNumberOfNotPermittedCalls());
        // Slow calls are those that exceeded the configured slow-call duration threshold.
        // A rising slow-call count without failure-rate increase means Ray is degrading
        // in latency but not yet throwing errors — an early warning signal.
        circuitBreakerMetrics.put("numberOfSlowCalls", cb.getMetrics().getNumberOfSlowCalls());
        circuitBreakerMetrics.put("numberOfSlowFailedCalls", cb.getMetrics().getNumberOfSlowFailedCalls());
        health.put("circuitBreaker", circuitBreakerMetrics);

        // HTTP client connection pool — runtime saturation (Reactor Netty gauges).
        Map<String, Object> httpClientMetrics = buildHttpClientMetrics();
        health.put("httpClient", httpClientMetrics);
        boolean isConnectionPoolNearLimit = Boolean.TRUE.equals(httpClientMetrics.get("WARNING_connectionPoolNearLimit"));
        
        // Derive live executor shape before either branch uses it.
        // queueCapacity = current queue size + remaining space = configured capacity.
        // This avoids hardcoding the value that lives in InferenceClientConfig.
        int derivedMaxPoolSize = -1;
        int derivedQueueCapacity = -1;
        if (inferenceExecutorBean instanceof ThreadPoolTaskExecutor) {
            ThreadPoolExecutor tpe = ((ThreadPoolTaskExecutor) inferenceExecutorBean).getThreadPoolExecutor();
            derivedMaxPoolSize = tpe.getMaximumPoolSize();
            derivedQueueCapacity = tpe.getQueue().size() + tpe.getQueue().remainingCapacity();
        }

        // Thread pool stats - check if executor is ThreadPoolTaskExecutor
        if (inferenceExecutorBean instanceof ThreadPoolTaskExecutor) {
            ThreadPoolTaskExecutor taskExecutor = (ThreadPoolTaskExecutor) inferenceExecutorBean;
            ThreadPoolExecutor executor = taskExecutor.getThreadPoolExecutor();
            
            Map<String, Object> threadPoolMetrics = new LinkedHashMap<>();
            threadPoolMetrics.put("corePoolSize", executor.getCorePoolSize());
            threadPoolMetrics.put("maxPoolSize", derivedMaxPoolSize);
            threadPoolMetrics.put("activeThreads", executor.getActiveCount());
            threadPoolMetrics.put("poolSize", executor.getPoolSize());
            threadPoolMetrics.put("queueSize", executor.getQueue().size());
            threadPoolMetrics.put("queueCapacity", derivedQueueCapacity);

            // Cumulative task history — useful for detecting rejections and throughput trends.
            long totalSubmitted = executor.getTaskCount();
            long totalCompleted = executor.getCompletedTaskCount();
            threadPoolMetrics.put("totalTasksSubmitted", totalSubmitted);
            threadPoolMetrics.put("totalTasksCompleted", totalCompleted);
            // Approximate rejected tasks: submitted that are not completed, active, or queued.
            // This is a point-in-time approximation — can be briefly inconsistent under
            // concurrent load, but a sustained positive value confirms AbortPolicy firings.
            long approxRejected = totalSubmitted - totalCompleted
                    - executor.getActiveCount() - executor.getQueue().size();
            threadPoolMetrics.put("approxRejectedTasks", Math.max(0L, approxRejected));

            // Calculate saturation using derived capacity (no hardcoded divisors).
            double threadSaturation = (double) executor.getActiveCount() / derivedMaxPoolSize * 100;
            double queueSaturation  = (double) executor.getQueue().size() / derivedQueueCapacity * 100;
            threadPoolMetrics.put("threadSaturation_%", String.format("%.1f", threadSaturation));
            threadPoolMetrics.put("queueSaturation_%", String.format("%.1f", queueSaturation));

            // Warning flags
            boolean isThreadPoolSaturated = threadSaturation > 80;
            boolean isQueueSaturated = queueSaturation > 80;
            threadPoolMetrics.put("WARNING_threadPoolNearLimit", isThreadPoolSaturated);
            threadPoolMetrics.put("WARNING_queueNearLimit", isQueueSaturated);
            
            health.put("threadPool", threadPoolMetrics);
            
            // Overall health assessment — ordered from most to least severe.
            String status;
            if (cb.getState() == CircuitBreaker.State.OPEN) {
                status = "DEGRADED_CIRCUIT_OPEN";
            } else if (isThreadPoolSaturated || isQueueSaturated) {
                status = "DEGRADED_SATURATED";
            } else if (isConnectionPoolNearLimit) {
                status = "DEGRADED_CONNECTION_POOL_NEAR_LIMIT";
            } else if (cb.getMetrics().getFailureRate() > 30) {
                status = "WARNING_HIGH_ERROR_RATE";
            } else if (cb.getMetrics().getSlowCallRate() > 50) {
                // Ray is responding slowly but not failing; latency risk before failure risk.
                status = "WARNING_HIGH_SLOW_CALL_RATE";
            } else {
                status = "HEALTHY";
            }
            health.put("status", status);
        } else {
            health.put("threadPool", "Not available (executor type: " + inferenceExecutorBean.getClass().getSimpleName() + ")");
            String status = cb.getState() == CircuitBreaker.State.OPEN ? "DEGRADED_CIRCUIT_OPEN"
                    : isConnectionPoolNearLimit ? "DEGRADED_CONNECTION_POOL_NEAR_LIMIT" : "HEALTHY";
            health.put("status", status);
        }

        // Runtime counters — cumulative totals since pod start from Micrometer.
        // Use Prometheus /metrics or a time-series dashboard for per-second rates.
        // Counters return -1 if the meter has not yet been registered (e.g., no requests
        // have been processed since startup, or RecommendationService not yet active).
        health.put("runtimeCounters", buildRuntimeCounters());

        // Hardness guarantees — all numeric values derived from live executor state.
        Map<String, Object> guarantees = new LinkedHashMap<>();
        // Single-queue model: the executor bulkhead is the ONLY concurrency gate.
        int maxSlots = (derivedMaxPoolSize > 0 && derivedQueueCapacity > 0)
                ? derivedMaxPoolSize + derivedQueueCapacity
                : -1; // unknown if executor is an unexpected type
        guarantees.put("executorBulkhead_maxSlots", maxSlots);
        // Connection pool: sized to cover all active threads with slack.
        // pendingAcquireMaxCount=0 → pool-full condition rejects immediately
        // (PoolAcquirePendingLimitException → onErrorResume → Redis fallback).
        // No secondary wait queue; no hidden latency added under saturation.
        guarantees.put("connectionPool_maxActive", maxConnections);
        guarantees.put("connectionPool_acquirePolicy", "fail-fast (pendingAcquireMaxCount=0)");
        guarantees.put("maxInferenceLatency_ms", 450);
        guarantees.put("executorRejectPolicy", "AbortPolicy - immediate rejection when bulkhead full");
        guarantees.put("canReverseBackpressureToCaller", false);
        guarantees.put("circuitBreakerThreshold", "50% failure rate over 50 calls");
        health.put("productionGuarantees", guarantees);
        
        return health;
    }

    /**
     * Reads existing Micrometer counters registered by RecommendationService.
     * Uses find-by-name so this never creates spurious meters as a side effect.
     * All values are all-time totals since pod start.
     */
    private Map<String, Object> buildRuntimeCounters() {
        Map<String, Object> counters = new LinkedHashMap<>();

        double successCount   = readCounter("recommendation_ray_success_total");
        double failureCount   = readCounter("recommendation_ray_failure_total");
        double degradedCount  = readCounter("recommendation_degraded_total");
        double missCount      = readCounter("recommendation_metadata_miss_total");
        double requestedCands = readCounter("recommendation_requested_candidates_total");
        double returnedCands  = readCounter("recommendation_returned_candidates_total");

        counters.put("ray_success_total",   formatCount(successCount));
        counters.put("ray_failure_total",   formatCount(failureCount));
        counters.put("degraded_total",      formatCount(degradedCount));
        counters.put("metadata_miss_total", formatCount(missCount));
        counters.put("requested_candidates_total", formatCount(requestedCands));
        counters.put("returned_candidates_total",   formatCount(returnedCands));

        // Ray call success rate over the pod's lifetime (not a sliding window).
        if (successCount >= 0 && failureCount >= 0) {
            double callTotal = successCount + failureCount;
            counters.put("ray_success_rate_%",
                    callTotal > 0 ? String.format("%.1f", successCount / callTotal * 100)
                                  : "no_calls_yet");
        }

        // Lifetime degradation rate: what fraction of handled requests fell back.
        // High rate relative to ray_failure_rate indicates fallbacks from timeout/bulkhead
        // as well as circuit-breaker opens — more complete than failure rate alone.
        if (successCount >= 0 && degradedCount >= 0) {
            double requestTotal = successCount + degradedCount;
            counters.put("lifetime_degradation_rate_%",
                    requestTotal > 0 ? String.format("%.1f", degradedCount / requestTotal * 100)
                                     : "no_requests_yet");
        }

        // Metadata miss ratios: distinguish occasional single miss from widespread Redis gap.
        // miss/requested = fraction of lookup pool missing; miss/returned = misses per returned item.
        if (missCount >= 0 && requestedCands > 0) {
            counters.put("metadata_miss_ratio_by_requested_%",
                    String.format("%.2f", missCount / requestedCands * 100));
        }
        if (missCount >= 0 && returnedCands > 0) {
            counters.put("metadata_miss_ratio_by_returned_%",
                    String.format("%.2f", missCount / returnedCands * 100));
        }

        return counters;
    }

    /**
     * HTTP client (Reactor Netty) connection pool runtime metrics.
     * Reads reactor.netty.connection.provider.active.connections for the inference pool.
     * When the pool has not been used yet, Reactor Netty may not have registered gauges —
     * in that case activeConnections is "unavailable" and saturation is omitted.
     */
    private Map<String, Object> buildHttpClientMetrics() {
        Map<String, Object> out = new LinkedHashMap<>();
        out.put("poolName", INFERENCE_POOL_NAME);
        out.put("maxConnections", maxConnections);

        double activeSum = 0;
        for (Meter meter : meterRegistry.getMeters()) {
            if (!(meter instanceof Gauge)) continue;
            Meter.Id id = meter.getId();
            if (!"reactor.netty.connection.provider.active.connections".equals(id.getName())) continue;
            if (!INFERENCE_POOL_NAME.equals(id.getTag("name"))) continue;
            activeSum += ((Gauge) meter).value();
        }

        if (activeSum < 0 || Double.isNaN(activeSum)) {
            out.put("activeConnections", "unavailable");
            out.put("connectionPool_saturation_%", "unavailable");
            out.put("WARNING_connectionPoolNearLimit", false);
            return out;
        }
        int active = (int) Math.round(activeSum);
        out.put("activeConnections", active);
        if (maxConnections > 0) {
            double saturation = active / (double) maxConnections * 100;
            out.put("connectionPool_saturation_%", String.format("%.1f", saturation));
            out.put("WARNING_connectionPoolNearLimit", saturation >= 80);
        } else {
            out.put("connectionPool_saturation_%", "unavailable");
            out.put("WARNING_connectionPoolNearLimit", false);
        }
        return out;
    }

    /** Looks up a counter by name + service tag without creating it if absent. */
    private double readCounter(String name) {
        Counter counter = meterRegistry.find(name).tag("service", "gateway").counter();
        return counter != null ? counter.count() : -1;
    }

    /** Formats a counter value; returns "unavailable" if the meter was not found. */
    private Object formatCount(double count) {
        return count >= 0 ? (long) count : "unavailable";
    }
}
