package com.scalestyle.gateway.common;

import org.junit.jupiter.api.DisplayName;
import org.junit.jupiter.api.Test;

import java.util.Set;
import java.util.stream.Collectors;
import java.util.stream.Stream;

import static org.assertj.core.api.Assertions.assertThat;

/**
 * Verifies canonical degradation reason vocabulary.
 * Contract: docs/DEGRADATION_REASONS.md.
 */
class DegradationReasonTest {

    private static final Set<String> CANONICAL_REASONS = Set.of(
            "REDIS_TIMEOUT",
            "REDIS_UNAVAILABLE",
            "PERSONALIZATION_UNAVAILABLE",
            "INFERENCE_TIMEOUT",
            "INFERENCE_UNAVAILABLE",
            "CACHE_MISS",
            "STALE_DATA_ALLOWED",
            "DOWNSTREAM_CIRCUIT_OPEN",
            "DOWNSTREAM_CAPACITY_REJECTED",
            "EMPTY_RESULTS_ALLOWED"
    );

    @Test
    @DisplayName("All DegradationReason names match canonical vocabulary")
    void allReasonsMatchCanonicalVocabulary() {
        for (DegradationReason reason : DegradationReason.values()) {
            assertThat(reason.name()).isIn(CANONICAL_REASONS);
        }
    }

    @Test
    @DisplayName("No duplicate or legacy reason names")
    void noLegacyStrings() {
        Set<String> names = Stream.of(DegradationReason.values())
                .map(DegradationReason::name)
                .collect(Collectors.toSet());
        assertThat(names).hasSize(DegradationReason.values().length);
        assertThat(names).isEqualTo(CANONICAL_REASONS);
    }

    @Test
    @DisplayName("Reason names are UPPER_SNAKE_CASE for metrics/logs")
    void reasonNamesAreUpperSnakeCase() {
        for (DegradationReason reason : DegradationReason.values()) {
            assertThat(reason.name()).isUpperCase();
            assertThat(reason.name()).contains("_");
            assertThat(reason.name()).doesNotContain(" ");
        }
    }
}
