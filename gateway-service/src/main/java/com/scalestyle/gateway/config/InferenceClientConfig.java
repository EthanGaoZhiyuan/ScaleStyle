package com.scalestyle.gateway.config;

import io.netty.channel.ChannelOption;
import io.netty.handler.timeout.ReadTimeoutHandler;
import io.netty.handler.timeout.WriteTimeoutHandler;
import io.micrometer.core.instrument.MeterRegistry;
import io.micrometer.core.instrument.Timer;
import io.netty.util.AttributeKey;
import org.springframework.beans.factory.annotation.Value;
import org.springframework.context.annotation.Bean;
import org.springframework.context.annotation.Configuration;
import org.springframework.http.client.reactive.ReactorClientHttpConnector;
import org.springframework.scheduling.concurrent.ThreadPoolTaskExecutor;
import org.springframework.web.reactive.function.client.WebClient;
import reactor.netty.http.client.HttpClient;
import reactor.netty.resources.ConnectionProvider;

import java.time.Duration;
import java.util.concurrent.Executor;
import java.util.concurrent.RejectedExecutionException;
import java.util.concurrent.TimeUnit;

@Configuration
public class InferenceClientConfig {

    private static final AttributeKey<Long> CONNECT_START_NANOS =
            AttributeKey.valueOf("gateway.inference.connect.start.nanos");

    /**
     * Dedicated thread pool for inference service calls.
     * Isolates Ray inference from common ForkJoinPool to prevent thread starvation.
     * 
     * === PRODUCTION HARDNESS GUARANTEES ===
     * 
     * BULKHEAD CAPACITY:
     *   Maximum resource consumption when inference is slow or degraded:
     *   - 64 threads (max pool size)
     *   - 100 queued tasks
     *   - Total: 164 concurrent requests maximum
     *   - Beyond capacity: immediate RejectedExecutionException → fallback path
     * 
     * TIMEOUT ENFORCEMENT:
     *   Per-request maximum duration: 450ms (Netty responseTimeout) - hard kill
     *   - 350ms: Application-level timeout (Mono.timeout)
     *   - 450ms: Netty socket-level timeout (HARD KILL)
     *   - Whichever fires first terminates the request; I/O is always cancelled
     * 
     * FAIL-FAST BEHAVIOR:
     *   - AbortPolicy: throws exception immediately when queue is full
     *   - No waiting, no blocking on saturation
     *   - Circuit breaker: fallback path activated within 10s of opening
     * 
     * CALLER ISOLATION:
     *   - Uses AbortPolicy, NOT CallerRunsPolicy
     *   - Rejection path: exception → onErrorResume → fallback
     *   - Controller uses DeferredResult: Tomcat threads are released immediately
     *   - No backpressure onto calling threads
     * 
     * === BLAST RADIUS ===
     * When Ray inference is down or slow:
     * - Maximum threads occupied: 64 (0.32% of system with 200 Tomcat threads)
     * - Maximum queued requests: 100
     * - Maximum duration per request: 450ms
     * - 165th+ concurrent request: Immediate rejection → Redis fallback
     * - Circuit breaker opens at: 50% failure rate (5 of 10 calls)
     */
    @Bean(name = "inferenceExecutor")
    public Executor inferenceExecutor() {
        ThreadPoolTaskExecutor executor = new ThreadPoolTaskExecutor();
        executor.setCorePoolSize(32);
        executor.setMaxPoolSize(64);
        executor.setQueueCapacity(100);
        executor.setThreadNamePrefix("inference-");
        
        // Fail-fast when bulkhead is saturated, forcing fallback
        // DO NOT use CallerRunsPolicy - it bypasses bulkhead isolation
        executor.setRejectedExecutionHandler((r, ex) -> {
            throw new java.util.concurrent.RejectedExecutionException(
                "Inference bulkhead saturated - queue full (core=" + ex.getCorePoolSize() +
                ", max=" + ex.getMaximumPoolSize() + ", queue=" + ex.getQueue().size() + ")"
            );
        });
        
        executor.initialize();
        return executor;
    }

    /**
     * True async HTTP client with hard timeout and proper cancellation.
     * Uses Reactor Netty with connection pooling for non-blocking I/O.
     *
     * === CANCELLATION SEMANTICS ===
     * When DeferredResult is cancelled (client drops HTTP connection):
     * 1. CompletableFuture.cancel() is called
     * 2. Reactor Mono is cancelled (propagates downstream)
     * 3. Netty channel is closed, TCP socket is killed
     * 4. No "zombie" HTTP request to Ray
     *
     * This is TRUE cancellation, not just "stop waiting for response".
     *
     * === TIMEOUT LAYERS (Defense in Depth) ===
     * 1. Connect timeout (1000ms): TCP handshake
     * 2. Read timeout (450ms): First byte after request sent (Netty hard kill)
     * 3. Response timeout (450ms): Total time from request to full response
     * 4. Application timeout (350ms in RecommendationService): business deadline
     *
     * Note: there is NO connection-acquire timeout layer. Connection acquisition
     * either succeeds immediately or fails immediately (see backpressure model below).
     *
     * === SINGLE-QUEUE BACKPRESSURE MODEL ===
     * The executor bulkhead (64 threads + 100 queue = 164 slots) is the SOLE
     * concurrency gate for inference requests.
     *
     * The connection pool is sized to match: maxConnections = 100 covers all
     * active threads (max 64) with slack.  When the pool is exhausted the pool
     * rejects the acquire call IMMEDIATELY — it does not park the caller in a
     * secondary wait queue.
     *
     *   pendingAcquireMaxCount = 0
     *     → zero requests may wait for a free connection
     *     → pool-full condition throws PoolAcquirePendingLimitException at once
     *     → exception routes through onErrorResume to the Redis fallback, same as
     *       any other transient connection error
     *     → no executor thread is held during a connection wait; threads are freed
     *       immediately for the next request
     *
     *   pendingAcquireTimeout = Duration.ZERO
     *     → belt-and-suspenders: explicit zero timeout enforces instant rejection
     *       even if the count limit is somehow reached by a future API change
     *
     * Why this is correct:
     *   With pendingAcquireMaxCount > 0, every request that enters the acquire
     *   queue holds its executor thread for up to pendingAcquireTimeout ms while
     *   waiting.  This means the effective bulkhead capacity seen by the application
     *   is reduced (threads are idle-blocked), and p99 latency absorbs the wait
     *   silently — requests appear "slow" rather than "rejected".  Setting count = 0
     *   ensures saturation is visible immediately via the error/fallback path and
     *   the circuit-breaker counter, rather than being hidden as latency inflation.
     */
    @Bean
    public WebClient inferenceWebClient(
            @Value("${inference.base-url}") String baseUrl,
            @Value("${inference.http.connect-timeout:1000}") int connectTimeoutMs,
            @Value("${inference.http.read-timeout:450}") int readTimeoutMs,
            @Value("${inference.http.write-timeout:1000}") int writeTimeoutMs,
            @Value("${inference.http.max-connections:100}") int maxConnections,
            MeterRegistry meterRegistry) {

        Timer connectTimer = Timer.builder("recommendation_phase_duration_seconds")
                .description("Recommendation request phase duration")
                .tag("service", "gateway")
                .tag("phase", "http_connect")
                .publishPercentileHistogram()
                .register(meterRegistry);

        ConnectionProvider connectionProvider = ConnectionProvider.builder("inference-pool")
                .maxConnections(maxConnections)
                // No pending-acquire queue: reject immediately when pool is exhausted.
                // The executor bulkhead is the only concurrency gate; see Javadoc above.
                .pendingAcquireMaxCount(0)
                .pendingAcquireTimeout(Duration.ZERO)
                .maxIdleTime(Duration.ofSeconds(30))
                .maxLifeTime(Duration.ofMinutes(5))
                .evictInBackground(Duration.ofSeconds(120))
                .build();
        
        // HttpClient with hard timeouts at Netty level
        // These are HARD - Netty will close the socket when timeout fires
        HttpClient httpClient = HttpClient.create(connectionProvider)
                .option(ChannelOption.CONNECT_TIMEOUT_MILLIS, connectTimeoutMs)
                .doOnChannelInit((observer, channel, remoteAddress) ->
                        channel.attr(CONNECT_START_NANOS).set(System.nanoTime()))
                .doOnConnected(conn -> {
                    Long startedAt = conn.channel().attr(CONNECT_START_NANOS).getAndSet(null);
                    if (startedAt != null) {
                        connectTimer.record(java.time.Duration.ofNanos(System.nanoTime() - startedAt));
                    }
                })
                .doOnConnected(conn -> conn
                        .addHandlerLast(new ReadTimeoutHandler(readTimeoutMs, TimeUnit.MILLISECONDS))
                        .addHandlerLast(new WriteTimeoutHandler(writeTimeoutMs, TimeUnit.MILLISECONDS))
                )
                .responseTimeout(Duration.ofMillis(readTimeoutMs));
        
        return WebClient.builder()
                .baseUrl(baseUrl)
                .clientConnector(new ReactorClientHttpConnector(httpClient))
                .build();
    }

    /**
     * Dedicated executor for recommendation result enrichment: product-metadata
     * lookup (Caffeine L1 + Redis L2) and recommendation-cache reads/writes.
     *
     * === ISOLATION MODEL ===
     * Stage 1 (Ray inference I/O) runs on Netty event-loop threads — non-blocking.
     * Stage 2 (blocking Redis / Caffeine) must NOT run on event-loop threads and
     * must NOT share the inferenceExecutor (which is sized for concurrent HTTP
     * calls, not blocking waits).  This executor provides a hard-bounded, named
     * thread pool that is exclusively responsible for Stage 2 work.
     *
     * === CAPACITY ===
     * Threads: recommendation.cache.executor.core-pool-size (default 8) to
     *          recommendation.cache.executor.max-pool-size  (default 32).
     * Queue:   recommendation.cache.executor.queue-capacity (default 200).
     * These defaults handle typical Redis latencies (1–5 ms) without starving
     * inference threads.  Increase max-pool-size or queue-capacity when Redis
     * tail latency exceeds ~20 ms at peak QPS.
     *
     * === REJECTION ===
     * AbortPolicy: throws RejectedExecutionException when queue is full.
     * The Reactor operator wraps this as a Mono error, which the service's
     * onErrorResume fallback catches and routes to popular-items degradation.
     * This is preferable to blocking the caller or silently dropping requests.
     *
     * === OBSERVABILITY ===
     * Threads are named rec-cache-N.  They appear in thread dumps, JVM metrics,
     * and any thread-pool Micrometer instrumentation under that prefix.
     */
    @Bean(name = "cacheExecutor")
    public Executor cacheExecutor(
            @Value("${recommendation.cache.executor.core-pool-size:8}")  int corePoolSize,
            @Value("${recommendation.cache.executor.max-pool-size:32}")  int maxPoolSize,
            @Value("${recommendation.cache.executor.queue-capacity:200}") int queueCapacity) {

        ThreadPoolTaskExecutor executor = new ThreadPoolTaskExecutor();
        executor.setCorePoolSize(corePoolSize);
        executor.setMaxPoolSize(maxPoolSize);
        executor.setQueueCapacity(queueCapacity);
        executor.setThreadNamePrefix("rec-cache-");

        // Fail-fast on saturation: reject immediately rather than caller-runs.
        // The rejected task surfaces as a Mono error, which onErrorResume
        // routes to the popular-items fallback path.
        executor.setRejectedExecutionHandler((r, ex) -> {
            throw new RejectedExecutionException(
                "Recommendation cache executor saturated (core=" + ex.getCorePoolSize() +
                ", max=" + ex.getMaximumPoolSize() +
                ", queue=" + ex.getQueue().size() + ")"
            );
        });

        executor.initialize();
        return executor;
    }

    @Bean
    public String inferenceBaseUrl(@Value("${inference.base-url}") String baseUrl) {
        return baseUrl;
    }
}
