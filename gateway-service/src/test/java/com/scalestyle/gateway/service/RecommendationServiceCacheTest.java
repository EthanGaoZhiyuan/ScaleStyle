package com.scalestyle.gateway.service;

import com.fasterxml.jackson.core.JsonProcessingException;
import com.fasterxml.jackson.databind.ObjectMapper;
import com.scalestyle.gateway.common.DegradationReason;
import com.scalestyle.gateway.dto.InferenceSearchResponse;
import com.scalestyle.gateway.dto.RecommendationDTO;
import io.github.resilience4j.circuitbreaker.CircuitBreakerRegistry;
import io.micrometer.core.instrument.Timer;
import io.micrometer.core.instrument.simple.SimpleMeterRegistry;
import org.junit.jupiter.api.BeforeEach;
import org.junit.jupiter.api.DisplayName;
import org.junit.jupiter.api.Test;
import org.junit.jupiter.api.extension.ExtendWith;
import org.mockito.Mock;
import org.mockito.junit.jupiter.MockitoExtension;
import org.springframework.data.redis.core.StringRedisTemplate;
import org.springframework.data.redis.core.ValueOperations;
import org.springframework.data.redis.core.ZSetOperations;
import org.springframework.web.reactive.function.client.WebClient;
import reactor.core.publisher.Mono;

import java.time.Duration;
import java.util.LinkedHashSet;
import java.util.List;
import java.util.Optional;
import java.util.concurrent.TimeoutException;

import static org.assertj.core.api.Assertions.assertThat;
import static org.mockito.ArgumentMatchers.*;
import static org.mockito.ArgumentMatchers.argThat;
import static org.mockito.Mockito.lenient;
import static org.mockito.Mockito.*;

/**
 * Verifies the recommendation cache isolation contract:
 *
 * NON-PERSONALIZED (userId == null):
 *   - successful inference  → writes shared rec-cache
 *   - inference failure     → reads shared rec-cache (cache hit served; cache miss falls through to popular)
 *
 * PERSONALIZED (userId non-blank):
 *   - successful inference  → does NOT write shared rec-cache
 *   - inference failure     → does NOT read shared rec-cache; routes directly to popular items
 *
 * The shared rec-cache key is (query, intent, k) — it does not include userId.
 * Storing or reading personalized results under that key is a correctness bug:
 * user B would receive user A's personalized ranking, not a quality issue but a
 * data-isolation violation.
 */
@ExtendWith(MockitoExtension.class)
class RecommendationServiceCacheTest {

    // ── WebClient mock chain ──────────────────────────────────────────────────
    @Mock private WebClient webClient;
    @Mock private WebClient.RequestBodyUriSpec requestBodyUriSpec;
    @Mock private WebClient.RequestBodySpec requestBodySpec;
    @SuppressWarnings("rawtypes")
    @Mock private WebClient.RequestHeadersSpec requestHeadersSpec;
    @Mock private WebClient.ResponseSpec responseSpec;

    // ── Cache infrastructure mocks ────────────────────────────────────────────
    @Mock private StringRedisTemplate recommendationCacheTemplate;
    @Mock private ValueOperations<String, String> valueOperations;
    @Mock private StringRedisTemplate popularItemsTemplate;
    @Mock private ZSetOperations<String, String> zSetOperations;

    @Mock private ProductCacheService productCacheService;

    private RecommendationService service;
    private final ObjectMapper objectMapper = new ObjectMapper();
    private SimpleMeterRegistry meterRegistry;

    @BeforeEach
    @SuppressWarnings("unchecked")
    void setUp() {
        // Wire WebClient chain so individual tests can stub bodyToMono as needed
        lenient().when(webClient.post()).thenReturn(requestBodyUriSpec);
        lenient().when(requestBodyUriSpec.uri(anyString())).thenReturn(requestBodySpec);
        lenient().when(requestBodySpec.contentType(any())).thenReturn(requestBodySpec);
        lenient().when(requestBodySpec.bodyValue(any())).thenReturn(requestHeadersSpec);
        lenient().when(requestHeadersSpec.retrieve()).thenReturn(responseSpec);

        // Wire Redis template delegates
        lenient().when(recommendationCacheTemplate.opsForValue()).thenReturn(valueOperations);
        lenient().when(popularItemsTemplate.opsForZSet()).thenReturn(zSetOperations);

        // Default: productCacheService returns a present Optional per single-ID lookup
        // (used by callRayInferenceReactive which now calls getProduct per item).
        lenient().when(productCacheService.getProduct(anyString())).thenAnswer(inv -> {
            String id = inv.getArgument(0);
            return Optional.of(RecommendationDTO.builder().itemId(id).name("Product " + id).build());
        });

        // Default: productCacheService returns a list for batch lookups
        // (used by getPopularItemsFallbackMono which calls getProducts).
        lenient().when(productCacheService.getProducts(anyList())).thenAnswer(inv -> {
            List<String> ids = inv.getArgument(0);
            return ids.stream()
                    .map(id -> RecommendationDTO.builder().itemId(id).name("Product " + id).build())
                    .toList();
        });

        // Both executors use Runnable::run (direct/synchronous) in tests so that
        // subscribeOn() keeps execution on the test thread.
        // CompletableFuture.join() still correctly blocks for the full chain.
        meterRegistry = new SimpleMeterRegistry();
        service = new RecommendationService(
                webClient,
                productCacheService,
                recommendationCacheTemplate,
                objectMapper,
                Runnable::run,   // inferenceExecutor
                Runnable::run,   // cacheExecutor
                popularItemsTemplate,
                CircuitBreakerRegistry.ofDefaults(),
                meterRegistry
        );
    }

    // ── Helpers ───────────────────────────────────────────────────────────────

    private InferenceSearchResponse successfulResponseWith(String... articleIds) {
        List<InferenceSearchResponse.ResultItem> items = java.util.Arrays.stream(articleIds)
                .map(id -> {
                    InferenceSearchResponse.ResultItem item = new InferenceSearchResponse.ResultItem();
                    item.setArticle_id(id);
                    return item;
                })
                .toList();
        InferenceSearchResponse resp = new InferenceSearchResponse();
        resp.setResults(items);
        return resp;
    }

    private void stubInferenceSuccess(String... articleIds) {
        when(responseSpec.bodyToMono(InferenceSearchResponse.class))
                .thenReturn(Mono.just(successfulResponseWith(articleIds)));
    }

    private void stubInferenceFailure() {
        when(responseSpec.bodyToMono(InferenceSearchResponse.class))
                .thenReturn(Mono.error(new RuntimeException("Ray unavailable")));
    }

    private void stubPopularItems(String... ids) {
        when(zSetOperations.reverseRange(argThat(key -> key != null && key.startsWith("popularity:materialized:24h:")), anyLong(), anyLong()))
                .thenReturn(new LinkedHashSet<>(List.of(ids)));
    }

    /**
     * Regression: Inference timeout maps to INFERENCE_TIMEOUT degradation reason.
     */
    private void stubInferenceTimeout() {
        when(responseSpec.bodyToMono(InferenceSearchResponse.class))
                .thenReturn(Mono.error(new TimeoutException("Request timed out")));
    }

    // ── Missing metadata filtering ────────────────────────────────────────────

    @Test
    @DisplayName("Items with missing metadata are excluded from the inference response; present items are kept")
    void inference_filtersOutItemsWithMissingMetadata() {
        // Ray returns 3 article IDs; metadata is available for 2 of them.
        stubInferenceSuccess("item1", "item2_missing", "item3");
        when(productCacheService.getProduct("item1"))
                .thenReturn(Optional.of(RecommendationDTO.builder().itemId("item1").name("Red Dress").build()));
        when(productCacheService.getProduct("item2_missing"))
                .thenReturn(Optional.empty()); // metadata absent from Redis
        when(productCacheService.getProduct("item3"))
                .thenReturn(Optional.of(RecommendationDTO.builder().itemId("item3").name("Blue Skirt").build()));

        List<RecommendationDTO> results =
                service.searchAsync("dresses", null, "search", 5, false).join();

        // Must contain exactly the 2 items that had metadata.
        assertThat(results).hasSize(2);
        assertThat(results).extracting(RecommendationDTO::getItemId)
                .containsExactly("item1", "item3"); // rank order preserved
        // Must not include any fabricated placeholder for item2_missing.
        assertThat(results).noneMatch(r -> "item2_missing".equals(r.getItemId()));
        assertThat(results).noneMatch(r -> "Unknown Product".equals(r.getName()));
    }

    @Test
    @DisplayName("All items missing metadata returns empty result list — no fabricated products")
    void inference_allMetadataMissing_returnsEmptyList() {
        stubInferenceSuccess("item1", "item2");
        when(productCacheService.getProduct(anyString())).thenReturn(Optional.empty());

        List<RecommendationDTO> results =
                service.searchAsync("dresses", null, "search", 5, false).join();

        assertThat(results).isEmpty();
    }

    // ── Non-personalized (userId == null) ─────────────────────────────────────

    @Test
    @DisplayName("Non-personalized success: result is written to shared rec-cache")
    void nonPersonalized_success_writesSharedCache() {
        stubInferenceSuccess("item1", "item2");

        service.searchAsync("red dress", null, "search", 5, false).join();

        // Exactly one cache write must occur with the correct TTL
        verify(valueOperations, times(1))
                .set(anyString(), anyString(), eq(Duration.ofMinutes(5)));
    }

    @Test
    @DisplayName("Non-personalized fallback with cache hit: cached result is served")
    void nonPersonalized_fallback_cacheHit_servesCachedResult() throws JsonProcessingException {
        stubInferenceFailure();

        // Prime the shared cache with a result for this query
        RecommendationDTO cachedItem = RecommendationDTO.builder()
                .itemId("cached_item").name("Cached Product").build();
        String cachedJson = objectMapper.writeValueAsString(List.of(cachedItem));
        when(valueOperations.get(anyString())).thenReturn(cachedJson);

        List<RecommendationDTO> results =
                service.searchAsync("red dress", null, "search", 5, false).join();

        // Cache read must have been attempted
        verify(valueOperations, atLeastOnce()).get(anyString());
        // Result must come from cache, marked degraded
        assertThat(results).isNotEmpty();
        assertThat(results.get(0).getSource()).isEqualTo("redis-cache");
        assertThat(results.get(0).isDegraded()).isTrue();
        assertThat(results.get(0).getDegradedReason()).isEqualTo(DegradationReason.STALE_DATA_ALLOWED.name());
    }

    @Test
    @DisplayName("Non-personalized fallback with cache miss: popular items are served")
    void nonPersonalized_fallback_cacheMiss_servesPopularItems() {
        stubInferenceFailure();
        stubPopularItems("popular1", "popular2");
        when(valueOperations.get(anyString())).thenReturn(null); // cache miss

        List<RecommendationDTO> results =
                service.searchAsync("red dress", null, "search", 5, false).join();

        // Cache read must have been attempted before falling through
        verify(valueOperations, atLeastOnce()).get(anyString());
        // Results come from popular items
        assertThat(results).isNotEmpty();
        assertThat(results.get(0).getSource()).isEqualTo("popular-fallback");
        assertThat(results.get(0).getDegradedReason()).isEqualTo(DegradationReason.CACHE_MISS.name());
    }

    // ── Personalized (userId non-blank) ───────────────────────────────────────

    @Test
    @DisplayName("Personalized success: result is NOT written to shared rec-cache")
    void personalized_success_doesNotWriteSharedCache() {
        stubInferenceSuccess("item1", "item2");

        service.searchAsync("red dress", "user_alice", "search", 5, false).join();

        // No write to the shared cache — personalized results must stay out of it
        verify(valueOperations, never()).set(anyString(), anyString(), any(Duration.class));
    }

    @Test
    @DisplayName("Personalized fallback: shared rec-cache is not read")
    void personalized_fallback_doesNotReadSharedCache() {
        stubInferenceFailure();
        stubPopularItems("popular1");

        service.searchAsync("red dress", "user_alice", "search", 5, false).join();

        // Must never touch the shared query cache
        verify(valueOperations, never()).get(anyString());
    }

    @Test
    @DisplayName("Personalized fallback: routes directly to popular items, not to shared cache")
    void personalized_fallback_routesToPopularItems() {
        stubInferenceFailure();
        stubPopularItems("popular1", "popular2");

        List<RecommendationDTO> results =
                service.searchAsync("red dress", "user_alice", "search", 5, false).join();

        verify(zSetOperations, atLeastOnce())
                .reverseRange(argThat(key -> key != null && key.startsWith("popularity:materialized:24h:")), anyLong(), anyLong());
        assertThat(results).isNotEmpty();
        assertThat(results.get(0).getSource()).isEqualTo("popular-fallback");
        assertThat(results.get(0).isDegraded()).isTrue();
        assertThat(results.get(0).getDegradedReason()).isEqualTo(DegradationReason.INFERENCE_UNAVAILABLE.name());
    }

    @Test
    @DisplayName("Fallback popularity prefers materialized 24h window and does not query legacy all-time key")
    void fallbackPopularity_usesMaterialized24hWindow() {
        stubInferenceFailure();
        when(valueOperations.get(anyString())).thenReturn(null);
        when(zSetOperations.reverseRange(argThat(key -> key != null && key.startsWith("popularity:materialized:24h:")), anyLong(), anyLong()))
                .thenReturn(new LinkedHashSet<>(List.of("hot_recent")));

        List<RecommendationDTO> results =
                service.searchAsync("red dress", null, "search", 5, false).join();

        assertThat(results).extracting(RecommendationDTO::getItemId).containsExactly("hot_recent");
        verify(zSetOperations, never()).reverseRange(eq("global:popular"), anyLong(), anyLong());
    }

    @Test
    @DisplayName("Fallback popularity uses 7d secondary window when 24h materialized window is empty")
    void fallbackPopularity_usesSecondary7dWindowWhenPrimaryEmpty() {
        stubInferenceFailure();
        when(valueOperations.get(anyString())).thenReturn(null);
        when(zSetOperations.reverseRange(argThat(key -> key != null && key.startsWith("popularity:materialized:24h:")), anyLong(), anyLong()))
                .thenReturn(new LinkedHashSet<>());
        when(zSetOperations.reverseRange(argThat(key -> key != null && key.startsWith("popularity:materialized:7d:")), anyLong(), anyLong()))
                .thenReturn(new LinkedHashSet<>(List.of("warm_item")));

        List<RecommendationDTO> results =
                service.searchAsync("red dress", null, "search", 5, false).join();

        assertThat(results).extracting(RecommendationDTO::getItemId).containsExactly("warm_item");
    }

    @Test
    @DisplayName("Personalized fallback: does NOT serve another user's cached personalized results")
    void personalized_fallback_doesNotServeCrossUserCachedResults() throws JsonProcessingException {
        stubInferenceFailure();
        stubPopularItems("popular1");

        // Simulate: user A previously cached personalized results in the shared key
        RecommendationDTO userAItem = RecommendationDTO.builder()
                .itemId("user_a_private_item").name("User A's Dress").build();
        String userACachedJson = objectMapper.writeValueAsString(List.of(userAItem));
        // The shared cache DOES contain data for this query, but user B must not see it
        lenient().when(valueOperations.get(anyString())).thenReturn(userACachedJson);

        // User B makes a personalized request for the same query
        List<RecommendationDTO> results =
                service.searchAsync("red dress", "user_bob", "search", 5, false).join();

        // Must never read from shared cache regardless of what it contains
        verify(valueOperations, never()).get(anyString());
        // Must not include user A's item in user B's results
        assertThat(results).noneMatch(r -> "user_a_private_item".equals(r.getItemId()));
    }

    @Test
    @DisplayName("Latency budget metrics record queue, inference, enrichment, and fallback phases")
    void recommendationPhaseMetrics_areRecorded() {
        stubInferenceFailure();
        stubPopularItems("popular1", "popular2");
        when(valueOperations.get(anyString())).thenReturn(null);

        service.searchAsync("red dress", null, "search", 5, false).join();

        Timer queueWait = meterRegistry.find("recommendation_phase_duration_seconds")
                .tag("phase", "admission_queue_wait")
                .timer();
        Timer inferenceHttp = meterRegistry.find("recommendation_phase_duration_seconds")
                .tag("phase", "inference_http")
                .timer();
        Timer fallback = meterRegistry.find("recommendation_phase_duration_seconds")
                .tag("phase", "fallback")
                .timer();

        assertThat(queueWait).isNotNull();
        assertThat(inferenceHttp).isNotNull();
        assertThat(fallback).isNotNull();
        assertThat(queueWait.count()).isGreaterThan(0);
        assertThat(inferenceHttp.count()).isGreaterThan(0);
        assertThat(fallback.count()).isGreaterThan(0);
    }

    // ── Windowed fallback failure paths ─────────────────────────────────────────

    @Test
    @DisplayName("Both 24h and 7d popularity windows empty returns empty list without exception")
    void fallbackPopularity_bothWindowsEmpty_returnsEmptyList() {
        stubInferenceFailure();
        when(valueOperations.get(anyString())).thenReturn(null);
        when(zSetOperations.reverseRange(argThat(key -> key != null && key.startsWith("popularity:materialized:24h:")), anyLong(), anyLong()))
                .thenReturn(new LinkedHashSet<>());
        when(zSetOperations.reverseRange(argThat(key -> key != null && key.startsWith("popularity:materialized:7d:")), anyLong(), anyLong()))
                .thenReturn(new LinkedHashSet<>());

        List<RecommendationDTO> results =
                service.searchAsync("red dress", null, "search", 5, false).join();

        assertThat(results).isEmpty();
    }

    @Test
    @DisplayName("Redis error during popular items fallback: onErrorResume returns empty list")
    void fallbackPopularity_redisError_returnsEmptyList() {
        stubInferenceFailure();
        when(valueOperations.get(anyString())).thenReturn(null);
        when(zSetOperations.reverseRange(anyString(), anyLong(), anyLong()))
                .thenThrow(new RuntimeException("Connection refused"));

        List<RecommendationDTO> results =
                service.searchAsync("red dress", null, "search", 5, false).join();

        assertThat(results).isEmpty();
    }

    @Test
    @DisplayName("Inference TimeoutException maps to INFERENCE_TIMEOUT degradation reason")
    void mapInferenceDegradationReason_timeoutMapsToInferenceTimeout() {
        stubInferenceTimeout();
        // Personalized request bypasses shared cache, so fallback uses trigger reason (INFERENCE_TIMEOUT)
        stubPopularItems("fallback_item");

        List<RecommendationDTO> results =
                service.searchAsync("red dress", "user-42", "search", 5, false).join();

        assertThat(results).hasSize(1);
        assertThat(results.get(0).getItemId()).isEqualTo("fallback_item");
        assertThat(results.get(0).isDegraded()).isTrue();
        assertThat(results.get(0).getDegradedReason()).isEqualTo(DegradationReason.INFERENCE_TIMEOUT.name());
    }
}
