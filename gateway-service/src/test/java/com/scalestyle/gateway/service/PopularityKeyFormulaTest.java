package com.scalestyle.gateway.service;

import org.junit.jupiter.api.DisplayName;
import org.junit.jupiter.api.Test;

import static org.assertj.core.api.Assertions.assertThat;

/**
 * Cross-language contract test: the gateway popularity materialized-key formula must produce
 * byte-for-byte identical keys to the Python inference service.
 *
 * FORMULA:
 *   key = "popularity:materialized:{window}:{floor(nowEpochSeconds / bucketSeconds) * bucketSeconds}"
 *
 * Fixed test timestamp: 1_700_000_000 Unix seconds (2023-11-14T22:13:20 UTC)
 *
 * Python equivalents in inference-service/tests/test_popularity_windowed.py:
 *   materialized_window_key("24h", 3600,  now_ts=1_700_000_000) == "popularity:materialized:24h:1699999200"
 *   materialized_window_key("7d",  86400, now_ts=1_700_000_000) == "popularity:materialized:7d:1699920000"
 *   materialized_window_key("1h",  300,   now_ts=1_700_000_000) == "popularity:materialized:1h:1699999800"
 *
 * If either language's expected string changes, the formulas have drifted and the gateway
 * will read from a different ZSET than the one inference wrote — silently falling through
 * to global:popular without a visible error.
 */
class PopularityKeyFormulaTest {

    private static final String PREFIX = "popularity:materialized";
    // 2023-11-14T22:13:20 UTC — shared anchor with Python test_popularity_windowed.py
    private static final long TEST_TS = 1_700_000_000L;

    @Test
    @DisplayName("24h window (3600s buckets): 1_700_000_000 → popularity:materialized:24h:1699999200")
    void key_24h_knownTimestamp() {
        assertThat(RecommendationService.popularityMaterializedKey(PREFIX, "24h", TEST_TS, 3600L))
                .isEqualTo("popularity:materialized:24h:1699999200");
    }

    @Test
    @DisplayName("7d window (86400s buckets): 1_700_000_000 → popularity:materialized:7d:1699920000")
    void key_7d_knownTimestamp() {
        assertThat(RecommendationService.popularityMaterializedKey(PREFIX, "7d", TEST_TS, 86400L))
                .isEqualTo("popularity:materialized:7d:1699920000");
    }

    @Test
    @DisplayName("1h window (300s buckets): 1_700_000_000 → popularity:materialized:1h:1699999800")
    void key_1h_knownTimestamp() {
        assertThat(RecommendationService.popularityMaterializedKey(PREFIX, "1h", TEST_TS, 300L))
                .isEqualTo("popularity:materialized:1h:1699999800");
    }

    @Test
    @DisplayName("Alignment invariant: bucketStart is always a multiple of bucketSeconds")
    void bucketStart_isMultipleOfBucketSeconds() {
        for (long bucketSecs : new long[]{300L, 3600L, 86400L}) {
            String key = RecommendationService.popularityMaterializedKey(PREFIX, "w", TEST_TS, bucketSecs);
            long bucketStart = Long.parseLong(key.substring(key.lastIndexOf(':') + 1));
            assertThat(bucketStart % bucketSecs)
                    .as("bucketStart must be a multiple of bucketSeconds=%d", bucketSecs)
                    .isEqualTo(0L);
        }
    }

    @Test
    @DisplayName("Boundary: timestamp exactly on a bucket boundary maps to that boundary")
    void key_exactBoundary_24h() {
        // 1_699_999_200 is itself a multiple of 3600 — should round to itself, not the prior bucket.
        assertThat(RecommendationService.popularityMaterializedKey(PREFIX, "24h", 1_699_999_200L, 3600L))
                .isEqualTo("popularity:materialized:24h:1699999200");
    }
}
