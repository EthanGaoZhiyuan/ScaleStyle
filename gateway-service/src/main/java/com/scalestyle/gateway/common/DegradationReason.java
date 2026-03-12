package com.scalestyle.gateway.common;

/**
 * Canonical degradation reason vocabulary.
 * Contract: docs/DEGRADATION_REASONS.md.
 * Use for logs, metrics (reason=), and API response degradedReason.
 */
public enum DegradationReason {
    REDIS_TIMEOUT,
    REDIS_UNAVAILABLE,
    PERSONALIZATION_UNAVAILABLE,
    INFERENCE_TIMEOUT,
    INFERENCE_UNAVAILABLE,
    CACHE_MISS,
    STALE_DATA_ALLOWED,
    DOWNSTREAM_CIRCUIT_OPEN,
    DOWNSTREAM_CAPACITY_REJECTED,
    EMPTY_RESULTS_ALLOWED
}
