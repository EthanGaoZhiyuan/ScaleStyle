package com.scalestyle.gateway.service;

import com.scalestyle.gateway.dto.RecommendationDTO;
import org.junit.jupiter.api.BeforeEach;
import org.junit.jupiter.api.DisplayName;
import org.junit.jupiter.api.Test;
import org.junit.jupiter.api.extension.ExtendWith;
import org.mockito.Mock;
import org.mockito.junit.jupiter.MockitoExtension;
import org.springframework.data.redis.core.HashOperations;
import org.springframework.data.redis.core.RedisTemplate;
import org.springframework.data.redis.core.StringRedisTemplate;
import org.springframework.data.redis.core.ValueOperations;
import org.springframework.test.util.ReflectionTestUtils;

import java.util.List;
import java.util.Map;
import java.util.Optional;

import static org.assertj.core.api.Assertions.assertThatThrownBy;
import static org.assertj.core.api.Assertions.assertThat;
import static org.mockito.ArgumentMatchers.anyString;
import static org.mockito.Mockito.*;

/**
 * Unit tests for ProductCacheService.
 *
 * Key invariant verified here: {@link ProductCacheService#init()} must only
 * initialise the in-process Caffeine cache.  It must NOT write to Redis.
 * Product metadata is a bootstrap concern owned by the data-pipeline job, not
 * by this serving pod.
 */
@ExtendWith(MockitoExtension.class)
class ProductCacheServiceTest {

    @Mock private RedisTemplate<String, RecommendationDTO> redisTemplate;
    @Mock private StringRedisTemplate stringRedisTemplate;

    @Mock private ValueOperations<String, RecommendationDTO> redisValueOps;
    @SuppressWarnings("rawtypes")
    @Mock private HashOperations hashOps;

    private ProductCacheService service;

    @BeforeEach
    void setUp() {
        service = new ProductCacheService(redisTemplate, stringRedisTemplate);
        // Inject @Value fields that Spring would normally bind at context startup.
        ReflectionTestUtils.setField(service, "localCacheMaxSize", 100);
        ReflectionTestUtils.setField(service, "localCacheTtlMinutes", 10);
    }

    // ── init() contract ───────────────────────────────────────────────────────

    @Test
    @DisplayName("init() builds Caffeine cache without writing to Redis")
    void init_doesNotWriteToRedis() {
        service.init();

        // No Redis writes must occur during Gateway pod startup.
        // Product metadata is expected to already be in Redis, loaded by the
        // data-pipeline bootstrap job or a Kubernetes Init Container.
        verifyNoInteractions(redisTemplate);
        verifyNoInteractions(stringRedisTemplate);
    }

    @Test
    @DisplayName("init() does not read the old REDIS_WARMED_FLAG sentinel key")
    void init_doesNotReadWarmupFlagKey() {
        service.init();

        // The product:cache:warmed sentinel key was part of the removed warmup
        // path.  Nothing should touch it in the serving pod.
        verify(stringRedisTemplate, never()).hasKey(anyString());
    }

    @Test
    @DisplayName("init() leaves the service ready to serve requests")
    void init_serviceIsReadyToServeAfterInit() {
        when(redisTemplate.opsForValue()).thenReturn(redisValueOps);
        when(redisValueOps.get("product:0000000001"))
                .thenReturn(RecommendationDTO.builder()
                        .itemId("0000000001")
                        .name("Blue Jeans")
                        .build());

        service.init();

        Optional<RecommendationDTO> result = service.getProduct("1");
        assertThat(result).isPresent();
        assertThat(result.get().getItemId()).isEqualTo("0000000001");
        assertThat(result.get().getName()).isEqualTo("Blue Jeans");
    }

    // ── Read path ─────────────────────────────────────────────────────────────

    @Test
    @DisplayName("getProduct() returns present Optional when product:<id> key exists")
    void getProduct_readsProductKeyFromRedis() {
        when(redisTemplate.opsForValue()).thenReturn(redisValueOps);
        when(redisValueOps.get("product:0000123456"))
                .thenReturn(RecommendationDTO.builder()
                        .itemId("0000123456")
                        .name("Red Dress")
                        .build());

        service.init();
        Optional<RecommendationDTO> result = service.getProduct("123456");

        assertThat(result).isPresent();
        assertThat(result.get().getItemId()).isEqualTo("0000123456");
        assertThat(result.get().getName()).isEqualTo("Red Dress");
        verify(redisValueOps).get("product:0000123456");
    }

    @Test
    @DisplayName("getProduct() falls back to item:<id> hash when product:<id> is absent")
    @SuppressWarnings("unchecked")
    void getProduct_fallsBackToItemHashWhenProductKeyAbsent() {
        when(redisTemplate.opsForValue()).thenReturn(redisValueOps);
        when(redisValueOps.get("product:0000000042")).thenReturn(null);
        when(stringRedisTemplate.opsForHash()).thenReturn(hashOps);
        when(hashOps.entries("item:0000000042")).thenReturn(Map.of(
                "prod_name", "Green Jacket",
                "department_name", "Outerwear",
                "price", "59.99"
        ));

        service.init();
        Optional<RecommendationDTO> result = service.getProduct("42");

        assertThat(result).isPresent();
        assertThat(result.get().getItemId()).isEqualTo("0000000042");
        assertThat(result.get().getName()).isEqualTo("Green Jacket");
        assertThat(result.get().getCategory()).isEqualTo("Outerwear");
        assertThat(result.get().getPrice()).isEqualTo(59.99);
    }

    @Test
    @DisplayName("getProduct() returns empty Optional when both Redis keys are absent")
    @SuppressWarnings("unchecked")
    void getProduct_returnsEmptyWhenBothKeysMissing() {
        when(redisTemplate.opsForValue()).thenReturn(redisValueOps);
        when(redisValueOps.get("product:0009999999")).thenReturn(null);
        when(stringRedisTemplate.opsForHash()).thenReturn(hashOps);
        when(hashOps.entries("item:0009999999")).thenReturn(Map.of());

        service.init();
        Optional<RecommendationDTO> result = service.getProduct("9999999");

        // Must not fabricate a placeholder — absence is the honest signal.
        assertThat(result).isEmpty();
    }

    @Test
    @DisplayName("getProduct() propagates Redis error so the miss is not cached")
    void getProduct_returnsEmptyOnRedisError() {
        when(redisTemplate.opsForValue()).thenReturn(redisValueOps);
        when(redisValueOps.get(anyString()))
                .thenThrow(new RuntimeException("Redis connection refused"));

        service.init();
        assertThatThrownBy(() -> service.getProduct("12345"))
                .isInstanceOf(RuntimeException.class)
                .hasMessageContaining("Redis unavailable for product metadata");
    }

    @Test
    @DisplayName("getProducts() returns only IDs that have metadata; missing IDs are filtered out")
    @SuppressWarnings("unchecked")
    void getProducts_filtersOutMissingProducts() {
        when(redisTemplate.opsForValue()).thenReturn(redisValueOps);
        // ID 1 has metadata, IDs 2 and 3 are absent from Redis.
        when(redisValueOps.get("product:0000000001"))
                .thenReturn(RecommendationDTO.builder().itemId("0000000001").name("Item A").build());
        when(redisValueOps.get("product:0000000002")).thenReturn(null);
        when(redisValueOps.get("product:0000000003")).thenReturn(null);
        when(stringRedisTemplate.opsForHash()).thenReturn(hashOps);
        when(hashOps.entries(anyString())).thenReturn(Map.of()); // item: schema also absent

        service.init();
        List<RecommendationDTO> results = service.getProducts(List.of("1", "2", "3"));

        // Only the ID with real metadata must appear — no "Unknown Product" placeholders.
        assertThat(results).hasSize(1);
        assertThat(results.get(0).getName()).isEqualTo("Item A");
    }

    @Test
    @DisplayName("getProducts() returns all IDs when all have metadata")
    void getProducts_returnsAllWhenAllPresent() {
        when(redisTemplate.opsForValue()).thenReturn(redisValueOps);
        when(redisValueOps.get("product:0000000001"))
                .thenReturn(RecommendationDTO.builder().itemId("0000000001").name("Item A").build());
        when(redisValueOps.get("product:0000000002"))
                .thenReturn(RecommendationDTO.builder().itemId("0000000002").name("Item B").build());

        service.init();
        List<RecommendationDTO> results = service.getProducts(List.of("1", "2"));

        assertThat(results).hasSize(2);
        assertThat(results).extracting(RecommendationDTO::getName)
                .containsExactlyInAnyOrder("Item A", "Item B");
    }

    // ── Article ID padding ────────────────────────────────────────────────────

    @Test
    @DisplayName("Article IDs shorter than 10 digits are left-padded with zeros")
    void getProduct_padsShortArticleId() {
        when(redisTemplate.opsForValue()).thenReturn(redisValueOps);
        when(redisValueOps.get("product:0000000007")).thenReturn(
                RecommendationDTO.builder().itemId("0000000007").name("Padded Item").build());

        service.init();
        Optional<RecommendationDTO> result = service.getProduct("7");

        verify(redisValueOps).get("product:0000000007");
        assertThat(result).isPresent();
        assertThat(result.get().getItemId()).isEqualTo("0000000007");
    }

    @Test
    @DisplayName("Non-numeric article IDs are not padded before lookup")
    void getProduct_doesNotPadNonNumericArticleId() {
        when(redisTemplate.opsForValue()).thenReturn(redisValueOps);
        when(redisValueOps.get("product:item_hot"))
                .thenReturn(RecommendationDTO.builder().itemId("item_hot").name("Hot Item").build());

        service.init();
        Optional<RecommendationDTO> result = service.getProduct("item_hot");

        verify(redisValueOps).get("product:item_hot");
        assertThat(result).isPresent();
        assertThat(result.get().getItemId()).isEqualTo("item_hot");
    }
}
