package com.scalestyle.gateway.service;

import com.fasterxml.jackson.core.type.TypeReference;
import com.fasterxml.jackson.databind.ObjectMapper;
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

import java.io.File;
import java.io.IOException;
import java.time.Duration;
import java.util.ArrayList;
import java.util.Collections;
import java.util.List;
import java.util.Map;
import java.util.concurrent.TimeUnit;
import java.util.stream.Collectors;

/**
 * Two-tier caching service for product metadata:
 * - L1 Cache: Local Caffeine cache (hot data, ~10k items)
 * - L2 Cache: Redis distributed cache (full dataset)
 * 
 * Benefits:
 * - Nanosecond access for hot products (L1 hit)
 * - Millisecond access for cold products (L2 hit)
 * - Automatic cache warming and TTL-based refresh
 * - Memory efficient (LRU eviction)
 */
@Slf4j
@Service
public class ProductCacheService {

    private static final String PRODUCT_KEY_PREFIX = "product:";
    private static final String ITEM_KEY_PREFIX = "item:";  // P0-2 Fix: Support loader/inference Redis schema
    private static final String REDIS_WARMED_FLAG = "product:cache:warmed";
    
    private final RedisTemplate<String, RecommendationDTO> redisTemplate;
    private final StringRedisTemplate stringRedisTemplate;
    
    @Value("${METADATA_PATH:/app/data/product_metadata.json}")
    private String metadataPath;
    
    @Value("${cache.local.max-size:10000}")
    private int localCacheMaxSize;
    
    @Value("${cache.local.ttl-minutes:10}")
    private int localCacheTtlMinutes;
    
    // L1: Local Caffeine cache with LRU eviction
    private LoadingCache<String, RecommendationDTO> localCache;
    
    public ProductCacheService(
            RedisTemplate<String, RecommendationDTO> redisTemplate,
            StringRedisTemplate stringRedisTemplate) {
        this.redisTemplate = redisTemplate;
        this.stringRedisTemplate = stringRedisTemplate;
    }
    
    @PostConstruct
    public void init() {
        // Initialize L1 local cache with automatic loading from L2
        localCache = Caffeine.newBuilder()
                .maximumSize(localCacheMaxSize)
                .expireAfterWrite(Duration.ofMinutes(localCacheTtlMinutes))
                .recordStats()  // Enable metrics
                .build(this::loadFromRedis);
        
        log.info("Initialized local cache: maxSize={}, ttl={}min", 
                localCacheMaxSize, localCacheTtlMinutes);
        
        // Warm up Redis if not already done
        if (!isRedisWarmed()) {
            warmupRedisFromJson();
        } else {
            log.info("Redis cache already warmed, skipping initialization");
        }
    }
    
    /**
     * Get product by ID with two-tier cache lookup.
     * Flow: L1 (local) → L2 (Redis) → Fallback (unknown product)
     */
    public RecommendationDTO getProduct(String articleId) {
        String paddedId = padArticleId(articleId);
        return localCache.get(paddedId);
    }
    
    /**
     * Batch get products (optimized for recommendation results).
     */
    public List<RecommendationDTO> getProducts(List<String> articleIds) {
        return articleIds.stream()
                .map(this::getProduct)
                .collect(Collectors.toList());
    }
    
    /**
     * Load from L2 (Redis). Called by Caffeine on L1 cache miss.
     * P0-2 Fix: Support both product:<id> (serialized object) and item:<id> (hash) schemas
     */
    private RecommendationDTO loadFromRedis(String paddedId) {
        // Try product:<id> first (original schema with serialized objects)
        String redisKey = PRODUCT_KEY_PREFIX + paddedId;
        
        try {
            RecommendationDTO product = redisTemplate.opsForValue().get(redisKey);
            
            if (product != null) {
                log.debug("L2 cache hit (product:) for: {}", paddedId);
                return product;
            }
            
            // P0-2 Fix: Fallback to item:<id> hash (loader/inference schema)
            String itemKey = ITEM_KEY_PREFIX + paddedId;
            Map<Object, Object> itemHash = stringRedisTemplate.opsForHash().entries(itemKey);
            
            if (itemHash != null && !itemHash.isEmpty()) {
                log.debug("L2 cache hit (item:) for: {}", paddedId);
                return buildFromItemHash(paddedId, itemHash);
            }
            
            log.debug("L2 cache miss for: {} (tried both product: and item:), returning fallback", paddedId);
            return createFallbackProduct(paddedId);
            
        } catch (Exception e) {
            log.warn("Failed to load from Redis for: {}, using fallback", paddedId, e);
            return createFallbackProduct(paddedId);
        }
    }
    
    /**
     * P0-2 Fix: Build RecommendationDTO from Redis hash (item:<id> schema).
     * This allows Gateway to work with data loaded by data-pipeline/loader.py
     */
    private RecommendationDTO buildFromItemHash(String articleId, Map<Object, Object> hash) {
        try {
            return RecommendationDTO.builder()
                    .itemId(articleId)
                    .name(getHashValue(hash, "product_type_name", "Unknown Product"))
                    .category(getHashValue(hash, "department_name", "General"))
                    .description(getHashValue(hash, "detail_desc", ""))
                    .price(parsePrice(getHashValue(hash, "price", "0")))
                    .imgUrl(getHashValue(hash, "image_url", "https://via.placeholder.com/150"))
                    .build();
        } catch (Exception e) {
            log.warn("Failed to build from hash for: {}", articleId, e);
            return createFallbackProduct(articleId);
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
     * Check if Redis cache has been warmed up.
     */
    private boolean isRedisWarmed() {
        try {
            return Boolean.TRUE.equals(stringRedisTemplate.hasKey(REDIS_WARMED_FLAG));
        } catch (Exception e) {
            log.warn("Failed to check Redis warm status", e);
            return false;
        }
    }
    
    /**
     * Warm up Redis cache from JSON metadata file.
     */
    private void warmupRedisFromJson() {
        log.info("Starting Redis cache warmup from: {}", metadataPath);
        long startTime = System.currentTimeMillis();
        
        try {
            File file = new File(metadataPath);
            if (!file.exists()) {
                log.warn("Metadata file not found at {}, skipping warmup", metadataPath);
                return;
            }
            
            ObjectMapper objectMapper = new ObjectMapper();
            Map<String, RecommendationDTO> products = objectMapper.readValue(
                    file, 
                    new TypeReference<Map<String, RecommendationDTO>>() {}
            );
            
            if (products.isEmpty()) {
                log.warn("No products found in metadata file");
                return;
            }
            
            // Use true Redis pipeline with RedisConnection for better performance
            // Old approach used redisTemplate inside callback, which may not pipeline correctly
            redisTemplate.executePipelined((org.springframework.data.redis.core.RedisCallback<Object>) connection -> {
                org.springframework.data.redis.serializer.RedisSerializer<String> keySerializer = 
                    redisTemplate.getStringSerializer();
                org.springframework.data.redis.serializer.RedisSerializer<RecommendationDTO> valueSerializer = 
                    (org.springframework.data.redis.serializer.RedisSerializer<RecommendationDTO>) redisTemplate.getValueSerializer();
                
                products.forEach((id, product) -> {
                    String redisKey = PRODUCT_KEY_PREFIX + id;
                    byte[] keyBytes = keySerializer.serialize(redisKey);
                    byte[] valueBytes = valueSerializer.serialize(product);
                    
                    if (keyBytes != null && valueBytes != null) {
                        connection.set(keyBytes, valueBytes);
                    }
                });
                return null;
            });
            
            // Set warmed flag with 7 days expiry
            stringRedisTemplate.opsForValue().set(
                    REDIS_WARMED_FLAG, 
                    "true", 
                    Duration.ofDays(7)
            );
            
            long elapsed = System.currentTimeMillis() - startTime;
            log.info("Redis cache warmup completed: {} products loaded in {}ms", 
                    products.size(), elapsed);
            
        } catch (IOException e) {
            log.error("Failed to warm Redis cache from JSON", e);
        } catch (Exception e) {
            log.error("Unexpected error during Redis warmup", e);
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
        if (rawId == null || rawId.length() >= 10) {
            return rawId;
        }
        return "0".repeat(10 - rawId.length()) + rawId;
    }
    
    /**
     * Create fallback product for unknown IDs.
     */
    private RecommendationDTO createFallbackProduct(String articleId) {
        return RecommendationDTO.builder()
                .itemId(articleId)
                .name("Unknown Product")
                .category("General")
                .description("Description unavailable")
                .price(0.00)
                .imgUrl("https://via.placeholder.com/150")
                .build();
    }
}
