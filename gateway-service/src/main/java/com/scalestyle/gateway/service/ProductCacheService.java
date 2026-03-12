package com.scalestyle.gateway.service;

import com.github.benmanes.caffeine.cache.Caffeine;
import com.github.benmanes.caffeine.cache.LoadingCache;
import com.github.benmanes.caffeine.cache.stats.CacheStats;
import com.scalestyle.gateway.dto.RecommendationDTO;
import jakarta.annotation.PostConstruct;
import lombok.extern.slf4j.Slf4j;
import org.springframework.beans.factory.annotation.Value;
import org.springframework.data.redis.core.RedisTemplate;
import org.springframework.data.redis.core.StringRedisTemplate;
import org.springframework.stereotype.Service;

import java.time.Duration;
import java.util.ArrayList;
import java.util.List;
import java.util.Map;
import java.util.Optional;

/**
 * Two-tier read-only caching service for product metadata:
 * <ul>
 *   <li>L1 Cache — in-process Caffeine cache (hot items, bounded LRU)</li>
 *   <li>L2 Cache — Redis distributed cache (full dataset)</li>
 * </ul>
 *
 * <p><b>Bootstrap contract:</b> Redis must already contain product metadata
 * (keys {@code product:<id>} or {@code item:<id>}) before serving begins.
 * Loading that data is the responsibility of a separate process such as the
 * data-pipeline bootstrap job, a Kubernetes Init Container, or an equivalent
 * pre-flight admin tool — never this serving pod.
 *
 * <p><b>Miss semantics:</b> When metadata is absent from Redis,
 * {@link #getProduct} returns {@link Optional#empty()} rather than a fabricated
 * placeholder.  Callers are responsible for deciding how to handle missing items
 * (typically: filter them from the result set and record a metric).  This makes
 * bootstrap gaps and Redis corruption visible in observability rather than
 * silently polluting recommendation responses with fake data.
 *
 * <p>Benefits:
 * <ul>
 *   <li>Sub-millisecond access for hot products (L1 hit)</li>
 *   <li>Millisecond access for cold products (L2 Redis hit)</li>
 *   <li>Honest absence signal on metadata miss — no phantom products</li>
 * </ul>
 */
@Slf4j
@Service
public class ProductCacheService {

    private static final String PRODUCT_KEY_PREFIX = "product:";
    private static final String ITEM_KEY_PREFIX = "item:";

    private final RedisTemplate<String, RecommendationDTO> redisTemplate;
    private final StringRedisTemplate stringRedisTemplate;

    @Value("${cache.local.max-size:10000}")
    private int localCacheMaxSize;
    
    @Value("${cache.local.ttl-minutes:10}")
    private int localCacheTtlMinutes;
    
    // L1: Local Caffeine cache with LRU eviction.
    // Value type is Optional<RecommendationDTO> so that genuine misses are
    // cached as Optional.empty() — Caffeine requires a non-null value from the
    // loader, and caching the miss avoids repeated Redis look-ups for absent IDs.
    private LoadingCache<String, Optional<RecommendationDTO>> localCache;
    
    public ProductCacheService(
            RedisTemplate<String, RecommendationDTO> redisTemplate,
            StringRedisTemplate stringRedisTemplate) {
        this.redisTemplate = redisTemplate;
        this.stringRedisTemplate = stringRedisTemplate;
    }
    
    /**
     * Creates the in-process L1 cache only. Called once after construction by Spring.
     * <ul>
     *   <li>Builds the Caffeine cache and wires it to the Redis-backed loader for L1 misses.</li>
     *   <li>Does <b>not</b> perform any Redis writes or bootstrap.</li>
     *   <li>Product metadata must be loaded into Redis by a separate process (data-pipeline,
     *       Init Container, or equivalent) before this pod serves traffic — see class-level Javadoc.</li>
     * </ul>
     */
    @PostConstruct
    public void init() {
        localCache = Caffeine.newBuilder()
                .maximumSize(localCacheMaxSize)
                .expireAfterWrite(Duration.ofMinutes(localCacheTtlMinutes))
                .recordStats()
                .build(this::loadFromRedis);

        log.info("Initialized local cache: maxSize={}, ttl={}min",
                localCacheMaxSize, localCacheTtlMinutes);
    }
    
    /**
     * Get product metadata by ID with two-tier cache lookup.
     * Flow: L1 (local Caffeine) → L2 (Redis) → Optional.empty() on miss.
     *
     * <p>Returns {@link Optional#empty()} when:
     * <ul>
     *   <li>Neither {@code product:<id>} nor {@code item:<id>} exists in Redis</li>
     *   <li>A Redis error occurs during lookup (logged at WARN)</li>
     * </ul>
     * Never fabricates a placeholder product — callers must handle absence explicitly.
     */
    public Optional<RecommendationDTO> getProduct(String articleId) {
        String paddedId = padArticleId(articleId);
        return localCache.get(paddedId);
    }

    /**
     * Batch get products, returning only IDs that have metadata in Redis.
     * Items with absent metadata are excluded from the returned list and
     * are observable: callers increment a miss counter and emit a WARN log
     * with the miss count, so bootstrap gaps surface in metrics and logs
     * without failing the request.
     */
    public List<RecommendationDTO> getProducts(List<String> articleIds) {
        List<RecommendationDTO> found = new ArrayList<>();
        int missingCount = 0;
        for (String articleId : articleIds) {
            Optional<RecommendationDTO> product = getProduct(articleId);
            if (product.isPresent()) {
                found.add(product.get());
            } else {
                missingCount++;
            }
        }
        if (missingCount > 0) {
            log.warn("Product metadata missing for {}/{} IDs in batch lookup",
                    missingCount, articleIds.size());
        }
        return found;
    }
    
    /**
     * Load from L2 (Redis). Called by Caffeine on L1 cache miss.
     * Supports both {@code product:<id>} (serialised object) and
     * {@code item:<id>} (hash) schemas so the Gateway works with data produced
     * by either the data-pipeline loader or the inference service bootstrap.
     *
     * <p>Returns {@link Optional#empty()} on genuine miss (key not found).
     * Caffeine caches this result to prevent hammering Redis for absent IDs.
     *
     * <p>Throws {@link RuntimeException} on Redis errors (connection, timeout, etc.)
     * so Caffeine <b>does not</b> cache the failure. The next request retries Redis,
     * ensuring fast recovery when Redis becomes available. A transient 30-second
     * Redis outage will not cause 10 minutes of degraded metadata lookups.
     */
    private Optional<RecommendationDTO> loadFromRedis(String paddedId) {
        String redisKey = PRODUCT_KEY_PREFIX + paddedId;
        String itemKey  = ITEM_KEY_PREFIX  + paddedId;

        try {
            RecommendationDTO product = redisTemplate.opsForValue().get(redisKey);
            if (product != null) {
                log.debug("L2 cache hit (product:) for: {}", paddedId);
                return Optional.of(product);
            }

            Map<Object, Object> itemHash = stringRedisTemplate.opsForHash().entries(itemKey);
            if (itemHash != null && !itemHash.isEmpty()) {
                log.debug("L2 cache hit (item:) for: {}", paddedId);
                return buildFromItemHash(paddedId, itemHash);
            }

            // Neither key exists — product is absent from Redis.  Cache the
            // empty Optional so subsequent requests don't hammer Redis for the
            // same missing ID within the TTL window.
            log.debug("Product metadata absent from Redis: article_id={} redis_key={} item_key={}",
                    paddedId, redisKey, itemKey);
            return Optional.empty();

        } catch (Exception e) {
            log.warn("Redis error loading product metadata: article_id={} error={}",
                    paddedId, e.getMessage());
            // DO NOT return Optional.empty() — throw so Caffeine does not cache this failure.
            // Next call will retry Redis, enabling fast recovery when Redis becomes available.
            throw new RuntimeException("Redis unavailable for product metadata: " + paddedId, e);
        }
    }
    
    /**
     * Build a {@link RecommendationDTO} from a Redis hash (the {@code item:<id>} schema
     * written by data-pipeline/loader.py).  {@code prod_name} is mapped from
     * {@code colour_group_name} by bootstrap_data.py.
     *
     * <p>Returns {@link Optional#empty()} if deserialisation fails, so callers
     * can treat a corrupt hash the same as a missing key.
     */
    private Optional<RecommendationDTO> buildFromItemHash(String articleId, Map<Object, Object> hash) {
        try {
            String name = getHashValue(hash, "prod_name", "");
            log.debug("Building from hash for {}: prod_name='{}', hash keys={}",
                    articleId, name, hash.keySet());

            return Optional.of(RecommendationDTO.builder()
                    .itemId(articleId)
                    .name(name)
                    .category(getHashValue(hash, "department_name", ""))
                    .description(getHashValue(hash, "detail_desc", ""))
                    .price(parsePrice(getHashValue(hash, "price", "0")))
                    .imgUrl(getHashValue(hash, "image_url", ""))
                    .build());
        } catch (Exception e) {
            log.warn("Failed to deserialise product hash: article_id={} error={}", articleId, e.getMessage());
            return Optional.empty();
        }
    }
    
    /**
     * Helper: Safely get string value from Redis hash.
     */
    private String getHashValue(Map<Object, Object> hash, String key, String defaultValue) {
        Object value = hash.get(key);
        return value != null ? value.toString() : defaultValue;
    }
    
    /**
     * Helper: Parse price from string, return 0.0 if invalid.
     */
    private double parsePrice(String priceStr) {
        try {
            return Double.parseDouble(priceStr);
        } catch (NumberFormatException e) {
            return 0.0;
        }
    }
    
    /**
     * Get cache statistics for monitoring.
     */
    public Map<String, Object> getCacheStats() {
        CacheStats stats = localCache.stats();
        return Map.of(
                "l1_size", localCache.estimatedSize(),
                "l1_hit_rate", stats.hitRate(),
                "l1_hit_count", stats.hitCount(),
                "l1_miss_count", stats.missCount(),
                "l1_eviction_count", stats.evictionCount(),
                "l1_load_success_count", stats.loadSuccessCount(),
                "l1_load_failure_count", stats.loadFailureCount(),
                "l1_avg_load_penalty_ms", stats.averageLoadPenalty() / 1_000_000.0
        );
    }
    
    /**
     * Invalidate local cache entry (for manual refresh).
     */
    public void invalidate(String articleId) {
        String paddedId = padArticleId(articleId);
        localCache.invalidate(paddedId);
        log.info("Invalidated local cache for product: {}", paddedId);
    }
    
    /**
     * Clear all local cache (keeps Redis intact).
     */
    public void clearLocalCache() {
        localCache.invalidateAll();
        log.info("Cleared all local cache entries");
    }
    
    /**
     * Pad article ID to 10 digits (H&M dataset format).
     */
    private String padArticleId(String rawId) {
        if (rawId == null || rawId.length() >= 10 || !rawId.chars().allMatch(Character::isDigit)) {
            return rawId;
        }
        return "0".repeat(10 - rawId.length()) + rawId;
    }
    
}
