package com.scalestyle.gateway.service;

import com.fasterxml.jackson.core.type.TypeReference;
import com.fasterxml.jackson.databind.ObjectMapper;
import com.scalestyle.gateway.dto.InferenceSearchRequest;
import com.scalestyle.gateway.dto.InferenceSearchResponse;
import com.scalestyle.gateway.dto.RecommendationDTO;
import io.github.resilience4j.circuitbreaker.annotation.CircuitBreaker;
import io.micrometer.core.instrument.Counter;
import io.micrometer.core.instrument.MeterRegistry;
import lombok.extern.slf4j.Slf4j;
import org.springframework.beans.factory.annotation.Autowired;
import org.springframework.context.annotation.Lazy;
import org.springframework.http.HttpEntity;
import org.springframework.http.HttpHeaders;
import org.springframework.http.MediaType;
import org.springframework.http.ResponseEntity;
import org.springframework.stereotype.Service;
import org.springframework.web.client.RestTemplate;

import java.time.Duration;
import java.util.List;
import java.util.concurrent.CompletableFuture;
import java.util.concurrent.Executor;
import java.util.concurrent.ExecutionException;
import java.util.stream.Collectors;

@Slf4j
@Service
public class RecommendationService {

    private static final String RECOMMENDATION_CACHE_PREFIX = "rec:v1:";
    private static final Duration CACHE_TTL = Duration.ofMinutes(5);
    
    private final RestTemplate restTemplate;
    private final String baseUrl;
    private final ProductCacheService productCacheService;
    // Use StringRedisTemplate + ObjectMapper instead of RedisTemplate<List>
    // to avoid Jackson default typing issues with generic collections
    private final org.springframework.data.redis.core.StringRedisTemplate recommendationCacheTemplate;
    private final ObjectMapper cacheObjectMapper;
    private final Executor inferenceExecutor;
    private final org.springframework.data.redis.core.StringRedisTemplate stringRedisTemplate;
    
    // Self-injection for AOP proxy to make @CircuitBreaker work in same-class calls
    private RecommendationService self;
    
    // Metrics for degradation monitoring
    private final Counter degradedTotalCounter;
    private final Counter raySuccessCounter;
    private final Counter rayFailureCounter;

    public RecommendationService(
            RestTemplate inferenceRestTemplate, 
            String inferenceBaseUrl,
            ProductCacheService productCacheService,
            org.springframework.data.redis.core.StringRedisTemplate recommendationCacheTemplate,
            ObjectMapper recommendationCacheObjectMapper,
            Executor inferenceExecutor,
            org.springframework.data.redis.core.StringRedisTemplate stringRedisTemplate,
            MeterRegistry meterRegistry) {
        this.restTemplate = inferenceRestTemplate;
        this.baseUrl = inferenceBaseUrl;
        this.productCacheService = productCacheService;
        this.recommendationCacheTemplate = recommendationCacheTemplate;
        this.cacheObjectMapper = recommendationCacheObjectMapper;
        this.inferenceExecutor = inferenceExecutor;
        this.stringRedisTemplate = stringRedisTemplate;
        
        // Initialize degradation metrics
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
    }
    
    /**
     * Self-injection setter for Spring AOP proxy.
     * This allows @CircuitBreaker annotation to work when called from within the same class.
     * @Lazy breaks circular reference during bean initialization.
     */
    @Autowired
    public void setSelf(@Lazy RecommendationService self) {
        this.self = self;
    }

    /**
     * Search with circuit breaker and timeout protection.
     * 
     * Flow:
     * 1. Try Ray inference (HTTP read-timeout=450ms provides timeout protection)
     * 2. If successful: cache result in Redis and return
     * 3. If timeout/error: CircuitBreaker triggers fallback to Redis cache or popular items
     * 4. If circuit is OPEN: skip Ray, go directly to fallback
     * 
     * Note: @TimeLimiter removed - HTTP client timeout (450ms) + CircuitBreaker is sufficient.
     * TimeLimiter without fallback would cause timeout exceptions to bypass degradation chain.
     * 
     * P2: Added intent parameter for multi-intent support (search/similar/outfit/trend).
     */
    public CompletableFuture<List<RecommendationDTO>> searchAsync(String query, String userId, String intent, int k, boolean debug) {
        // Use dedicated executor to isolate inference calls from common ForkJoinPool
        return CompletableFuture.supplyAsync(() -> {
            // Call via self proxy to ensure @CircuitBreaker AOP interceptor works
            return self.callRayInferenceWithCircuitBreaker(query, userId, intent, k, debug);
        }, inferenceExecutor);
    }
    
    /**
     * Synchronous Ray inference call with CircuitBreaker protection.
     * Must be public for Spring AOP proxy to work.
     * Called via self-injection to ensure AOP interceptor is triggered.
     * P2: Added intent parameter.
     */
    @CircuitBreaker(name = "ray", fallbackMethod = "rayInferenceFallback")
    public List<RecommendationDTO> callRayInferenceWithCircuitBreaker(String query, String userId, String intent, int k, boolean debug) {
        try {
            log.debug("Calling Ray inference for query: {}, intent: {}", query, intent);
            
            InferenceSearchRequest req = InferenceSearchRequest.builder()
                .query(query)
                .k(k)
                .debug(debug)
                .userId(userId)
                .intent(intent)
                .build();

            HttpHeaders headers = new HttpHeaders();
            headers.setContentType(MediaType.APPLICATION_JSON);
            HttpEntity<InferenceSearchRequest> entity = new HttpEntity<>(req, headers);
            
            ResponseEntity<InferenceSearchResponse> response = restTemplate.postForEntity(
                baseUrl + "/search",
                entity,
                InferenceSearchResponse.class
            );
            
            InferenceSearchResponse resp = response.getBody();
            
            if (resp == null || resp.getResults() == null) {
                log.warn("Ray returned empty response for query: {}", query);
                rayFailureCounter.increment();
                // Throw RuntimeException to be caught by Resilience4j
                throw new RuntimeException("Inference service returned empty response");
            }
            
            // Extract article IDs and fetch product details
            List<String> articleIds = resp.getResults().stream()
                    .map(item -> String.valueOf(item.getArticle_id()))
                    .collect(Collectors.toList());
            
            List<RecommendationDTO> results = productCacheService.getProducts(articleIds);
            
            // Map reason and reason_source from inference response to DTOs
            // Only Top-1 result has reason field (AI-generated explanation)
            for (int i = 0; i < results.size() && i < resp.getResults().size(); i++) {
                InferenceSearchResponse.ResultItem item = resp.getResults().get(i);
                RecommendationDTO dto = results.get(i);
                
                // Set reason and reasonSource (only Top-1 has these fields from inference)
                dto.setReason(item.getReason());
                dto.setReasonSource(item.getReasonSource());
                
                // Mark as Ray source
                dto.setSource("ray");
                dto.setDegraded(false);
            }
            
            // Cache result for future fallbacks
            String cacheKey = buildCacheKey(query, intent, k);
            try {
                // Serialize List<RecommendationDTO> to JSON string to avoid default typing issues
                String jsonValue = cacheObjectMapper.writeValueAsString(results);
                recommendationCacheTemplate.opsForValue().set(cacheKey, jsonValue, CACHE_TTL);
                log.debug("Cached Ray result: key={}, size={}", cacheKey, results.size());
            } catch (Exception e) {
                log.warn("Failed to cache Ray result: {}", e.getMessage());
            }
            
            raySuccessCounter.increment();
            log.info("Ray inference successful: query={}, results={}", query, results.size());
            
            return results;
            
        } catch (Exception e) {
            log.error("Ray inference failed: query={}, error={}", query, e.getMessage());
            rayFailureCounter.increment();
            // Don't wrap exception - let Resilience4j record-exceptions match the original type
            if (e instanceof RuntimeException) {
                throw (RuntimeException) e;
            }
            throw new RuntimeException("Ray inference call failed", e);
        }
    }
    
    /**
     * Fallback for Ray inference failures.
     * Tries: 1) Redis cache 2) Popular items
     * Must be public for CircuitBreaker fallback to work.
     */
    public List<RecommendationDTO> rayInferenceFallback(String query, String userId, String intent, int k, boolean debug, Throwable t) {
        log.warn("Ray inference fallback triggered for query: {}, intent: {}, reason: {}", query, intent, t.getClass().getSimpleName());
        degradedTotalCounter.increment();
        
        // Fix: Extract exception type for degradedReason field (for Grafana/Locust analysis)
        String degradedReason = t != null ? t.getClass().getSimpleName() : "Unknown";
        
        String cacheKey = buildCacheKey(query, intent, k);
        
        // Try to get cached result from previous successful Ray call
        try {
            // Deserialize JSON string to List<RecommendationDTO> using TypeReference
            String jsonValue = recommendationCacheTemplate.opsForValue().get(cacheKey);
            if (jsonValue != null && !jsonValue.isEmpty()) {
                List<RecommendationDTO> cached = cacheObjectMapper.readValue(
                    jsonValue, 
                    new TypeReference<List<RecommendationDTO>>() {}
                );
                
                if (cached != null && !cached.isEmpty()) {
                    log.info("Fallback: Using cached result for query: {}, size={}", query, cached.size());
                    
                    // Mark as degraded with reason
                    cached.forEach(dto -> {
                        dto.setSource("redis-cache");
                        dto.setDegraded(true);
                        dto.setDegradedReason(degradedReason);
                    });
                    
                    return cached;
                }
            }
        } catch (Exception e) {
            log.warn("Failed to get cached result: {}", e.getMessage());
        }
        
        // Last resort: popular items
        return getPopularItemsFallback(k, degradedReason);
    }

    /**
     * Get popular items as ultimate fallback.
     * These are pre-cached in Redis by the data pipeline.
     * Added degradedReason parameter for observability.
     */
    private List<RecommendationDTO> getPopularItemsFallback(int k, String degradedReason) {
        try {
            // Popular items are stored in Redis as a sorted set: global:popular
            java.util.Set<String> popularIds = stringRedisTemplate.opsForZSet()
                    .reverseRange("global:popular", 0, Math.min(k, 100) - 1);
            
            if (popularIds == null || popularIds.isEmpty()) {
                log.warn("No popular items found in Redis, returning empty fallback");
                return List.of();
            }
            
            List<String> topIds = popularIds.stream().limit(k).toList();
            List<RecommendationDTO> results = productCacheService.getProducts(topIds);
            
            // Mark as degraded popular items with reason
            results.forEach(dto -> {
                dto.setSource("popular-fallback");
                dto.setDegraded(true);
                dto.setDegradedReason(degradedReason);
            });
            
            log.info("Fallback: Returning {} popular items", results.size());
            return results;
            
        } catch (Exception e) {
            log.error("Failed to get popular items fallback", e);
            return List.of();
        }
    }
    /**
     * Synchronous wrapper for backward compatibility with existing controllers.
     * Added timeout to prevent thread pool exhaustion during load.
     */
    public List<RecommendationDTO> search(String query, String userId, String intent, int k, boolean debug) {
        CompletableFuture<List<RecommendationDTO>> future = null;
        try {
            // Add timeout to prevent blocking web threads indefinitely
            future = searchAsync(query, userId, intent, k, debug);
            return future.get(350, java.util.concurrent.TimeUnit.MILLISECONDS);
        } catch (java.util.concurrent.TimeoutException e) {
            // Timeout should trigger fallback, not return empty list
            // This provides better user experience and correct degradation semantics
            log.warn("Async search timed out after 350ms (thread pool pressure): query={}", query);
            
            // Cancel the future to release resources
            if (future != null) {
                future.cancel(true);
            }
            
            // Trigger fallback with TimeoutException
            // This allows:
            // 1. User gets cached/popular items instead of empty list
            // 2. Metrics track "timeout-induced fallback" separately
            // 3. degradedReason="TimeoutException" for observability
            return rayInferenceFallback(query, userId, intent, k, debug, e);
        } catch (ExecutionException e) {
            // Unwrap the original exception for proper Resilience4j tracking
            Throwable cause = e.getCause();
            log.error("Async search failed: {}", cause.getMessage());
            if (cause instanceof RuntimeException) {
                throw (RuntimeException) cause;
            }
            throw new RuntimeException("Search operation failed", cause);
        } catch (Exception e) {
            log.error("Async search failed", e);
            throw new RuntimeException("Search operation failed", e);
        }
    }
    
    /**
     * Build cache key for recommendation results.
     * Format: rec:v1:{intent}:{sha1(query)}:{k}
     * Uses SHA-1 instead of hashCode to avoid collision risk.
     * Intent dimension now functional (not hardcoded).
     */
    private String buildCacheKey(String query, String intent, int k) {
        String normalizedQuery = query.toLowerCase().trim();
        String querySha1 = computeSha1(normalizedQuery);
        String intentKey = (intent != null && !intent.isEmpty()) ? intent : "search";
        return String.format("%s%s:%s:%d", RECOMMENDATION_CACHE_PREFIX, intentKey, querySha1, k);
    }
    
    /**
     * Compute SHA-1 hash of input string.
     * More collision-resistant than Java hashCode().
     */
    private String computeSha1(String input) {
        try {
            java.security.MessageDigest digest = java.security.MessageDigest.getInstance("SHA-1");
            byte[] hash = digest.digest(input.getBytes(java.nio.charset.StandardCharsets.UTF_8));
            StringBuilder hexString = new StringBuilder();
            for (byte b : hash) {
                String hex = Integer.toHexString(0xff & b);
                if (hex.length() == 1) hexString.append('0');
                hexString.append(hex);
            }
            return hexString.toString();
        } catch (java.security.NoSuchAlgorithmException e) {
            // Fallback to hashCode if SHA-1 unavailable (unlikely)
            return String.valueOf(input.hashCode());
        }
    }
    
    /**
     * Get cache statistics for monitoring and debugging.
     */
    public java.util.Map<String, Object> getCacheStats() {
        return productCacheService.getCacheStats();
    }
}
