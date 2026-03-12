# ScaleStyle: Deep Repository Walkthrough

---

## 1. Executive Project Summary

**What this system is:**
ScaleStyle is a real-time personalized fashion recommendation engine. Users submit text queries (or images); the system returns semantically ranked product recommendations, personalized by each user's recent clicks, category affinity, and rolling online popularity signals.

**What problem it solves:**
Generic keyword search for fashion is poor. ScaleStyle replaces it with: (1) dense vector search over product embeddings for semantic relevance, (2) cross-encoder reranking for re-ordering relevance, and (3) an online behavior loop that adjusts scores in real time based on what users are clicking.

**What kind of system it is:**
A production-style, latency-sensitive ML serving system with:
- An API gateway (Java/Spring Boot) handling HTTP fan-out
- A Python inference backend (Ray Serve) running ML models
- A real-time event pipeline (Kafka → Python consumer → Redis)
- A vector database (Milvus) for ANN retrieval
- A fully configured deployment (Docker Compose local, Kubernetes + Terraform for EKS)

**Main implemented capabilities:**
- Text search via BGE-large embeddings + Milvus ANN retrieval
- Cross-encoder reranking (ms-marco-MiniLM)
- Intent routing (BROWSE vs SEARCH) with A/B test bucketing
- Real-time personalization: exact click boost (1.5×), category affinity boost (1.2×), rolling popularity windows (1h/24h/7d)
- Click event ingestion with broker-acked Kafka publishing
- Redis online feature store with Lua-atomic decay updates
- Tiered retry / DLQ topology for the event consumer
- Two-tier product cache (L1 Caffeine + L2 Redis)
- Circuit breaker on the inference call (Resilience4j)
- Full observability: Prometheus, Grafana, Jaeger (OTel)
- K8s + Terraform AWS deployment stack

**What is NOT fully implemented or is optional:**
- Image search (CLIP vision) is a feature-flagged optional path; `vision_handle` is optional
- LLM generation (Qwen2-1.5B) is `GENERATION_ENABLED=false` by default
- Milvus is self-managed in K8s (not a managed service like Zilliz), which carries operational risk
- Redis Cluster mode is explicitly **incompatible** with the Lua script; standalone/replication only
- The data pipeline (`data-pipeline/`) is a one-time bootstrap job, not an ongoing pipeline
- gRPC is in the Gradle dependency list but the actual serving path is HTTP (the comment in `application.properties` says "Phase 2 default: call Ray Serve via HTTP")

---

## 2. Repository Map

```
ScaleStyle/
├── gateway-service/          CORE — Java/Spring Boot API gateway
├── inference-service/        CORE — Python Ray Serve ML inference backend
├── event-consumer/           CORE — Python Kafka→Redis real-time feature updater
├── data-pipeline/            INFRA/ONCE — One-time ETL bootstrap for Milvus + Redis
├── infrastructure/
│   ├── k8s/                  INFRA — Kubernetes manifests (base + minikube + EKS overlays)
│   └── terraform/            INFRA — AWS infra (VPC, EKS, ElastiCache, ECR, S3)
├── observability/            SUPPORT — Prometheus config, Grafana dashboards
├── tests/                    SUPPORT — Integration tests
├── docs/                     OPTIONAL — API docs
├── docker-compose.yml        INFRA — Full local stack (13 services)
└── Makefile                  INFRA — Deploy automation
```

### Per-module breakdown

| Module | Key Files | Classification |
|---|---|---|
| `gateway-service/` | `RecommendationController`, `RecommendationService`, `EventController`, `EventTrackingService`, `ProductCacheService`, `KafkaConfig`, `InferenceClientConfig` | **Core runtime** |
| `inference-service/src/deployments/` | `ingress.py`, `router.py`, `embedding.py`, `retrieval.py`, `reranker.py`, `popularity.py`, `generation.py`, `multimodal.py` | **Core runtime** |
| `inference-service/src/personalization/` | `feature_reader.py`, `behavior_boost.py`, `snapshot.py`, `snapshot_loader.py`, `popularity_windows.py` | **Core runtime** |
| `inference-service/src/utils/` | `redis_client.py`, `redis_metadata.py`, `metrics.py`, `observability.py`, `bucketing.py` | **Support** |
| `event-consumer/src/` | `consumer.py` (Lua script embedded), `config.py`, `metrics.py` | **Core runtime** |
| `data-pipeline/src/` | `bootstrap_data.py` | **Infra/once** |
| `infrastructure/k8s/` | `gateway.yaml`, `inference-hpa.yaml`, `event-consumer.yaml`, overlays | **Infra** |
| `infrastructure/terraform/` | `eks.tf`, `elasticache.tf`, `vpc.tf`, `iam.tf` | **Infra** |
| `observability/` | `prometheus.yml`, Grafana dashboards | **Support** |

---

## 3. Actual Runtime Architecture

```
                          ┌─────────────────────────────┐
  Client HTTP             │     gateway-service          │
  GET /api/recommendation/search                         │
──────────────────────────► RecommendationController     │
                          │   │ DeferredResult (600ms)   │
                          │   ▼                          │
                          │ RecommendationService         │
                          │   │ Resilience4j CB (ray)    │
                          │   │ WebClient (450ms timeout) │
                          │   ▼                          │
                          │  InferenceClient ────────────┼──► inference-service (Ray Serve)
                          │                              │         │
                          │  ProductCacheService ◄───────┼──       │  IngressDeployment
                          │   │L1:Caffeine│L2:Redis│     │         │   RouterDeployment
                          │   ▼                          │         │   EmbeddingDeployment
                          │  Response assembly           │         │   RetrievalDeployment (Milvus)
                          └─────────────────────────────┘         │   RerankerDeployment
                                                                   │   PersonalizationSnapshot
                                                                   │   BehaviorBoost
                                  ┌────────────────────────────────┘
  Click event flow:               │
  POST /api/events/click          │
──────────────────────────────────▼
                          EventController
                          EventTrackingService
                          EventProducerService
                               │ Kafka (acks=all, idempotent)
                               ▼
                          scalestyle.clicks topic
                               │
                               ▼
                     event-consumer-primary
                     EventConsumer.LUA_UPSERT_FEATURES
                               │ Redis (standalone mode)
                               ├──► user:{id}:recent_clicks (LPUSH)
                               ├──► user:{id}:category_affinity (HSET + decay)
                               ├──► item:{id}:clicks (decay)
                               ├──► popularity:bucket:1h/24h/7d:{ts} (ZINCRBY)
                               └──► global:popular (ZADD with log surrogate)

                     On failure:
                     retry-1s → retry-10s → retry-60s → DLQ
```

**Key architectural facts:**
- The gateway is the **only public-facing entry point**; inference and event-consumer are internal
- Ray Serve handles in-pod actor scaling; EKS HPA handles pod scaling
- The Redis online feature store is **written** by the event consumer and **read** by the inference service — the gateway only reads Redis for product metadata
- Milvus is reached only by the inference service (via `RetrievalDeployment`)
- The inference service is CPU-only by default; GPU override via env vars

---

## 4. Main Request Flow (Step-by-Step)

### A. Request entry
**File:** `gateway-service/src/main/java/com/scalestyle/gateway/controller/RecommendationController.java:77`

```
GET /api/recommendation/search?query=...&userId=...&k=10
```

`RecommendationController.search()` wraps the work in a `DeferredResult<>(600ms)` — the Tomcat thread is immediately released. A `CompletableFuture` is dispatched. Three hooks enforce the "no zombie request" contract: `onTimeout` (503 + cancel), `onCompletion` (cancel if still running), `whenComplete` (propagate result or exception).

**Timeout layers from outermost to innermost:**
```
600ms  DeferredResult (controller: last-resort catch-all)
450ms  Netty read timeout (InferenceClientConfig: hard socket kill)
350ms  Mono.timeout in RecommendationService (application-level)
```

### B. Gateway orchestration
**File:** `gateway-service/src/main/java/com/scalestyle/gateway/service/RecommendationService.java`

`RecommendationService.searchAsync()` uses WebFlux `Mono` to:
1. Build `InferenceSearchRequest` from query/userId/k/intent
2. Call `webClient.post()` to `{INFERENCE_BASE_URL}/` with 350ms timeout
3. Apply Resilience4j circuit breaker (`CircuitBreakerOperator.of(circuitBreaker)`)
4. On success: call `ProductCacheService.enrichRecommendations()` to look up metadata from L1/L2 cache
5. On failure/CB open: call `getPopularItemsFallback()` which reads from Redis materialized popularity windows

### C. Inference service entry
**File:** `inference-service/src/deployments/ingress.py:333`

`IngressDeployment.__call__()` extracts the W3C `traceparent` header to continue the distributed trace. Routes POST `/` to `_search_impl()`.

### D. Intent routing
**File:** `inference-service/src/deployments/ingress.py:554`

```python
route = await self.router_handle.route.remote(req.query, req.user_id)
```

Returns `{"intent": "BROWSE"|"SEARCH", "filters": {...}, "flow": "smart"|"base"}`.
- `BROWSE` → skip embedding/retrieval entirely, return `popularity_handle.topk.remote(k)`
- A/B bucketing: `bucket_user(user_id, 2) == 0` → `smart` flow (with reranking), else `base`

### E. Embedding
**File:** `inference-service/src/deployments/ingress.py:660`

```python
vector = await asyncio.wait_for(
    self.embedding_handle.embed.remote(req.query, is_query=True),
    timeout=embed_timeout_ms / 1000.0  # 500ms
)
```

BGE-large prepends `"Represent this sentence for searching relevant passages:"` to query text. On `asyncio.TimeoutError`: falls back to `_safe_popularity_topk(k)`, records `DegradationReason.INFERENCE_TIMEOUT`.

### F. Vector retrieval
**File:** `inference-service/src/deployments/ingress.py` (after embedding phase)

```python
candidates = await asyncio.wait_for(
    self.retrieval_handle.search.remote(vector, recall_k=100),
    timeout=300/1000.0  # 300ms
)
```

`RetrievalDeployment` calls Milvus ANN search. Returns up to 100 candidates. On timeout: popularity fallback.

### G. Metadata enrichment
**File:** `inference-service/src/services/search_result_service.py` (via `_enrich_and_filter`)

Candidates are enriched with product metadata from Redis (`item:{id}:meta` hashes). Items missing metadata are filtered out. Capped at `RerankerConfig.MAX_DOCS` (50) before reranking.

### H. Personalization snapshot load
**File:** `inference-service/src/personalization/feature_reader.py:305`

`FeatureReader.load_personalization_snapshot()` runs a **bounded 3-stage read plan** (max ~3 Redis round trips):
1. Pipeline: `LRANGE user:{id}:recent_clicks`, `HGETALL category_affinity`, `HGETALL affinity:last_ts`
2. Resolve popularity window keys (with distributed lock on ZUNIONSTORE if stale)
3. Pipeline: `HGET item:{id}:meta category` for all candidates + recent-click items, `ZMSCORE` on 1h/24h/7d materialized windows

Returns a `PersonalizationSnapshot` dataclass — immutable for the request.

### I. Reranking
**File:** `inference-service/src/deployments/reranker.py`

```python
reranked = await asyncio.wait_for(
    self.reranker_handle.rerank.remote(query, candidates[:50]),
    timeout=250/1000.0  # 250ms
)
```

Cross-encoder `ms-marco-MiniLM-L-6-v2` scores (query, doc) pairs in batch. On timeout: candidates returned in original retrieval order (no reranking).

### J. Behavior boost
**File:** `inference-service/src/personalization/behavior_boost.py:55`

`BehaviorBoost.apply_boost(snapshot, results)`:
- Exact click: `score *= 1.5` if `canonical_id in recent_clicks`
- Category affinity: `score *= 1.2` if item category in clicked categories
- Popularity: additive log-normalized boost from 1h/24h/7d signals (budget: 0.20/0.10/0.05)
- Cap: `boost_factor = min(boost_factor, 3.0)`
- Re-sorts list by boosted score

### K. Response return
Back at `IngressDeployment._build_response()`: `_contract_normalize()` enforces field names and limits to `k` results. Returns JSON. The gateway `RecommendationService` receives the response, enriches with product metadata from `ProductCacheService`, returns `CommonApiResponse<List<RecommendationDTO>>`.

**Full call graph:**
```
RecommendationController.search()
  → RecommendationService.searchAsync()
    → WebClient POST /  [Resilience4j CB + 350ms Mono.timeout + 450ms Netty]
      → IngressDeployment.__call__()
        → _search_impl(req)
          → RouterDeployment.route()        [intent + A/B]
          → EmbeddingDeployment.embed()     [500ms timeout]
          → RetrievalDeployment.search()    [300ms timeout → Milvus]
          → _enrich_and_filter()            [Redis metadata]
          → FeatureReader.load_personalization_snapshot()  [3 Redis pipelines]
          → RerankerDeployment.rerank()     [250ms timeout]
          → BehaviorBoost.apply_boost()     [CPU-only, no I/O]
          → _contract_normalize()
    → ProductCacheService.enrichRecommendations()  [L1 Caffeine + L2 Redis]
```

---

## 5. Event / Online Feature Flow (Step-by-Step)

### A. Event produced at gateway
**File:** `gateway-service/src/main/java/com/scalestyle/gateway/controller/EventController.java`

`POST /api/events/click` → `EventTrackingService.trackClick()`

### B. Validation + normalization
**File:** `gateway-service/src/main/java/com/scalestyle/gateway/service/EventTrackingService.java:50`

`toCommand()` → `validateRequiredFields()` → `normalizeCommand()`. Item IDs are zero-padded to 10 digits if purely numeric (`%010d`). Source is lowercased. Device defaults to `"api"`.

### C. Kafka publish (broker-acknowledged)
**File:** `gateway-service/src/main/java/com/scalestyle/gateway/service/EventTrackingService.java:70`

```java
SendResult result = eventProducerService.publishClick(event)
    .get(brokerAckTimeoutMs, TimeUnit.MILLISECONDS);  // 6000ms default
```

This is **synchronous wait for broker ACK** — not fire-and-forget. The HTTP response to the client is `"status": "acknowledged_by_broker"`. Configured with `acks=all`, idempotent producer, lz4 compression, 2ms linger, 8KB batches, 3 retries.

### D. Kafka topic
Topic `scalestyle.clicks` (3 partitions local, 12+ EKS). Messages are `ClickEventV1` JSON.

### E. Consumer: primary path
**File:** `event-consumer/src/consumer.py:179`

`EventConsumer` starts `KafkaConsumer` on `scalestyle.clicks`. Per message:
1. Parse and validate JSON
2. Look up item category (from `_CategoryCache` LRU first, then Redis `HGET item:{id}:meta category`)
3. Execute `LUA_UPSERT_FEATURES` script against Redis

### F. Lua atomic update
**File:** `event-consumer/src/consumer.py:198`

The Lua script (embedded as `LUA_UPSERT_FEATURES`) runs **atomically on Redis**:
1. Check dedup key `SETEX dedupe:{event_id}` (idempotency); return `DUPLICATE` if already seen
2. `LPUSH user:{id}:recent_clicks`, `LTRIM` to max 100
3. `HSET user:{id}:category_affinity` with exponential decay applied: `new_score = decayed_old + 1.0`
4. `SET item:{id}:clicks` (decay)
5. `SET item:{id}:recent_clicks` (decay)
6. `ZADD global:popular` with log-surrogate score: `log(actual_score) + λ·ts`
7. `ZINCRBY popularity:bucket:1h/24h/7d:{bucket_ts}` for windowed popularity
8. `LPUSH session:{id}:clicks` if session_id present

Returns `"OK"` or `"DUPLICATE"`.

### G. Retry / DLQ flow
**File:** `event-consumer/src/consumer.py:95`

On `TRANSIENT_FAILURE` result: the primary consumer routes the message to `retry-1s` topic. On `PERMANENT_FAILURE` (schema error, malformed data): routes to DLQ.

Retry consumer processes:
- `retry-1s` → on failure: `retry-10s`
- `retry-10s` → on failure: `retry-60s`
- `retry-60s` → on failure: DLQ

Max 3 retries across tiers. `RETRY_ENFORCE_DELAY=true` by default: consumer **pauses partitions** instead of sleeping, then unpauses at `routed_at_ts + tier_delay`.

**Commit semantics after retry publish:**
`RetryPublishedCommitFailedError` and `DlqPublishedCommitFailedError` are explicitly **not swallowed** — they force process exit and Kubernetes restart. This is a deliberate fail-fast design: "the retry message already exists on the retry topic, so the consumer must not continue with an uncommitted source offset."

**Event path map:**
```
EventController
  → EventTrackingService.trackClick()
    → validate + normalize
    → EventProducerService.publishClick().get(6000ms)  ← broker ACK sync
      → Kafka topic: scalestyle.clicks
        → EventConsumer (primary)
          → _CategoryCache.get() / Redis HGET (LRU cache)
          → LUA_UPSERT_FEATURES (atomic)
            → DUPLICATE → commit, skip
            → OK → commit
          → TRANSIENT_FAILURE → publish retry-1s → commit source
          → PERMANENT_FAILURE → publish DLQ → commit source
        → EventConsumer (retry)
          → retry-1s → (same Lua) → retry-10s → retry-60s → DLQ
```

---

## 6. Fallback / Degradation / Failure Semantics

### Inference slow or unavailable
**Where:** `RecommendationService.java` — Resilience4j CB + Mono.timeout

- 350ms `Mono.timeout` → `TimeoutException` → CB records failure → popularity fallback
- 450ms Netty read timeout → `WebClientRequestException` → CB records failure → popularity fallback
- CB opens at 50% failure rate (sliding window 50, min 5) → `CallNotPermittedException` → instant popularity fallback (no network call attempted)
- CB waits 10s then half-opens (5 test calls)

Fallback reads from Redis materialized popularity windows (`popularity:materialized:24h`, then `7d`). Source: `application.properties:68-74`.

### Redis online features unavailable
**Where:** `feature_reader.py:369`

`redis.TimeoutError` and `redis.ConnectionError` in any of the 3 stages → appends `DegradationReason.REDIS_TIMEOUT` or `REDIS_UNAVAILABLE` to `degraded_reasons`. Snapshot is still returned but with `degraded=True`. `BehaviorBoost.apply_boost()` still runs; with empty `recent_clicks` and zero `category_affinity`, boost factor stays 1.0. Results are returned without personalization — silently degraded.

### Kafka publish fails (producer side)
**Where:** `EventTrackingService.java:76`

`TimeoutException` (broker ACK timeout) → `EventTrackingUnavailableException` → 503 to client.
`ExecutionException` (publish failed) → same 503.
Producer retries 3× with 250–1000ms backoff before propagating failure.

This is **explicit, not silent**. The API contract guarantees the client knows if the event was not durably published.

### Consumer processing fails
**Where:** `consumer.py:95`

`TRANSIENT_FAILURE` → retry topology (up to 3 retries). `PERMANENT_FAILURE` → DLQ. Consumer exits on commit-after-retry-publish failure. Kubernetes restarts; at-least-once delivery with idempotency via dedup key.

### Optional components unavailable
- `vision_handle=None` → image search path returns empty or falls back; guard in `__init__`
- `GENERATION_ENABLED=false` → generation step is entirely skipped
- `RetrievalDeployment` not ready → `/readyz` reports `retrieval: false` but service remains available (popularity fallback covers it)
- Personalization init fails → `NullFeatureReader` replaces `FeatureReader`; background probe thread retries every 30s (`PERSONALIZATION_INIT_RETRY_INTERVAL_SEC`)

**Where:** `ingress.py:264` — `_build_feature_reader_locked()` catches all init exceptions, sets `personalization_fallback_active` gauge to 1, starts probe thread.

---

## 7. Performance / Hot Path Design

### What the hot path is
```
Client → Gateway → Ray Serve IngressDeployment
  → RouterDeployment (classifies intent)
  → EmbeddingDeployment (BGE-large, CPU)
  → RetrievalDeployment (Milvus ANN)
  → Redis pipeline (metadata enrich + personalization snapshot)
  → RerankerDeployment (cross-encoder, CPU)
  → BehaviorBoost (CPU only, no I/O)
  → Gateway ProductCacheService (L1 Caffeine → L2 Redis)
```

### Timeout layering
From the code:
```
600ms  DeferredResult (RecommendationController, outer catch-all)
450ms  Netty responseTimeout (InferenceClientConfig)
350ms  Mono.timeout (RecommendationService, application-level)
500ms  asyncio.wait_for embedding (EmbeddingConfig.TIMEOUT_MS)
300ms  asyncio.wait_for retrieval (RetrievalConfig.TIMEOUT_MS)
250ms  asyncio.wait_for reranker (RerankerConfig.TIMEOUT_MS)
```

Each inner layer fires **before** the outer layer in the normal path. The DeferredResult 600ms is the emergency backstop. Note: embedding/retrieval/reranker timeouts are inside the inference service, upstream of the 350ms gateway timeout — they add up. A request doing embed(500ms max) + retrieval(300ms max) + rerank(250ms max) theoretically exceeds 350ms; in practice, these are warm paths and much faster. The timeout values represent tail budgets.

### Synchronous vs asynchronous
- **Async inside Ray Serve:** `_search_impl()` is `async def`, all deployment calls use `await`
- **Async at gateway:** WebFlux `Mono`; `DeferredResult` releases Tomcat thread
- **Synchronous block:** `EventTrackingService` does `.get(6000ms)` — broker ACK is intentionally synchronous
- **Personalization snapshot:** `load_personalization_snapshot()` is synchronous; the 3 Redis pipelines are pipelined (not sequential individual commands)

### Read amplification reduction
- **Personalization snapshot: bounded 3 pipelines.** The comment in `feature_reader.py:315` is explicit: "Load once, consume snapshot only. No per-item or per-feature Redis reads in the reranker or serving code."
- **Stage 3 batches all item reads.** One `pipeline.execute()` fetches categories for all candidates + recent clicks, and `ZMSCORE` for all 3 windows in a single round trip.
- **Materialized popularity windows.** `ZUNIONSTORE` pre-aggregates bucket ZSETs into a single materialized key. Serving only does `ZMSCORE` against that key — not a union scan per request.
- **Process-local window cache.** `_materialized_window_cache` in `FeatureReader` stores the key + expiry in memory. If still valid, zero Redis round trips for window resolution.
- **`_CategoryCache` LRU.** Event consumer caches item → category for 1 hour in-process; eliminates one Redis HGET per event on warm paths.

### Protecting p99 latency
- **Circuit breaker prevents cascade:** if Ray is slow/failing, the CB opens and the gateway returns popularity immediately without waiting
- **No Redis fallback blocks:** all Redis exceptions in `load_personalization_snapshot()` are caught; snapshot is returned degraded, not as an error
- **Connection pool bounded:** `inference.http.max-connections=100` with `pendingAcquireMaxCount=0`: no queuing — reject immediately on saturation
- **Reranker capped at 50 docs:** `RERANKER_MAX_DOCS=50` — prevents cross-encoder from scaling with result set size
- **Recall k=100 then trim to 50:** ANN retrieval fetches 100, enrichment/filter trims to 50, reranker sees max 50

### Batching / snapshot / cache bounds
- Popularity windows: `1h` → 12 buckets of 5min; `24h` → 24 buckets of 1h; `7d` → 7 buckets of 1d
- Materialized window TTL: 1h→60s, 24h→300s, 7d→900s (re-built only when expired)
- L1 Caffeine: 1000 items, 5min TTL
- Recent clicks: max 100 stored in Redis; max 20 used for boost (`MAX_RECENT_CLICKS_USED`)
- Global popularity ZSET: capped at 50K items (`ZREMRANGEBYRANK global:popular 0 -(50001)`)

---

## 8. Core Engineering Decisions and Trade-offs

### Java gateway + Python inference
Evidence: `gateway-service/build.gradle` (Spring Boot 3.4, Java 21), `inference-service/requirements.txt` (Ray, PyTorch, transformers).

**Trade-off:** Java handles high-concurrency HTTP serving efficiently (Tomcat, WebFlux); Python is required for ML model loading (BGE, cross-encoder). The cost is a cross-process HTTP hop on every request. The 450ms Netty timeout and connection pool cap are the compensating controls.

### HTTP serving path (not gRPC)
Evidence: `application.properties:9` — "Phase 2 default: call Ray Serve via HTTP". gRPC is in `build.gradle` dependencies but not used on the hot path.

**Rationale:** Ray Serve's HTTP ingress is simpler to operate; FastAPIInstrumentor auto-instruments trace propagation. gRPC would require Proto definitions and custom serialization for model inputs.

### Broker-acked Kafka (not fire-and-forget)
Evidence: `EventTrackingService.java:70` — `.get(brokerAckTimeoutMs)`.

**Trade-off:** The 200 response to the client guarantees the event reached Kafka. This adds latency to the event endpoint (up to 6s timeout, though typical is ms). The alternative — fire-and-forget — would silently drop events on broker failures. For a personalization loop, lost events degrade model quality.

### Redis online state design (standalone, Lua atomicity)
Evidence: `consumer.py:183-195` — explicit comment: "INCOMPATIBLE with Redis Cluster mode". Lua script is a hard constraint.

**Trade-off:** The Lua script atomically writes across `user:*`, `item:*`, `global:*`, `popularity:*` keys — cross-slot operations that require standalone Redis. This simplifies event processing (one script call = complete atomic state update with dedup) but prevents horizontal Redis scaling via cluster mode. For a read-heavy serving workload, a single Redis replication group is sufficient.

### Snapshot-style personalization reads
Evidence: `feature_reader.py:8` — "The ONLY supported hot-path entry point is `load_personalization_snapshot()`. Load once, consume snapshot only."

**Trade-off:** Prevents N+1 Redis reads in reranking. Bounded latency regardless of candidate set size (not proportional to k). The cost is that the snapshot may be slightly stale if events arrive during the request — acceptable for ranking.

### Distributed lock on ZUNIONSTORE rebuild
Evidence: `feature_reader.py:143` — `redis.set(lock_key, "1", nx=True, px=5000)`.

**Rationale:** Multiple inference replicas may all detect an expired materialized window simultaneously. Without the lock, they'd all issue `ZUNIONSTORE` — thundering herd. The lock ensures only one replica rebuilds; others wait on a `threading.Event` (50ms timeout) then recheck.

### Explicit degradation vs silent fallback
Evidence: `DegradationReason` enum across `ingress.py`, `feature_reader.py`, `consumer.py`. Counter `recommendation_request_degraded_total` labeled by reason.

**Design philosophy:** Failures are tracked, not hidden. Every degradation path records a metric with its reason. This makes it possible to distinguish "inference timeout" from "Redis unavailable" from "retrieval failure" in Grafana.

### Fail-fast consumer on commit uncertainty
Evidence: `RetryPublishedCommitFailedError`, `DlqPublishedCommitFailedError` — both re-raised, not swallowed. Comment: "The consumer must not continue with an uncommitted source offset."

**Rationale:** At-least-once delivery with idempotency is safe. At-most-once (swallowing the error and continuing) would silently drop events. Process exit + K8s restart + dedup key in Redis ensures correctness.

---

## 9. Subsystem Deep Dives

### 9.1 Gateway Service
**Responsibilities:** HTTP ingress, request validation, inference fan-out, product metadata enrichment, click event ingestion, circuit breaking, caching, metrics/tracing.

**Critical files:**
- `RecommendationController.java` — DeferredResult pattern, 3-layer timeout contract
- `RecommendationService.java` — WebClient + CB + enrichment + fallback
- `EventTrackingService.java` — broker-ack semantics, validation, normalization
- `ProductCacheService.java` — L1/L2 cache logic
- `application.properties` — all timeout/pool/CB/Kafka config

**What to remember:** The DeferredResult with its 3-hook contract is the key non-obvious piece. The circuit breaker doesn't time individual requests — it tracks failure rate over a sliding window of 50 calls. The connection pool rejects on saturation (no queue) — this is intentional to prevent head-of-line blocking.

### 9.2 Inference Service
**Responsibilities:** Orchestrate the full ML pipeline inside Ray Serve. Manage deployment handles. Load personalization. Apply ranking boost.

**Critical files:**
- `ingress.py` — main request handler, all fallback logic, personalization recovery probe
- `router.py` — intent classification, A/B bucketing
- `config.py` — all timeout/model/personalization config

**What to remember:** `IngressDeployment` is a Ray actor (0.25 CPU, 1-3 replicas). The `feature_reader` property has a self-healing pattern: on init failure, `NullFeatureReader` is returned and a daemon thread probes every 30s for recovery. This means personalization can re-enable itself after a Redis outage without a pod restart.

### 9.3 Personalization / Behavior Boost
**Responsibilities:** Load user state from Redis in one bounded snapshot; apply multiplicative + additive boosts to reranked candidates.

**Critical files:**
- `feature_reader.py` — the 3-stage read plan, distributed lock, window cache
- `behavior_boost.py` — boost formulas, cap logic, re-sort
- `snapshot.py` — immutable snapshot dataclass

**Boost formula (from code):**
```
boost_factor = 1.0
if item in recent_clicks: boost_factor *= 1.5
elif item.category in clicked_categories: boost_factor *= 1.2
popularity_factor = 1.0
+ 0.20 * log1p(signal_1h) / log1p(max_1h)
+ 0.10 * log1p(signal_24h) / log1p(max_24h)
+ 0.05 * log1p(signal_7d) / log1p(max_7d)
boost_factor *= popularity_factor
boost_factor = min(boost_factor, 3.0)
final_score = original_score * boost_factor
```

Log-normalization prevents a single viral item from dominating by its raw click count.

### 9.4 Event Consumer
**Responsibilities:** Consume click events from Kafka, atomically update Redis online features, handle retries and DLQ.

**Critical files:**
- `consumer.py` — entire logic in one file: Lua script, retry topology, category LRU cache, partition-pause retry delay
- `config.py` — Kafka topics, Redis config, decay lambdas, retry tiers

**What to remember:** The Lua script is the heart. It handles 8 separate Redis state mutations atomically. The dedup key (`dedupe:{event_id}`) expires after configurable TTL — duplicate events within TTL window are idempotent; after TTL expiry, re-delivery would be applied again. This is a known trade-off for at-least-once delivery.

### 9.5 Redis Update Semantics
**Implemented decay model:**

For category affinity (7-day half-life):
```
λ = ln(2) / (7 * 86400)
new_score = old_score * exp(-λ * elapsed_seconds) + 1.0
```

For global popularity (ZSET surrogate score):
```
zset_score = log(actual_score) + λ * last_update_ts
```

This preserves correct ZSET ordering without needing to rescore the entire set on each update — a ranking-surrogate trick that avoids a full ZSET rescan per click.

Popularity buckets are simple `ZINCRBY` — no decay inside buckets. Decay is achieved by bucket expiry (old buckets naturally disappear when TTL expires).

### 9.6 Milvus / Retrieval
**Files:** `inference-service/src/deployments/retrieval.py`, `inference-service/src/utils/milvus_client.py`

`RetrievalDeployment` connects to Milvus on startup. `search()` runs ANN search over BGE-large (1024-dim) vectors. Collection: `scale_style_bge_v2`. `recall_k=100`.

The `data-pipeline/src/bootstrap_data.py` populates this collection from parquet files during cluster initialization (K8s `init-job.yaml`).

**What to remember:** Milvus is self-managed. If Milvus is unavailable at startup, `retrieval_ready=false` but the service stays up (popularity fallback). This is reflected in `/readyz` which treats retrieval as non-blocking.

### 9.7 Observability Wiring
**Files:** `observability/prometheus.yml`, `observability/grafana/`, `inference-service/src/utils/observability.py`

- **Metrics:** Prometheus scrapes `/actuator/prometheus` (gateway), `/metrics` (inference, event-consumer) every 15s
- **Tracing:** OTel → OTLP gRPC → Jaeger. Gateway injects `traceparent` header; inference service extracts it with `TraceContextTextMapPropagator`. Guard at `ingress.py:76` prevents duplicate instrumentation on replica start/reload.
- **Dashboards:** `scalestyle-overview.json` (latency, throughput, cache hits), `scalestyle-resilience.json` (CB state, timeout rates)
- **Alerts:** `event-consumer-alerts.yaml` — Kafka consumer lag, processing rate, Redis errors, consumer health

**Per-phase metrics:** `recommendation_request_phase_duration_seconds` with labels `phase` (route/embed/retrieval/rerank/fallback) and `outcome` (success/timeout/error). This is how you diagnose which stage is slow.

### 9.8 Kubernetes / Terraform
**K8s base layer:** `infrastructure/k8s/base/`
- `gateway.yaml`: 2 replicas, `200m` CPU req / `512Mi` mem req, `1` CPU / `1Gi` mem limit
- `inference-hpa.yaml`: 2–8 replicas, 50% CPU target
- `event-consumer.yaml`: primary (2 replicas) + retry (1 replica)

**EKS overlay:** `infrastructure/k8s/overlays/eks/`
- ALB ingress (AWS Load Balancer Controller)
- Strimzi-managed Kafka
- IRSA service account for ElastiCache access
- `deploy-production.sh` orchestrates the full deployment sequence

**Terraform:** VPC, EKS managed cluster, ElastiCache replication group (NOT cluster mode, required by Lua), ECR, S3, IAM/IRSA.

**What to remember:** The ElastiCache must be configured with `cluster_mode_enabled = false`. This is a hard constraint from the Lua script's cross-slot writes.

---

## 10. Configuration Surface

### Application config (gateway)
`gateway-service/src/main/resources/application.properties`
- **Timeout hierarchy:** `server.tomcat.threads.max=200`, `inference.http.read-timeout=450`, `inference.http.max-connections=100`
- **Kafka producer:** `acks=all`, idempotent, lz4, 2ms linger, 8KB batch, 3 retries, 5s request timeout
- **Circuit breaker:** 50% failure rate, 50-call window, 10s wait, 5 half-open probes
- **Redis pool:** 8 max active/idle, 2000ms timeout
- **Cache:** L1 1000 items/5min, L2 Redis

### Inference config
`inference-service/src/config.py` — all env-var overrideable:
- `EMBEDDING_TIMEOUT_MS=500`, `RETRIEVAL_TIMEOUT_MS=300`, `RERANKER_TIMEOUT_MS=250`
- `RECALL_K=100`, `RERANKER_MAX_DOCS=50`, `RERANKER_BATCH_SIZE=16`
- `EXACT_CLICK_BOOST=1.5`, `CATEGORY_AFFINITY_BOOST=1.2`, boost cap `3.0`
- `CATEGORY_AFFINITY_HALF_LIFE_DAYS=7` → λ computed from this
- Popularity bucket sizes: `1h=300s`, `24h=3600s`, `7d=86400s`
- Materialized window TTLs: `1h→60s`, `24h→300s`, `7d→900s`
- `GENERATION_ENABLED=false` (optional LLM off by default)
- `BASE_FLOW_MODE=vector` (A/B base group uses vector search, not pure popularity)

### Event consumer config
`event-consumer/src/config.py`
- Retry tiers: `retry-1s` (1s delay), `retry-10s` (10s), `retry-60s` (60s)
- Decay lambdas: affinity 7d half-life, item click 72h, recent item click 24h, global popularity 48h
- `CATEGORY_CACHE_MAX_SIZE`, `CATEGORY_CACHE_TTL_SECONDS`
- `RETRY_ENFORCE_DELAY=true` (partition pause vs sleep)
- `METRICS_PORT`

### Deployment configs
- `infrastructure/terraform/variables.tf` — AWS region, cluster name, instance types
- `infrastructure/k8s/overlays/eks/` — resource limits, replicas, image tags
- `docker-compose.yml` — local port assignments, service dependencies, health check intervals

---

## 11. Tests as System Contracts

### Degradation behavior
- `test_degradation_reasons.py` — verifies correct `DegradationReason` labels are recorded for each failure type (timeout, unavailable, fallback)
- `ImageSearchFallbackTest` (gateway) — verifies image search falls back gracefully

### Timeout behavior
- `test_ingress_probe_recovery.py` — verifies the inference service's health probe recovery after personalization init failure
- Gateway `RecommendationControllerTest` — exercises the DeferredResult timeout path

### Redis atomicity
- `test_redis_metadata_contract.py` — verifies metadata key format contracts (`item:{id}:meta`)
- `test_redis_startup_validation.py` — verifies startup checks for Redis (cluster mode detection, etc.)
- Consumer tests verify Lua script dedup behavior — `DUPLICATE` vs `OK` return values

### Consumer retry / DLQ semantics
- Tests validate:
  - Retry routing on transient failure
  - DLQ routing on permanent failure
  - Partition pause for retry delay enforcement
  - `RetryPublishedCommitFailedError` causes process exit (not swallowed)

### Personalization correctness
- `test_personalization_hot_path.py` — verifies snapshot load, 3-stage read plan, round trip count
- `test_behavior_boost_cap.py` — verifies boost never exceeds 3.0× regardless of signal combination
- `test_feature_reader_decay.py` — verifies exponential decay calculation in snapshot loading
- `test_feature_reader_failure_paths.py` — verifies graceful degradation on each Redis error type

### Popularity window correctness
- `test_popularity_windowed.py` — verifies bucket key calculation, ZUNIONSTORE aggregation, materialized TTL logic, distributed lock behavior (winner vs locked-out)

### Infra / runtime contract checks
- `test_redis_startup_validation.py` — fails fast on Redis Cluster mode detection
- `test_contract.py` — verifies response schema compliance (`article_id`, `score`, required fields)
- `EventTrackingServiceBestEffortContractTest` — verifies broker-ack semantics: 200 only on ACK, 503 on timeout/failure

**What the tests imply:** This codebase treats degradation paths, timeout semantics, and atomic correctness as first-class concerns. The test suite is not just happy-path — it specifically validates the failure and fallback behaviors.

---

## 12. What Is Actually "Production-Oriented" Here

### Correctness
- Lua atomic multi-key Redis update prevents partial state writes on events
- Idempotency via dedup key — safe for at-least-once Kafka delivery
- Item ID normalization (zero-padding) in both gateway and consumer — prevents key mismatches between click events and product metadata
- `canonical_article_id()` used consistently in inference and consumer — same normalization function

### Resilience
- Three-tier retry + DLQ topology for event consumer
- Fail-fast process exit on commit uncertainty (no silent data loss)
- Circuit breaker with half-open probing (not just binary open/closed)
- Background personalization recovery probe (self-healing without restart)
- `NullFeatureReader` as graceful degradation when Redis is unavailable at startup

### Latency-awareness
- DeferredResult releases Tomcat threads immediately
- Connection pool rejects on saturation rather than queuing
- Bounded 3-pipeline Redis read for personalization (no N+1)
- Process-local materialized window cache (zero Redis RTT on warm path)
- Category LRU cache in consumer (eliminates Redis lookup per event)
- Max 50 docs to reranker (prevents quadratic cross-encoder cost)

### Operational semantics
- `recommendation_request_phase_duration_seconds` with phase labels — you can see exactly where latency lives
- `recommendation_request_degraded_total` labeled by reason — distinguish timeout from unavailability
- Runbook in `event-consumer/RUNBOOK.md` — consumer lag monitoring, HPA bounds, failfast semantics documented
- `/readyz` distinguishes hard dependencies (Redis, embedding, popularity) from soft (retrieval)

### Observability
- Distributed traces span gateway → inference via W3C `traceparent` propagation
- Idempotency guard on FastAPIInstrumentor (prevents duplicate spans on replica start)
- Grafana dashboards provisioned automatically (not manual setup)
- Per-window popularity materialization metrics: `snapshot_materialization_total` labeled by outcome (local_cache_hit / redis_ttl_hit / rebuilt / lock_skipped / peer_rebuilt)

### Service boundaries
- Gateway is the only egress point for clients; inference and consumer are internal
- Redis is the integration point between event consumer and inference service — no direct coupling
- Data pipeline is a one-time Job, not a continuously running service

---

## 13. What Is Partial / Optional / Less Mature

| Area | Status | Notes |
|---|---|---|
| **Image search (CLIP/Vision)** | Optional path | `vision_handle` is `Optional[DeploymentHandle]`. Feature flag in `server.py`. Not on the main hot path. |
| **LLM Generation (Qwen2-1.5B)** | Off by default | `GENERATION_ENABLED=false`. When enabled: 10ms timeout (very aggressive — mostly template mode). Adds ~2GB memory. |
| **gRPC** | Dependency present, not used | In `build.gradle`, but `application.properties` uses HTTP. The comment says "Phase 2 default". |
| **Redis Cluster** | Explicitly unsupported | Lua script cross-slot writes; must be standalone/replication. ElastiCache cluster mode must be disabled. |
| **Milvus in production** | Self-managed, no HA tooling | No Milvus operator, no backup/restore automation, no managed cloud version. |
| **Data pipeline** | Bootstrap only | `bootstrap_data.py` is a one-time init job. No incremental update pipeline. |
| **A/B testing** | Partial | Bucketing logic exists (`bucketing.py`, `bucket_user()`), `BASE_FLOW_MODE` config exists. No dashboard or analysis tooling. |
| **LegacyFeatureReader** | Debug/admin only | `feature_reader_legacy.py` exists but is explicitly marked "not the hot path." |
| **Kafka in K8s** | Strimzi, EKS only | Kafka is not in the minikube overlay; the local stack uses a single-node Docker Compose Kafka. Production uses Strimzi. |
| **Session-level features** | Wired in Lua, not read back | `session:{id}:clicks` is written in the Lua script, but there's no session feature reader in the inference service personalization. |

---

## 14. How To Explain This Project Clearly

### 30-second explanation
"ScaleStyle is a real-time personalized fashion recommendation engine. When a user searches, a Java API gateway calls a Python Ray Serve backend that embeds the query with BGE-large, retrieves the top 100 items from Milvus, reranks them with a cross-encoder, then applies real-time personalization boosts based on the user's recent clicks and category behavior — which are updated asynchronously via a Kafka consumer writing atomic Redis state. The whole system is deployed on Kubernetes with circuit breaking, tiered retries, and full distributed tracing."

### 2-minute explanation
"The system has three main runtime paths. The recommendation path: a Spring Boot gateway receives a query, calls Ray Serve over HTTP with a 450ms timeout and a circuit breaker, and Ray Serve runs a four-stage pipeline — intent routing, BGE-large embedding, Milvus ANN retrieval of 100 candidates, cross-encoder reranking of the top 50 — then applies personalization boosts before returning results.

The event path: when a user clicks a result, the gateway publishes a broker-acknowledged Kafka event. An event consumer reads it and executes a Lua script that atomically updates Redis with the user's recent clicks, category affinity (with exponential decay), item click signals, and windowed popularity buckets across 1h/24h/7d windows.

The personalization path connects these: at request time, the inference service loads a personalization snapshot in three pipelined Redis reads — user history, window materialization, item categories — and applies multiplicative boosts (1.5× for exact item match, 1.2× for category match) plus log-normalized popularity boosts, capped at 3×.

The system degrades gracefully at every layer: Redis unavailable → serve without personalization; inference timeout → serve popular items; reranker timeout → return retrieval order. Every degradation emits a labeled Prometheus counter. The whole system runs on EKS with Terraform-managed infrastructure including ElastiCache for Redis."

### 5-minute engineering walkthrough outline
1. **Architecture overview** — three services, two data stores, one event bus. Gateway → Ray Serve → [Milvus, Redis]. Event Consumer as async writer to Redis.
2. **Request path deep dive** — DeferredResult pattern in Java, WebClient with CB, Ray Serve actor graph (5+ deployments), asyncio.wait_for timeouts, 3-stage Redis read plan.
3. **Personalization design** — snapshot contract (load once, no N+1), behavior boost formula (multiplicative exact/category + additive log-normalized popularity), max boost cap.
4. **Event pipeline** — broker-ack semantics (not fire-and-forget), Lua atomic multi-key update, decay model, windowed popularity buckets, retry topology.
5. **Resilience decisions** — circuit breaker configuration, fail-fast commit semantics, NullFeatureReader recovery probe, degradation reason taxonomy.
6. **Deployment and observability** — K8s HPA for gateway/inference/consumer, OTel trace propagation across services, per-phase latency histograms.

---

## 15. Memory Aids / What To Remember

### 10 most important things to remember

1. **Three-layer timeout hierarchy at the gateway:** DeferredResult 600ms → Netty 450ms → Mono.timeout 350ms. Each layer has a distinct failure mode.

2. **The inference pipeline is sequential inside Ray Serve:** route → embed → retrieve → enrich → snapshot → rerank → boost. Each stage has an independent asyncio timeout with a fallback to popularity.

3. **Personalization snapshot is bounded at 3 Redis round trips.** No N+1. Stages 1 (user state), 2 (window materialization), 3 (item categories + ZMSCORE) are each pipelined. The process-local cache can reduce this to 1 round trip on warm paths.

4. **Kafka publish is broker-acknowledged (not fire-and-forget).** 200 to the client means the event reached Kafka. This is the explicit design choice that makes the event pipeline reliable.

5. **Lua script is the atomicity guarantee.** All 8 Redis state mutations happen in a single Lua call. Cross-slot → requires standalone Redis. ElastiCache cluster mode must be disabled.

6. **Circuit breaker is count-based, not time-based.** Sliding window of 50 calls, 50% failure rate → open. 10s wait, then 5 half-open probes.

7. **Decay is applied at read time (category affinity) and at write time (Lua script for clicks).** Popularity windowed buckets don't decay; they expire naturally. Global popularity uses a log-surrogate ZSET score to preserve ordering without full rescore.

8. **Fail-fast on commit uncertainty in the consumer.** `RetryPublishedCommitFailedError` is never swallowed. Process exits, K8s restarts, dedup key handles the duplicate.

9. **Redis Cluster mode is incompatible.** This is a hard architectural constraint, not a soft preference. Must be validated at startup.

10. **Generation and image search are optional feature flags.** `GENERATION_ENABLED=false` is the default. `vision_handle=None` is guarded. These are not on the main production path.

### 5 most important files to reread first

1. `inference-service/src/deployments/ingress.py` — the full pipeline, all fallback logic, personalization recovery
2. `inference-service/src/personalization/feature_reader.py` — the bounded read plan, distributed lock, window cache
3. `event-consumer/src/consumer.py` — the Lua script, retry topology, fail-fast commit semantics
4. `gateway-service/src/main/java/com/scalestyle/gateway/service/RecommendationService.java` — WebClient, CB, enrichment, fallback
5. `inference-service/src/config.py` — all timeout/model/decay/personalization config in one place

### 5 most important engineering themes

1. **Bounded latency at every layer.** Every downstream call has an explicit timeout + a fallback. No unbounded waits.
2. **Read amplification control.** Snapshot pattern, pipeline batching, process-local caches, and materialized windows all serve the same goal: fixed Redis cost per request regardless of candidate set size.
3. **At-least-once with explicit idempotency.** Kafka delivery guarantee + Lua dedup key = correct event processing without exactly-once Kafka.
4. **Explicit degradation over silent failure.** Every failure mode emits a labeled metric. Serving continues in a degraded state rather than returning errors.
5. **Atomic state consistency via Lua.** The Lua script is the heart of the online feature store's correctness guarantee. Its Redis Cluster incompatibility is the main operational constraint that flows from this choice.

---

## Appendix A: Request Path Map

```
RecommendationController.search()          [DeferredResult 600ms]
  → RecommendationService.searchAsync()   [Mono, Resilience4j CB]
    → WebClient POST / to Ray Serve        [Netty 450ms + Mono.timeout 350ms]
      → IngressDeployment.__call__()       [OTel trace context extract]
        → _search_impl(req)
          → RouterDeployment.route()       [intent: BROWSE/SEARCH, A/B flow]
          → EmbeddingDeployment.embed()    [BGE-large, 500ms timeout]
          → RetrievalDeployment.search()   [Milvus ANN recall_k=100, 300ms timeout]
          → _enrich_and_filter()           [Redis HGET item:*:meta, trim to 50]
          → FeatureReader.load_personalization_snapshot()
              Stage 1: LRANGE + HGETALL (user state)
              Stage 2: Materialized window resolution (0-3 RTTs)
              Stage 3: HGET categories + ZMSCORE popularity (1 pipeline)
          → RerankerDeployment.rerank()    [cross-encoder top-50, 250ms timeout]
          → BehaviorBoost.apply_boost()   [multiplicative boost + re-sort]
          → _contract_normalize()
    → ProductCacheService.enrichRecommendations()  [L1 Caffeine → L2 Redis]
  → ResponseEntity<CommonApiResponse<List<RecommendationDTO>>>
```

---

## Appendix B: Event Path Map

```
EventController.trackClick()
  → EventTrackingService.trackClick()
      → toCommand() → validateRequiredFields() → normalizeCommand()
      → toClickEventV1()  [UUID event_id, ISO-8601 timestamp]
      → EventProducerService.publishClick(event).get(6000ms)  [broker ACK sync]
        → Kafka topic: scalestyle.clicks (acks=all, idempotent, lz4)
          → EventConsumer (primary)
              → _CategoryCache.get(item_id)      [LRU 1h TTL]
              → Redis HGET item:{id}:meta category  [on LRU miss]
              → lua_upsert_script(dedupe_key, ARGV=[...21 params...])
                  → EXISTS dedupe:event_id → DUPLICATE
                  → SETEX dedupe:event_id {ttl} "1"
                  → LPUSH user:{id}:recent_clicks + LTRIM to 100
                  → HSET user:{id}:category_affinity (decay + increment)
                  → SET item:{id}:clicks (decay + increment)
                  → ZINCRBY popularity:bucket:1h/24h/7d:{bucket}
                  → ZADD global:popular (log-surrogate score)
                  → LPUSH session:{id}:clicks (if session_id present)
              → commit offset
              → on TRANSIENT_FAILURE: publish retry-1s + commit
              → on PERMANENT_FAILURE: publish DLQ + commit
          → EventConsumer (retry)
              retry-1s → retry-10s → retry-60s → DLQ
              partition pause (RETRY_ENFORCE_DELAY=true)
              fail-fast exit on RetryPublishedCommitFailedError
```

---

## Appendix C: Where Logic Lives

| Concern | Location |
|---|---|
| Transport (HTTP in/out) | `RecommendationController`, `IngressDeployment.__call__()` |
| Gateway business logic | `RecommendationService`, `EventTrackingService` |
| ML pipeline orchestration | `IngressDeployment._search_impl()` |
| Intent classification | `RouterDeployment.route()` |
| Embedding | `EmbeddingDeployment.embed()` |
| Vector retrieval | `RetrievalDeployment.search()` → Milvus |
| Cross-encoder reranking | `RerankerDeployment.rerank()` |
| Personalization feature loading | `FeatureReader.load_personalization_snapshot()` |
| Personalization boost application | `BehaviorBoost.apply_boost()` |
| Online feature writes | `EventConsumer.LUA_UPSERT_FEATURES` (Lua) |
| Retry / DLQ routing | `EventConsumer` (consumer.py, partition-pause + tier routing) |
| Popularity window materialization | `FeatureReader._resolve_materialized_popularity_windows()` |
| Product metadata enrichment (gateway) | `ProductCacheService.enrichRecommendations()` |
| Fallback logic | `RecommendationService.getPopularItemsFallback()`, `IngressDeployment._safe_popularity_topk()` |
| All timeouts and model params | `inference-service/src/config.py`, `application.properties` |
| Deployment topology | `infrastructure/k8s/`, `infrastructure/terraform/`, `docker-compose.yml` |

---

## Appendix D: What Changed My Mental Model

**1. The gateway fallback doesn't call the inference service.** When the circuit breaker opens or inference times out, the gateway reads from Redis materialized popularity windows directly — not from the `PopularityDeployment` inside Ray Serve. The inference service's `_safe_popularity_topk()` is the fallback *inside* Ray Serve; the gateway has its own parallel fallback path in `RecommendationService.getPopularItemsFallback()`.

**2. Personalization is bolt-on, not core to retrieval.** The embedding, retrieval, and reranking pipeline runs completely without user context. Personalization is a post-rerank score adjustment. This means the ML pipeline's output is independent of user state — user state only changes the ordering of already-retrieved candidates.

**3. The event consumer is single-threaded by design.** The `_CategoryCache` comment says "Thread-safety: the consumer loop is single-threaded, so no locking is needed." Scaling is via Kafka partitions and K8s pod replicas — not threads.

**4. Popularity windows and global popularity are separate data structures serving different purposes.** The windowed buckets (`popularity:bucket:1h/24h/7d:*`) are the primary signal for per-request personalization boost. The `global:popular` ZSET is the fallback for popularity-based recommendations when inference fails. They are written by the same Lua call but read by different subsystems.

**5. gRPC is present but not used.** The dependency tree includes gRPC and Protocol Buffers. `application.properties` comment says "Phase 2 default: call Ray Serve via HTTP." This suggests gRPC was an earlier design that was replaced by HTTP for simplicity/compatibility reasons — but the dependency was never removed from `build.gradle`.

**6. Session-level features are partially implemented.** The Lua script writes `session:{id}:clicks`. Nothing in the inference service reads this key. It's wired on the write side but has no corresponding feature reader — suggesting it was planned but not yet used in ranking.
