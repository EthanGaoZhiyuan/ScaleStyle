package com.scalestyle.gateway.service;

import com.fasterxml.jackson.core.type.TypeReference;
import com.fasterxml.jackson.databind.ObjectMapper;
import com.scalestyle.gateway.common.DegradationReason;
import com.scalestyle.gateway.dto.ImageSearchRequest;
import com.scalestyle.gateway.dto.ImageSearchResponse;
import com.scalestyle.gateway.dto.InferenceSearchRequest;
import com.scalestyle.gateway.dto.InferenceSearchResponse;
import com.scalestyle.gateway.dto.RecommendationDTO;
import io.github.resilience4j.circuitbreaker.CircuitBreaker;
import io.github.resilience4j.circuitbreaker.CallNotPermittedException;
import io.github.resilience4j.circuitbreaker.CircuitBreakerRegistry;
import io.github.resilience4j.reactor.circuitbreaker.operator.CircuitBreakerOperator;
import io.micrometer.core.instrument.Counter;
import io.micrometer.core.instrument.Gauge;
import io.micrometer.core.instrument.MeterRegistry;
import io.micrometer.core.instrument.Timer;
import io.opentelemetry.api.trace.Span;
import io.opentelemetry.api.trace.SpanContext;
import lombok.extern.slf4j.Slf4j;
import org.springframework.beans.factory.annotation.Autowired;
import org.springframework.beans.factory.annotation.Qualifier;
import org.springframework.beans.factory.annotation.Value;
import org.springframework.context.annotation.Lazy;
import org.springframework.http.MediaType;
import org.springframework.stereotype.Service;
import org.springframework.web.reactive.function.client.WebClient;
import reactor.core.publisher.Mono;
import reactor.core.scheduler.Scheduler;
import reactor.core.scheduler.Schedulers;

import java.time.Duration;
import java.time.Instant;
import java.util.ArrayList;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;
import java.util.Optional;
import java.util.UUID;
import java.util.concurrent.CompletableFuture;
import java.util.concurrent.Executor;
import java.util.concurrent.RejectedExecutionException;
import java.util.concurrent.atomic.AtomicReference;
import java.util.stream.Collectors;

@Slf4j
@Service
public class RecommendationService {

    private static final String RECOMMENDATION_CACHE_PREFIX = "rec:v1:";
    private static final Duration CACHE_TTL = Duration.ofMinutes(5);
    private static final Duration REQUEST_TIMEOUT = Duration.ofMillis(350);
    private static final String DEFAULT_POPULARITY_MATERIALIZED_PREFIX = "popularity:materialized";

    /**
     * Overread multiplier for metadata-miss tolerance.
     *
     * Both the Ray inference path and the popular-items fallback fetch
     * {@code k * K_OVERREAD_MULTIPLIER} candidates from their respective sources,
     * assemble only those with available Redis metadata, and then truncate to k.
     * This keeps the k-contract intact when a small number of items have missing
     * metadata, at the cost of slightly more upstream work.
     *
     * When metadata misses exceed the buffer (more than k items missing out of
     * k*2 candidates), a WARN log is emitted so operators can detect a Redis
     * bootstrap gap before it degrades end-user experience.
     */
    private static final int K_OVERREAD_MULTIPLIER = 2;

    /**
     * Dedicated scheduler for ALL blocking cache and product-materialization work,
     * backed by the {@code cacheExecutor} bean (see InferenceClientConfig).
     *
     * Thread boundary contract:
     *   Stage 1 – Ray inference I/O:  Netty event-loop threads (non-blocking)
     *   Stage 2 – Result enrichment:  cacheScheduler / rec-cache-* threads (blocking OK)
     *
     * Instance field rather than a static Schedulers.boundedElastic() so that:
     *   - pool sizing is controlled via recommendation.cache.executor.* properties
     *   - threads are named rec-cache-N for visibility in thread dumps / JVM metrics
     *   - saturation (AbortPolicy) is immediately observable and independently tunable
     *   - the pool does not compete with other boundedElastic users in the same JVM
     */
    private final Scheduler cacheScheduler;

    @Value("${recommendation.fallback.popularity.materialized-prefix:popularity:materialized}")
    private String fallbackPopularityMaterializedPrefix = DEFAULT_POPULARITY_MATERIALIZED_PREFIX;

    @Value("${recommendation.fallback.popularity.primary-window:24h}")
    private String fallbackPopularityPrimaryWindow = "24h";

    @Value("${recommendation.fallback.popularity.secondary-window:7d}")
    private String fallbackPopularitySecondaryWindow = "7d";

    @Value("${recommendation.fallback.popularity.window.1h.bucket-seconds:300}")
    private long popularityWindow1hBucketSeconds = 300L;

    @Value("${recommendation.fallback.popularity.window.24h.bucket-seconds:3600}")
    private long popularityWindow24hBucketSeconds = 3600L;

    @Value("${recommendation.fallback.popularity.window.7d.bucket-seconds:86400}")
    private long popularityWindow7dBucketSeconds = 86400L;

    private final WebClient webClient;
    private final ProductCacheService productCacheService;
    private final MeterRegistry meterRegistry;
    private final org.springframework.data.redis.core.StringRedisTemplate recommendationCacheTemplate;
    private final ObjectMapper cacheObjectMapper;
    private final Scheduler inferenceScheduler;
    private final org.springframework.data.redis.core.StringRedisTemplate stringRedisTemplate;
    private final CircuitBreaker circuitBreaker;
    private RecommendationService self;
    
    /**
     * Per-request miss ratio threshold for structured WARN log.
     * When a single request's miss ratio hits or exceeds this value, a log line with a
     * fixed key ({@code METADATA_MISS_RATIO_HIGH}) is emitted so log aggregators and
     * alerting rules can grep/filter for it without needing Prometheus.
     */
    private static final double MISS_RATIO_WARN_THRESHOLD = 0.3;

    /**
     * EMA smoothing factor for {@code recommendation_metadata_miss_ratio}.
     * 0.2 weights the most recent request at 20%, providing a fast-moving but
     * noise-resistant signal.  Lower values make the gauge lag more; higher values
     * make it react faster to individual noisy requests.
     */
    private static final double EMA_ALPHA = 0.2;

    private final Counter degradedTotalCounter;
    private final Counter raySuccessCounter;
    private final Counter rayFailureCounter;
    private final Counter metadataMissCounter;
    private final Counter requestedCandidatesCounter;
    private final Counter returnedCandidatesCounter;
    private final Timer queueWaitTimer;
    private final Timer inferenceHttpTimer;
    private final Timer metadataEnrichmentTimer;
    private final Timer fallbackTimer;

    /**
     * Exponential moving average of per-request metadata miss ratio (0.0–1.0).
     * Updated after every Ray-path assembly.  Exposed as the Micrometer gauge
     * {@code recommendation_metadata_miss_ratio} so a single Prometheus alert rule
     * on this gauge replaces manual ratio arithmetic across two counters.
     */
    private final AtomicReference<Double> missRatioEma = new AtomicReference<>(0.0);

    public RecommendationService(
            WebClient inferenceWebClient,
            ProductCacheService productCacheService,
            org.springframework.data.redis.core.StringRedisTemplate recommendationCacheTemplate,
            ObjectMapper recommendationCacheObjectMapper,
            @Qualifier("inferenceExecutor") Executor inferenceExecutor,
            @Qualifier("cacheExecutor")     Executor cacheExecutor,
            org.springframework.data.redis.core.StringRedisTemplate stringRedisTemplate,
            CircuitBreakerRegistry circuitBreakerRegistry,
            MeterRegistry meterRegistry) {
        this.webClient = inferenceWebClient;
        this.productCacheService = productCacheService;
        this.meterRegistry = meterRegistry;
        this.recommendationCacheTemplate = recommendationCacheTemplate;
        this.cacheObjectMapper = recommendationCacheObjectMapper;
        this.inferenceScheduler = Schedulers.fromExecutor(inferenceExecutor);
        this.cacheScheduler    = Schedulers.fromExecutor(cacheExecutor);
        this.stringRedisTemplate = stringRedisTemplate;
        this.circuitBreaker = circuitBreakerRegistry.circuitBreaker("ray");
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

        this.metadataMissCounter = Counter.builder("recommendation_metadata_miss_total")
                .description("Product items excluded from recommendations due to missing metadata in Redis")
                .tag("service", "gateway")
                .register(meterRegistry);
        this.requestedCandidatesCounter = Counter.builder("recommendation_requested_candidates_total")
                .description("Total candidate IDs looked up for product metadata (denominator for miss ratio by requested)")
                .tag("service", "gateway")
                .register(meterRegistry);
        this.returnedCandidatesCounter = Counter.builder("recommendation_returned_candidates_total")
                .description("Total candidates with metadata included in result set (denominator for miss ratio by returned)")
                .tag("service", "gateway")
                .register(meterRegistry);
        this.queueWaitTimer = Timer.builder("recommendation_phase_duration_seconds")
                .description("Recommendation request phase duration")
                .tag("service", "gateway")
                .tag("phase", "admission_queue_wait")
                .publishPercentileHistogram()
                .register(meterRegistry);
        this.inferenceHttpTimer = Timer.builder("recommendation_phase_duration_seconds")
                .description("Recommendation request phase duration")
                .tag("service", "gateway")
                .tag("phase", "inference_http")
                .publishPercentileHistogram()
                .register(meterRegistry);
        this.metadataEnrichmentTimer = Timer.builder("recommendation_phase_duration_seconds")
                .description("Recommendation request phase duration")
                .tag("service", "gateway")
                .tag("phase", "metadata_enrichment")
                .publishPercentileHistogram()
                .register(meterRegistry);
        this.fallbackTimer = Timer.builder("recommendation_phase_duration_seconds")
                .description("Recommendation request phase duration")
                .tag("service", "gateway")
                .tag("phase", "fallback")
                .publishPercentileHistogram()
                .register(meterRegistry);
        Gauge.builder("recommendation_metadata_miss_ratio", missRatioEma, AtomicReference::get)
                .description("Exponential moving average (alpha=0.2) of per-request metadata miss ratio (0.0–1.0). "
                        + "Alert when sustained value exceeds bootstrap-gap threshold.")
                .tag("service", "gateway")
                .register(meterRegistry);
    }
    
    /**
     * Self-injection for AOP proxy support.
     */
    @Autowired
    public void setSelf(@Lazy RecommendationService self) {
        this.self = self;
    }

    /**
     * Asynchronous recommendation search with end-to-end reactive pipeline.
     * Circuit breaker, timeout, and error handling are all reactive operators.
     * Falls back to cached or popular items on failure.
     *
     * Cache behaviour depends on whether the request is personalized:
     * - Non-personalized (userId null/blank): results may be written to and read from
     *   the shared query-level cache keyed by (query, intent, k).
     * - Personalized (userId present): the shared cache is bypassed entirely in both
     *   directions.  Personalized results must never be stored in or served from a
     *   key that does not include userId, because doing so would expose one user's
     *   recommendations to a different user.
     */
    public CompletableFuture<List<RecommendationDTO>> searchAsync(String query, String userId, String intent, int k, boolean debug) {
        boolean personalized = isPersonalized(userId);
        RequestContext ctx = new RequestContext(UUID.randomUUID().toString(), currentTraceId(), System.nanoTime());
        return Mono.defer(() -> {
                    queueWaitTimer.record(Duration.ofNanos(System.nanoTime() - ctx.enqueuedAtNanos()));
                    log.info("request_id={} trace_id={} phase=admission_queue_wait outcome=scheduled query_hash={} k={} personalized={}",
                            ctx.requestId(), ctx.traceId(), queryHash(query), k, personalized);
                    return callRayInferenceReactive(query, userId, intent, k, debug, personalized, ctx);
                })
                .transform(CircuitBreakerOperator.of(circuitBreaker))
                .timeout(REQUEST_TIMEOUT)
                .subscribeOn(inferenceScheduler)
                .onErrorResume(throwable -> {
                    DegradationReason degradedReason = mapInferenceDegradationReason(throwable);
                    log.warn("request_id={} trace_id={} phase=inference_http outcome=degraded query_hash={} k={} personalized={} degrade_reason={}",
                            ctx.requestId(), ctx.traceId(), queryHash(query), k, personalized, degradedReason.name());
                    degradedTotalCounter.increment();
                    recordDegradedReason(degradedReason);
                    return getFallbackResultsMono(query, intent, k, degradedReason, personalized, ctx);
                })
                .toFuture();
    }
    
    /**
     * Core Ray inference call as pure reactive pipeline.
     * No blocking - returns Mono for end-to-end reactive composition.
     *
     * @param personalized true when userId is present and Ray may return user-specific
     *                     results.  When true, the shared rec-cache is not written so
     *                     that another user's query never hits this user's ranked results.
     */
    private Mono<List<RecommendationDTO>> callRayInferenceReactive(
            String query, String userId, String intent, int k, boolean debug, boolean personalized, RequestContext ctx) {
        // Request K_OVERREAD_MULTIPLIER * k candidates from Ray so that metadata
        // misses on a minority of items can be covered from the extra headroom,
        // keeping the final result count close to the caller's requested k.
        int fetchK = k * K_OVERREAD_MULTIPLIER;
        log.debug("request_id={} trace_id={} phase=inference_http outcome=start intent={} personalized={} fetch_k={}",
                ctx.requestId(), ctx.traceId(), intent, personalized, fetchK);
        InferenceSearchRequest req = InferenceSearchRequest.builder()
            .query(query)
            .k(fetchK)
            .debug(debug)
            .userId(userId)
            .intent(intent)
            .build();

        long httpStartNanos = System.nanoTime();
        return webClient.post()
                .uri("/search")
                .contentType(MediaType.APPLICATION_JSON)
                .bodyValue(req)
                .retrieve()
                .bodyToMono(InferenceSearchResponse.class)
                // Stage 1 complete: response arrived on Netty event-loop thread.
                // Stage 2: enrich with product metadata and write rec-cache.
                // Both are blocking (Caffeine L1 + Redis L2 + Redis write), so we
                // hand off explicitly to CACHE_SCHEDULER before touching them.
                .flatMap(resp -> {
                    if (resp == null || resp.getResults() == null) {
                        log.warn("Ray returned empty response: query_hash={}, k={}, personalized={}",
                                queryHash(query), k, personalized);
                        rayFailureCounter.increment();
                        return Mono.error(new RuntimeException("Inference service returned empty response"));
                    }

                    List<String> articleIds = resp.getResults().stream()
                            .map(item -> String.valueOf(item.getArticle_id()))
                            .collect(Collectors.toList());

                    // All blocking work (L1/L2 cache read + rec-cache write) in one
                    // callable executed on cacheScheduler (rec-cache-* threads), never
                    // on the Netty event loop or the inferenceScheduler.
                    return Mono.fromCallable(() -> {
                        long enrichStartNanos = System.nanoTime();
                        try {
                        // Index inference results by article ID for O(1) join.
                        // LinkedHashMap preserves Ray's rank order for the assembly loop below.
                        Map<String, InferenceSearchResponse.ResultItem> inferenceByArticleId =
                                new LinkedHashMap<>();
                        for (InferenceSearchResponse.ResultItem inferenceItem : resp.getResults()) {
                            inferenceByArticleId.put(
                                    String.valueOf(inferenceItem.getArticle_id()), inferenceItem);
                        }

                        // Enrich with product metadata, skipping items whose metadata is
                        // absent from Redis.  Missing metadata is surfaced as a metric and
                        // WARN log — never papered over with a fabricated placeholder object.
                        List<RecommendationDTO> results = new ArrayList<>();
                        int metadataMissCount = 0;
                        for (String articleId : articleIds) {
                            Optional<RecommendationDTO> productOpt =
                                    productCacheService.getProduct(articleId);
                            if (productOpt.isEmpty()) {
                                metadataMissCount++;
                                log.debug("Product metadata absent, excluding from response: article_id={}", articleId);
                                continue;
                            }
                            RecommendationDTO dto = productOpt.get();
                            InferenceSearchResponse.ResultItem inferenceItem =
                                    inferenceByArticleId.get(articleId);
                            if (inferenceItem != null) {
                                dto.setReason(inferenceItem.getReason());
                                dto.setReasonSource(inferenceItem.getReasonSource());
                            }
                            dto.setSource("ray");
                            dto.setDegraded(false);
                            results.add(dto);
                        }
                        if (metadataMissCount > 0) {
                            metadataMissCounter.increment(metadataMissCount);
                        }
                        requestedCandidatesCounter.increment(articleIds.size());
                        returnedCandidatesCounter.increment(results.size());

                        // Per-request miss ratio: update EMA gauge and emit structured WARN
                        // when ratio exceeds the operational threshold.
                        double missRatio = articleIds.isEmpty() ? 0.0
                                : (double) metadataMissCount / articleIds.size();
                        missRatioEma.updateAndGet(prev -> EMA_ALPHA * missRatio + (1 - EMA_ALPHA) * prev);

                        if (missRatio >= MISS_RATIO_WARN_THRESHOLD) {
                            // Fixed log key METADATA_MISS_RATIO_HIGH for grep/alert in log aggregators.
                            // Fires when a single request loses >= 30% of candidates to metadata misses.
                            log.warn("METADATA_MISS_RATIO_HIGH: query_hash={} k={} personalized={} "
                                            + "miss_ratio={} miss_count={} total_candidates={} returned={}",
                                    queryHash(query), k, personalized,
                                    String.format("%.2f", missRatio),
                                    metadataMissCount, articleIds.size(), results.size());
                        } else if (metadataMissCount > 0) {
                            log.debug("Product metadata missing for {}/{} candidates",
                                    metadataMissCount, articleIds.size());
                        }

                        // Truncate to the caller's requested k after overread assembly.
                        // Overread gave us up to fetchK=k*2 assembled candidates; we only
                        // expose k to the caller to honour the contract.
                        if (results.size() > k) {
                            results = new ArrayList<>(results.subList(0, k));
                        }

                        // Surface residual truncation: metadata misses exceeded the overread
                        // buffer (> k items out of fetchK candidates had no Redis metadata).
                        if (results.size() < k) {
                            log.warn("Recommendations still truncated after {}x overread: "
                                    + "query_hash={}, personalized={}, requested_k={}, "
                                    + "candidates_fetched={}, returned={}, miss_count={}",
                                    K_OVERREAD_MULTIPLIER, queryHash(query), personalized,
                                    k, articleIds.size(), results.size(), metadataMissCount);
                        }

                        // Write to shared rec-cache ONLY for non-personalized requests.
                        // Personalized results are specific to userId; caching them under
                        // a key that excludes userId would cause cross-user cache pollution:
                        // the next non-personalized (or differently-personalized) request
                        // for the same query would be served user-specific ranked results.
                        if (!personalized) {
                            try {
                                String cacheKey = buildCacheKey(query, intent, k);
                                String json = cacheObjectMapper.writeValueAsString(results);
                                recommendationCacheTemplate.opsForValue().set(cacheKey, json, CACHE_TTL);
                                log.debug("Cached Ray result: key={}, size={}", cacheKey, results.size());
                            } catch (Exception e) {
                                log.warn("Failed to cache Ray result: query_hash={}, k={}, error={}",
                                        queryHash(query), k, e.getMessage());
                            }
                        } else {
                            log.debug("Skipped shared rec-cache write for personalized request");
                        }

                            return results;
                        } finally {
                            metadataEnrichmentTimer.record(Duration.ofNanos(System.nanoTime() - enrichStartNanos));
                        }
                    }).subscribeOn(cacheScheduler);
                })
                .doOnSuccess(results -> {
                    inferenceHttpTimer.record(Duration.ofNanos(System.nanoTime() - httpStartNanos));
                    raySuccessCounter.increment();
                    log.info("request_id={} trace_id={} phase=inference_http outcome=success results={}",
                            ctx.requestId(), ctx.traceId(), results.size());
                })
                .doOnError(e -> {
                    inferenceHttpTimer.record(Duration.ofNanos(System.nanoTime() - httpStartNanos));
                    log.error("request_id={} trace_id={} phase=inference_http outcome=error query_hash={} k={} personalized={} error={}",
                            ctx.requestId(), ctx.traceId(), queryHash(query), k, personalized, e.getMessage());
                    rayFailureCounter.increment();
                });
    }
    
    /**
     * Fallback handler for inference failures.
     *
     * Thread boundary: all Redis reads happen on cacheScheduler (rec-cache-* threads)
     * via Mono.fromCallable, so this method is safe to call from any reactive context
     * (including Netty threads).
     *
     * @param personalized when true the shared query-level cache is skipped entirely and
     *                     the method falls through directly to popular items.  This prevents
     *                     a personalized user's degraded fallback from being served results
     *                     that were cached for a different user's earlier personalized request.
     *                     The shared cache key does not contain userId, so any hit could be
     *                     stale, wrong-user data — it is not safe to serve it to a userId-bearing
     *                     request regardless of how fresh the cached entry is.
     */
    private Mono<List<RecommendationDTO>> getFallbackResultsMono(
            String query, String intent, int k, DegradationReason triggerReason, boolean personalized, RequestContext ctx) {
        long fallbackStartNanos = System.nanoTime();
        log.debug("Using fallback: query_hash={}, k={}, personalized={}, degrade_reason={}",
                queryHash(query), k, personalized, triggerReason.name());

        // For personalized requests: skip the shared query cache and go directly to
        // popular items.  The shared cache may contain results that were ranked for a
        // different user (cross-user pollution).  Popular items are safe because they are
        // the same for everyone and are never user-scoped.
        if (personalized) {
            log.debug("Personalized request: bypassing shared query cache, routing to popular items");
            return getPopularItemsFallbackMono(k, triggerReason)
                    .doFinally(signalType -> fallbackTimer.record(Duration.ofNanos(System.nanoTime() - fallbackStartNanos)));
        }

        String cacheKey = buildCacheKey(query, intent, k);

        // Blocking Redis read isolated to cacheScheduler (rec-cache-* threads).
        // Returns null (→ Mono.empty) on cache miss so switchIfEmpty can route to popular items.
        return Mono.fromCallable(() -> {
            String jsonValue = recommendationCacheTemplate.opsForValue().get(cacheKey);
            if (jsonValue == null || jsonValue.isEmpty()) {
                return null;
            }
            List<RecommendationDTO> cached = cacheObjectMapper.readValue(
                    jsonValue, new TypeReference<List<RecommendationDTO>>() {});
            if (cached == null || cached.isEmpty()) {
                return null;
            }
            recordDegradedReason(DegradationReason.STALE_DATA_ALLOWED);
            cached.forEach(dto -> {
                dto.setSource("redis-cache");
                dto.setDegraded(true);
                dto.setDegradedReason(DegradationReason.STALE_DATA_ALLOWED.name());
            });
            log.debug("Fallback: cached result size={}", cached.size());
            return cached;
        })
        .subscribeOn(cacheScheduler)
        .switchIfEmpty(Mono.defer(() -> {
            recordDegradedReason(DegradationReason.CACHE_MISS);
            return getPopularItemsFallbackMono(k, DegradationReason.CACHE_MISS);
        }))
        .onErrorResume(e -> {
            DegradationReason cacheFallbackReason = mapRedisDegradationReason(e);
            log.warn("Fallback cache read failed [routing to popular items]: "
                            + "query_hash={}, k={}, personalized={}, degrade_reason={}, cache_error={}",
                    queryHash(query), k, personalized, cacheFallbackReason.name(), e.getMessage());
            recordDegradedReason(cacheFallbackReason);
            return getPopularItemsFallbackMono(k, cacheFallbackReason);
        })
        .doFinally(signalType -> {
            fallbackTimer.record(Duration.ofNanos(System.nanoTime() - fallbackStartNanos));
            log.info("request_id={} trace_id={} phase=fallback outcome={} personalized={}",
                    ctx.requestId(), ctx.traceId(), triggerReason.name(), personalized);
        });
    }

    /**
     * Last-resort fallback: rolling online popularity from a materialized Redis ZSET.
     *
     * Thread boundary: all Redis and cache reads happen on cacheScheduler (rec-cache-* threads).
     */
    private Mono<List<RecommendationDTO>> getPopularItemsFallbackMono(int k, DegradationReason degradedReason) {
        return Mono.fromCallable(() -> {
            // Fetch K_OVERREAD_MULTIPLIER * k candidates from the sorted-set so that
            // metadata misses do not silently shrink the fallback result below k.
            // The ZSet scan is capped at 100 to avoid unbounded range operations.
            int popularFetchCount = Math.min(k * K_OVERREAD_MULTIPLIER, 100);
            PopularityFallbackSource popularitySource = resolveFallbackPopularitySource(popularFetchCount);
            java.util.Set<String> popularIds = popularitySource.ids();
            if (popularIds == null || popularIds.isEmpty()) {
                log.warn("No materialized popularity window available [returning empty fallback]: k={}, degrade_reason={}, primary_window={}, secondary_window={}",
                        k, degradedReason.name(), fallbackPopularityPrimaryWindow, fallbackPopularitySecondaryWindow);
                return List.<RecommendationDTO>of();
            }
            // Pass all fetched candidates; getProducts filters to those with metadata.
            // Truncate to k afterwards — do NOT limit before the metadata filter or
            // the overread headroom is wasted.
            List<RecommendationDTO> results = productCacheService.getProducts(
                    new ArrayList<>(popularIds));
            if (results.size() > k) {
                results = new ArrayList<>(results.subList(0, k));
            }
            if (results.size() < k) {
                log.warn("Popular items fallback truncated by metadata misses: "
                                + "k={}, degrade_reason={}, returned={}",
                        k, degradedReason.name(), results.size());
            }
            results.forEach(dto -> {
                dto.setSource("popular-fallback");
                dto.setDegraded(true);
                dto.setDegradedReason(degradedReason.name());
            });
            recordFallbackPopularityWindow(popularitySource.window(), "hit");
            log.info("fallback_source_window={} fallback_source_key={} degrade_reason={} returned={} requested_k={}",
                    popularitySource.window(), popularitySource.key(), degradedReason.name(), results.size(), k);
            return results;
        })
        .subscribeOn(cacheScheduler)
        .onErrorResume(e -> {
            log.error("Popular items fallback failed: k={}, degrade_reason={}, error={}",
                    k, degradedReason.name(), e.getMessage(), e);
            return Mono.just(List.of());
        });
    }

    private PopularityFallbackSource resolveFallbackPopularitySource(int fetchCount) {
        String primaryKey = buildFallbackPopularityKey(fallbackPopularityPrimaryWindow);
        java.util.Set<String> primaryIds = stringRedisTemplate.opsForZSet()
                .reverseRange(primaryKey, 0, fetchCount - 1);
        if (primaryIds != null && !primaryIds.isEmpty()) {
            return new PopularityFallbackSource(fallbackPopularityPrimaryWindow, primaryKey, primaryIds);
        }
        recordFallbackPopularityWindow(fallbackPopularityPrimaryWindow, "empty");

        String secondaryWindow = fallbackPopularitySecondaryWindow;
        if (secondaryWindow == null || secondaryWindow.isBlank()
                || secondaryWindow.equals(fallbackPopularityPrimaryWindow)) {
            return new PopularityFallbackSource(fallbackPopularityPrimaryWindow, primaryKey, primaryIds);
        }

        String secondaryKey = buildFallbackPopularityKey(secondaryWindow);
        java.util.Set<String> secondaryIds = stringRedisTemplate.opsForZSet()
                .reverseRange(secondaryKey, 0, fetchCount - 1);
        if (secondaryIds != null && !secondaryIds.isEmpty()) {
            return new PopularityFallbackSource(secondaryWindow, secondaryKey, secondaryIds);
        }
        recordFallbackPopularityWindow(secondaryWindow, "empty");
        return new PopularityFallbackSource(secondaryWindow, secondaryKey, secondaryIds);
    }

    private String buildFallbackPopularityKey(String window) {
        long bucketSeconds = bucketSecondsForWindow(window);
        long bucketStart = (Instant.now().getEpochSecond() / bucketSeconds) * bucketSeconds;
        return fallbackPopularityMaterializedPrefix + ":" + window + ":" + bucketStart;
    }

    private long bucketSecondsForWindow(String window) {
        return switch (window) {
            case "1h" -> popularityWindow1hBucketSeconds;
            case "24h" -> popularityWindow24hBucketSeconds;
            case "7d" -> popularityWindow7dBucketSeconds;
            default -> throw new IllegalArgumentException("Unsupported fallback popularity window: " + window);
        };
    }

    private void recordFallbackPopularityWindow(String window, String outcome) {
        meterRegistry.counter(
                "recommendation_fallback_popularity_window_total",
                "service", "gateway",
                "window", window,
                "outcome", outcome
        ).increment();
    }
    


    /**
     * Asynchronous image search with end-to-end reactive pipeline.
     * Circuit breaker, timeout, and inference bulkhead are applied on the image path.
     *
     * <h3>Three-layer fallback contract</h3>
     * <ol>
     *   <li><b>Text fallback</b> (query present): delegates to {@link #searchAsync},
     *       which re-applies the full resiliency contract — circuit breaker +
     *       {@code REQUEST_TIMEOUT} + inference bulkhead + its own fallback to popular
     *       items.  {@code callRayInferenceReactive()} must NOT be called here directly
     *       because it carries no circuit-breaker guard, no timeout, and no bulkhead.</li>
     *
     *   <li><b>Popular-items fallback</b> (no query, or text fallback also fails):
     *       returns globally popular items from Redis via
     *       {@link #getPopularItemsFallbackMono}.  This ensures the image path honours
     *       the same "partial success rather than empty result" contract as the text
     *       path.  {@code mode="popular_fallback", status="success", degraded=true}.</li>
     *
     *   <li><b>Empty result</b> (popular-items Redis also unavailable): last resort,
     *       returned only when {@code getPopularItemsFallbackMono} itself throws — an
     *       extreme edge case that is already guarded internally by that method.</li>
     * </ol>
     */
    public CompletableFuture<ImageSearchResponse> imageSearchAsync(ImageSearchRequest request) {
        int k = request.getK() != null ? request.getK() : 10;

        return callImageSearchReactive(request, k)
                .transform(CircuitBreakerOperator.of(circuitBreaker))
                .timeout(REQUEST_TIMEOUT)
                .subscribeOn(inferenceScheduler)
                .onErrorResume(throwable -> {
                    DegradationReason imageFailReason = mapInferenceDegradationReason(throwable);
                    log.warn("Image search failed or timed out: mode={}, k={}, degrade_reason={}",
                            request.getMode(), k, imageFailReason.name());
                    degradedTotalCounter.increment();
                    recordDegradedReason(imageFailReason);

                    if (request.getQuery() != null && !request.getQuery().isBlank()) {
                        // Layer 1: text fallback — carries full resiliency guards.
                        return Mono.fromFuture(
                                        searchAsync(
                                                request.getQuery(),
                                                request.getUserId(),
                                                "search",
                                                k,
                                                Boolean.TRUE.equals(request.getDebug())
                                        )
                                )
                                .map(items -> ImageSearchResponse.builder()
                                        .items(items)
                                        .k(k)
                                        .mode("text_fallback")
                                        .status("success")
                                        .degraded(true)
                                        .degradedReason(imageFailReason.name())
                                        .build())
                                .onErrorResume(e -> {
                                    DegradationReason textFailReason = mapInferenceDegradationReason(e);
                                    // Layer 2: text fallback also failed — route to popular items.
                                    log.warn("Text fallback failed after image search failure "
                                                    + "[routing to popular items]: mode={}, k={}, "
                                                    + "imageError={}, textError={}",
                                            request.getMode(), k,
                                            imageFailReason.name(), textFailReason.name());
                                    recordDegradedReason(textFailReason);
                                    return getPopularItemsFallbackMono(k, textFailReason)
                                            .map(items -> ImageSearchResponse.builder()
                                                    .items(items)
                                                    .k(k)
                                                    .mode("popular_fallback")
                                                    .status("success")
                                                    .degraded(true)
                                                    .degradedReason(textFailReason.name())
                                                    .build());
                                });
                    }

                    // Layer 2 (no query): skip text path entirely, go straight to popular items.
                    log.warn("Image search failed with no query [routing to popular items]: mode={}, k={}, error={}",
                            request.getMode(), k, imageFailReason.name());
                    return getPopularItemsFallbackMono(k, imageFailReason)
                            .map(items -> ImageSearchResponse.builder()
                                    .items(items)
                                    .k(k)
                                    .mode("popular_fallback")
                                    .status("success")
                                    .degraded(true)
                                    .degradedReason(imageFailReason.name())
                                    .build());
                })
                .toFuture();
    }
    
    /**
     * Core image search call as pure reactive pipeline.
     * No blocking - returns Mono for end-to-end reactive composition.
     */
    private Mono<ImageSearchResponse> callImageSearchReactive(ImageSearchRequest request, int k) {
        log.debug("Calling image search: mode={}", request.getMode());
        
        return webClient.post()
                .uri("/search/image")
                .contentType(MediaType.APPLICATION_JSON)
                .bodyValue(request)
                .retrieve()
                .bodyToMono(new org.springframework.core.ParameterizedTypeReference<Map<String, Object>>() {})
                .flatMap(body -> {
                    if (body == null) {
                        return Mono.error(new RuntimeException("Inference image search returned empty response"));
                    }

                    @SuppressWarnings("unchecked")
                    List<Map<String, Object>> rawItems = (List<Map<String, Object>>) body.get("items");
                    List<RecommendationDTO> items = rawItems != null ? 
                        cacheObjectMapper.convertValue(rawItems, new TypeReference<List<RecommendationDTO>>() {}) : 
                        List.of();

                    ImageSearchResponse response = ImageSearchResponse.builder()
                            .items(items)
                            .k((Integer) body.getOrDefault("k", k))
                            .mode((String) body.getOrDefault("mode", request.getMode()))
                            .status("success")
                            .degraded(Boolean.TRUE.equals(body.get("degraded")))
                            .degradedReason((String) body.get("degraded_reason"))
                            .requestId((String) body.get("request_id"))
                            .latencyMs(resolveImageLatencyMs(body))
                            .build();
                    
                    log.debug("Image search successful: mode={}, results={}", request.getMode(), items.size());
                    return Mono.just(response);
                })
                .doOnError(e -> log.error("Image search failed: {}", e.getMessage()));
    }

    private Double resolveImageLatencyMs(Map<String, Object> body) {
        Object rawLatency = body.get("latency_ms");
        if (!(rawLatency instanceof Number)) {
            rawLatency = body.get("query_time_ms");
        }
        return rawLatency instanceof Number ? ((Number) rawLatency).doubleValue() : null;
    }


    
    /**
     * Builds cache key: rec:v1:{intent}:{sha1(query)}:{k}
     */
    private String buildCacheKey(String query, String intent, int k) {
        String normalizedQuery = query.toLowerCase().trim();
        String querySha1 = computeSha1(normalizedQuery);
        String intentKey = (intent != null && !intent.isEmpty()) ? intent : "search";
        return String.format("%s%s:%s:%d", RECOMMENDATION_CACHE_PREFIX, intentKey, querySha1, k);
    }
    
    /**
     * Returns the first 8 hex characters of the SHA-1 of the normalised query.
     *
     * <p>Short enough to include inline in every log line without noise, long
     * enough to act as a stable correlation key: the same query always produces
     * the same hash, so a Kibana/CloudWatch query of {@code queryHash=a3f8b1c2}
     * returns all log lines — inference call, metadata assembly, fallback, cache
     * write — for that exact query across replicas and threads without exposing
     * the raw query text (PII/privacy concern for production log retention).
     */
    private String queryHash(String query) {
        if (query == null || query.isBlank()) return "null";
        return computeSha1(query.toLowerCase().trim()).substring(0, 8);
    }

    /**
     * Computes SHA-1 hash of input string.
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
            return String.valueOf(input.hashCode());
        }
    }
    
    /**
     * Returns true when a request carries a userId and is therefore subject to
     * user-specific personalisation by Ray inference.
     *
     * Personalized requests must not read from or write to the shared query-level
     * recommendation cache because that cache does not include userId in its key.
     * Storing or serving personalized results under a user-agnostic key causes
     * cross-user cache pollution: user B would receive user A's ranked results.
     */
    private boolean isPersonalized(String userId) {
        return userId != null && !userId.isBlank();
    }

    /**
     * Returns cache statistics.
     */
    public java.util.Map<String, Object> getCacheStats() {
        return productCacheService.getCacheStats();
    }

    private void recordDegradedReason(DegradationReason reason) {
        meterRegistry.counter(
                "recommendation_degraded_reason_total",
                "service", "gateway",
                "reason", reason.name()
        ).increment();
    }

    private DegradationReason mapInferenceDegradationReason(Throwable throwable) {
        if (throwable instanceof java.util.concurrent.TimeoutException) {
            return DegradationReason.INFERENCE_TIMEOUT;
        }
        if (throwable instanceof CallNotPermittedException) {
            return DegradationReason.DOWNSTREAM_CIRCUIT_OPEN;
        }
        if (throwable instanceof RejectedExecutionException) {
            return DegradationReason.DOWNSTREAM_CAPACITY_REJECTED;
        }
        return DegradationReason.INFERENCE_UNAVAILABLE;
    }

    private DegradationReason mapRedisDegradationReason(Throwable throwable) {
        String typeName = throwable.getClass().getSimpleName().toLowerCase();
        if (typeName.contains("timeout")) {
            return DegradationReason.REDIS_TIMEOUT;
        }
        return DegradationReason.REDIS_UNAVAILABLE;
    }

    private String currentTraceId() {
        SpanContext ctx = Span.current().getSpanContext();
        return ctx.isValid() ? ctx.getTraceId() : "trace-unavailable";
    }

    private record RequestContext(String requestId, String traceId, long enqueuedAtNanos) {
    }

    private record PopularityFallbackSource(String window, String key, java.util.Set<String> ids) {
    }
}
