package com.scalestyle.gateway.service;

import com.fasterxml.jackson.databind.ObjectMapper;
import com.scalestyle.gateway.common.DegradationReason;
import com.scalestyle.gateway.dto.ImageSearchRequest;
import com.scalestyle.gateway.dto.ImageSearchResponse;
import com.scalestyle.gateway.dto.RecommendationDTO;
import io.github.resilience4j.circuitbreaker.CircuitBreakerRegistry;
import io.micrometer.core.instrument.simple.SimpleMeterRegistry;
import org.junit.jupiter.api.BeforeEach;
import org.junit.jupiter.api.DisplayName;
import org.junit.jupiter.api.Test;
import org.junit.jupiter.api.extension.ExtendWith;
import org.mockito.Mock;
import org.mockito.junit.jupiter.MockitoExtension;
import org.springframework.core.ParameterizedTypeReference;
import org.springframework.data.redis.core.StringRedisTemplate;
import org.springframework.data.redis.core.ValueOperations;
import org.springframework.data.redis.core.ZSetOperations;
import org.springframework.web.reactive.function.client.WebClient;
import reactor.core.publisher.Mono;

import java.util.LinkedHashSet;
import java.util.List;
import java.util.Map;

import static org.assertj.core.api.Assertions.assertThat;
import static org.mockito.ArgumentMatchers.*;
import static org.mockito.Mockito.lenient;
import static org.mockito.Mockito.*;

/**
 * Verifies the image search three-layer fallback contract:
 *
 * <ol>
 *   <li>Image success   → response returned as-is</li>
 *   <li>Image fails, query present → text fallback (searchAsync)</li>
 *   <li>Image fails, query present, text also fails → popular items</li>
 *   <li>Image fails, no query → popular items directly (no text path attempted)</li>
 * </ol>
 *
 * Layers 3 and 4 are the new paths added to close the "empty result on image failure
 * without query" gap in the graceful-degradation contract.
 */
@ExtendWith(MockitoExtension.class)
class ImageSearchFallbackTest {

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

    @BeforeEach
    @SuppressWarnings("unchecked")
    void setUp() {
        lenient().when(webClient.post()).thenReturn(requestBodyUriSpec);
        lenient().when(requestBodyUriSpec.uri(anyString())).thenReturn(requestBodySpec);
        lenient().when(requestBodySpec.contentType(any())).thenReturn(requestBodySpec);
        lenient().when(requestBodySpec.bodyValue(any())).thenReturn(requestHeadersSpec);
        lenient().when(requestHeadersSpec.retrieve()).thenReturn(responseSpec);

        lenient().when(recommendationCacheTemplate.opsForValue()).thenReturn(valueOperations);
        lenient().when(popularItemsTemplate.opsForZSet()).thenReturn(zSetOperations);

        lenient().when(productCacheService.getProducts(anyList())).thenAnswer(inv -> {
            List<String> ids = inv.getArgument(0);
            return ids.stream()
                    .map(id -> RecommendationDTO.builder().itemId(id).name("Product " + id).build())
                    .toList();
        });

        service = new RecommendationService(
                webClient,
                productCacheService,
                recommendationCacheTemplate,
                objectMapper,
                Runnable::run,   // inferenceExecutor (synchronous in tests)
                Runnable::run,   // cacheExecutor     (synchronous in tests)
                popularItemsTemplate,
                CircuitBreakerRegistry.ofDefaults(),
                new SimpleMeterRegistry()
        );
    }

    // ── Helpers ───────────────────────────────────────────────────────────────

    /** Stubs the image-search endpoint to return an error. */
    @SuppressWarnings("unchecked")
    private void stubImageSearchFailure() {
        when(responseSpec.bodyToMono(any(ParameterizedTypeReference.class)))
                .thenReturn(Mono.error(new RuntimeException("image-search unavailable")));
    }

    /** Stubs the image-search endpoint to return a successful response map. */
    @SuppressWarnings("unchecked")
    private void stubImageSearchSuccess() {
        Map<String, Object> body = Map.of(
                "items", List.of(),
                "k", 10,
                "mode", "text_to_image"
        );
        when(responseSpec.bodyToMono(any(ParameterizedTypeReference.class)))
                .thenReturn(Mono.just(body));
    }

    /** Stubs the popular-items sorted-set with the given IDs. */
    private void stubPopularItems(String... ids) {
        when(zSetOperations.reverseRange(argThat(key -> key != null && key.startsWith("popularity:materialized:24h:")), anyLong(), anyLong()))
                .thenReturn(new LinkedHashSet<>(List.of(ids)));
    }

    private ImageSearchRequest requestWithQuery(String query) {
        return ImageSearchRequest.builder()
                .mode("image_to_image")
                .imageHash("hash123")
                .query(query)
                .k(5)
                .build();
    }

    private ImageSearchRequest requestWithoutQuery() {
        return ImageSearchRequest.builder()
                .mode("image_to_image")
                .imageHash("hash123")
                .k(5)
                .build();
    }

    // ── Layer 1: success path ─────────────────────────────────────────────────

    @Test
    @DisplayName("Image success: response returned directly, not degraded")
    void imageSearch_success_returnsDirectResponse() {
        stubImageSearchSuccess();

        ImageSearchResponse result =
                service.imageSearchAsync(requestWithoutQuery()).join();

        assertThat(result.getStatus()).isEqualTo("success");
        assertThat(result.getDegraded()).isFalse();
        assertThat(result.getMode()).isEqualTo("text_to_image");
    }

    // ── Layer 3: image fail + query present + text also fails → popular items ──

    @Test
    @DisplayName("Image fails, text fallback fails: falls back to popular items with degraded=true")
    void imageSearch_bothPathsFail_fallsBackToPopularItems() {
        stubImageSearchFailure();
        // popular-items Redis is available
        stubPopularItems("pop1", "pop2", "pop3");
        // Rec-cache miss so searchAsync also falls through to popular items —
        // but searchAsync itself won't be reached here because both the image path
        // and the text fallback path will fail (Ray unavailable on both).
        // We need the text path (Ray) to also fail.
        // The responseSpec stub already returns a failure for any bodyToMono call,
        // which covers both the image path and the text/Ray path.

        ImageSearchResponse result = service.imageSearchAsync(
                requestWithQuery("red dress")).join();

        assertThat(result.getStatus()).isEqualTo("success");
        assertThat(result.getDegraded()).isTrue();
        assertThat(result.getMode()).isEqualTo("text_fallback");
        assertThat(result.getDegradedReason()).isEqualTo(DegradationReason.INFERENCE_UNAVAILABLE.name());
        assertThat(result.getItems()).isNotEmpty();
        assertThat(result.getItems()).allMatch(item -> "popular-fallback".equals(item.getSource()));
    }

    // ── Layer 2 (new): image fail + no query → popular items directly ──────────

    @Test
    @DisplayName("Image fails, no query: routes directly to popular items without attempting text path")
    void imageSearch_noQuery_fallsBackToPopularItems() {
        stubImageSearchFailure();
        stubPopularItems("pop1", "pop2");

        ImageSearchResponse result = service.imageSearchAsync(requestWithoutQuery()).join();

        assertThat(result.getStatus()).isEqualTo("success");
        assertThat(result.getDegraded()).isTrue();
        assertThat(result.getMode()).isEqualTo("popular_fallback");
        assertThat(result.getDegradedReason()).isEqualTo(DegradationReason.INFERENCE_UNAVAILABLE.name());
        assertThat(result.getItems()).isNotEmpty();
        assertThat(result.getItems()).allMatch(item -> "popular-fallback".equals(item.getSource()));
    }

    @Test
    @DisplayName("Image fails, no query: popular items response honours k contract")
    void imageSearch_noQuery_popularFallback_honoursK() {
        stubImageSearchFailure();
        // Supply more popular items than k so truncation is exercised
        stubPopularItems("p1", "p2", "p3", "p4", "p5", "p6", "p7", "p8", "p9", "p10");

        ImageSearchResponse result = service.imageSearchAsync(requestWithoutQuery()).join();

        assertThat(result.getItems().size()).isLessThanOrEqualTo(requestWithoutQuery().getK());
    }

    @Test
    @DisplayName("Image fails, no query, popular items empty: returns empty list (last resort) not exception")
    void imageSearch_noQuery_popularItemsEmpty_returnsEmptyList() {
        stubImageSearchFailure();
        when(zSetOperations.reverseRange(argThat(key -> key != null && key.startsWith("popularity:materialized:24h:")), anyLong(), anyLong()))
                .thenReturn(new LinkedHashSet<>());
        when(zSetOperations.reverseRange(argThat(key -> key != null && key.startsWith("popularity:materialized:7d:")), anyLong(), anyLong()))
                .thenReturn(new LinkedHashSet<>());  // both windows empty

        ImageSearchResponse result = service.imageSearchAsync(requestWithoutQuery()).join();

        // getPopularItemsFallbackMono already handles empty Redis gracefully
        assertThat(result).isNotNull();
        assertThat(result.getStatus()).isEqualTo("success");
        assertThat(result.getDegraded()).isTrue();
    }

    @Test
    @DisplayName("Image fails, no query, popular items Redis unavailable: completes without exception")
    void imageSearch_noQuery_popularItemsRedisDown_completes() {
        stubImageSearchFailure();
        when(zSetOperations.reverseRange(anyString(), anyLong(), anyLong()))
                .thenThrow(new RuntimeException("Redis unavailable"));

        // Should not throw — getPopularItemsFallbackMono has its own onErrorResume
        ImageSearchResponse result = service.imageSearchAsync(requestWithoutQuery()).join();

        assertThat(result).isNotNull();
    }
}
