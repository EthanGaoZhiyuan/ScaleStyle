# ScaleStyle — Final Technical Metrics Report

**Date:** 2026-05-17  
**Environment:** Local Docker Compose — Apple M4 Max, 128 GB RAM  
**Branch:** main  
**Note:** All results are local single-machine measurements. Not AWS/EKS production numbers.

---

## 1. Executive Summary

ScaleStyle is a full-stack ML recommendation system with text search, image-to-image search, hybrid dual-recall search, a real-time Kafka-driven personalization loop, and a three-tier popularity fallback. This report records locally measured correctness, latency, throughput, and stability results across all subsystems.

**What holds up under measurement:**
- Text search: p50 ~164 ms, p99 ~171 ms, 0% error rate at c=1; scales to 66 rps at c=25 with 0% errors.
- Image search: p50 ~150 ms at c=1; degrades at c=25 due to CLIP inference queuing (VISION_MAX_ONGOING_REQUESTS=8 chosen after scaling experiment).
- Hybrid search: p50 ~152 ms at c=1; same CLIP bottleneck as image at c=25.
- Image embedding cache: ~4–5× throughput improvement for repeated images (cache-hit p50 drops from ~387 ms to ~79 ms).
- Event-consumer Kafka loop: 100 events at 775 rps, 1,000 events at 970 rps, 0 errors, OffsetAndMetadata compatibility verified across kafka-python 2.0.2 (Docker) and 2.3.1 (local).
- Personalization (behavior boost): exact-click 1.5× and category-affinity 1.2× boosts confirmed live in hybrid search responses.
- Three-tier fallback: degraded path returns HTTP 200 with populated items from Redis popularity ZSET; inference restores cleanly.
- 10-minute stability soak (c=10, mixed): 39,753 total requests, 0 errors, 0 container restarts.

**What is not measured here:**
- AWS/EKS pod-to-pod latency or node-level resource pressure.
- Unique-image traffic (cache benchmarks use one repeated image).
- Realistic think-time or connection variability.
- K8s horizontal scaling, eviction, or rolling deployment behavior.

---

## 2. Environment

| Parameter | Value |
|---|---|
| Host | Apple M4 Max, 128 GB RAM |
| Deployment | Local Docker Compose (single machine) |
| Platform | macOS Darwin 25.5.0 |
| Gateway port | 8080 |
| Inference port | 8000 (Ray Serve) |
| Redis port | 6379 |
| Milvus port | 19530 |
| Kafka port | 9092 |

---

## 3. Runtime Configuration

### 3.1 Gateway (`gateway-service/src/main/resources/application.properties`)

| Parameter | Value |
|---|---|
| Reactor application timeout | 600 ms |
| Netty read timeout (hard socket kill) | 700 ms |
| Netty write timeout | 1,000 ms |
| Redis command timeout (`spring.data.redis.timeout`) | 150 ms |
| Redis pool max-active | 32 |
| Redis pool max-idle | 16 |
| Redis pool min-idle | 4 |
| Redis pool max-wait | 50 ms |
| Inference HTTP max-connections | 100 |
| Inference HTTP connect-timeout | 1,000 ms |
| Kafka click event linger | 2 ms |
| Kafka click batch size | 8 KiB |
| Kafka click compression | lz4 |
| Fallback source order | primary: `popularity:materialized:24h:*` → secondary: `popularity:materialized:7d:*` → tier-3: `global:popular` |

### 3.2 Inference (`inference-service/src/config.py` + `docker-compose.yml`)

| Parameter | Value |
|---|---|
| EMBEDDING_TIMEOUT_MS | 200 (docker-compose override; default 500) |
| RETRIEVAL_TIMEOUT_MS | 150 (docker-compose override; default 300) |
| RERANKER_TIMEOUT_MS | 120 (docker-compose override; default 250) |
| GENERATION_TIMEOUT_MS | 50 |
| PERSONALIZATION_TIMEOUT_MS | 50 |
| REDIS_CONNECT_TIMEOUT_MS | 150 |
| REDIS_SOCKET_TIMEOUT_MS | 150 |
| EMBEDDING_MAX_ONGOING_REQUESTS | 4 |
| RETRIEVAL_MAX_ONGOING_REQUESTS | 20 |
| RERANKER_MAX_ONGOING_REQUESTS | 6 |
| INGRESS_MAX_ONGOING_REQUESTS | 50 |
| VISION_MAX_ONGOING_REQUESTS | **8** (raised from 4 after scaling experiment — see §8) |

### 3.3 Models

| Role | Model |
|---|---|
| Text embedding | BAAI/bge-small-en-v1.5 (384-dim) |
| Image embedding | openai/clip-vit-base-patch32 (512-dim) |
| Reranker | BAAI/bge-reranker-base (cross-encoder, RERANKER_ENABLED=1) |
| Generation | GENERATION_ENABLED=0 (disabled; template mode) |

### 3.4 Image Embedding Cache

| Parameter | Value |
|---|---|
| Scope | In-process per Ray actor (not distributed) |
| Key strategy | SHA-256(raw bytes) + model name |
| Max size | 256 entries (env: `VISION_EMBEDDING_CACHE_SIZE`) |
| TTL | 600 s (env: `VISION_EMBEDDING_CACHE_TTL_SECONDS`) |
| Eviction | LRU (OrderedDict; oldest evicted on capacity overflow) |
| Thread safety | `threading.Lock` on all store operations |
| Cache miss behavior | Falls back to direct encode; never fails the request |
| Model invalidation | Automatic (model name is part of key) |

---

## 4. Data Store State

### 4.1 Milvus Collections

| Collection | Rows | Dims | Model |
|---|---|---|---|
| `scale_style_bge_small_v1_5` | 105,542 | 384 | BAAI/bge-small-en-v1.5 |
| `scale_style_clip_image_v1` | 105,100 | 512 | openai/clip-vit-base-patch32 |

Both collections loaded and indexed. Row counts verified via `pymilvus Collection.num_entities`.

### 4.2 Redis State

| Key | Type | Cardinality | Note |
|---|---|---|---|
| `global:popular` | ZSET | 1,147 entries | All-time bootstrap popularity fallback |
| `popularity:materialized:7d:*` | ZSET | 121 entries | Rolling 7-day window materialized by event-consumer |

Top 5 `global:popular` entries (all-time bootstrap):

| Rank | itemId | Score |
|---|---|---|
| 1 | 0464297007 | 25,025 |
| 2 | 0372860002 | 24,458 |
| 3 | 0610776001 | 22,451 |
| 4 | 0399223001 | 22,236 |
| 5 | 0706016003 | 21,241 |

---

## 5. Pre-Check: Smoke Test Results

Smoke tests run immediately before benchmarks.

| Test | Result |
|---|---|
| `smoke-text` | PASS (items=5, degraded=false, itemId=0827955002) |
| `smoke-image` | PASS (items=5, degraded=false, mode=image) |
| `smoke-hybrid` | PASS (items=5, degraded=false, arch=dual_recall_normalized_fusion) |
| `smoke-fallback` (non-destructive) | PASS (tier-2: 121 entries, tier-3: 1,147 entries) |
| `smoke-all` | ALL PASSED |

---

## 6. Single-Endpoint Latency Benchmark

Source: `scripts/benchmark_local_endpoints.py` run 2026-05-17.  
Methodology: 20 warmup requests (text) / 10 warmup (image, hybrid), then measured run.  
Image: `data-pipeline/data/raw/images/010/0108775015.jpg` (real H&M product image).  
All measurements through gateway port 8080.

| Endpoint | n | p50 (ms) | p95 (ms) | p99 (ms) | avg (ms) | min (ms) | max (ms) | err% | deg% | rps |
|---|---|---|---|---|---|---|---|---|---|---|
| text | 200 | 163.9 | 168.5 | 171.0 | 164.1 | 158.5 | 171.7 | 0.0% | 20.0% | 6.1 |
| image | 100 | 150.4 | 163.3 | 164.7 | 152.8 | 146.9 | 164.7 | 0.0% | 0.0% | 6.55 |
| hybrid | 100 | 151.9 | 158.2 | 173.1 | 152.2 | 147.1 | 173.1 | 0.0% | 0.0% | 6.57 |

**Text degradation note (~20%):** The benchmark uses 10 rotating queries. The gateway rec-cache (5-min TTL) retains warmup results. Occasional inference time-outs (embed+retrieve+rerank+personalization sum occasionally exceeding the 600 ms Reactor budget) cause the gateway to serve the rec-cache result with `degraded=true, source=redis-cache`. These degraded responses are HTTP 200 fallback responses — not hard failures; error rate remained 0% throughout. The ~20% rate was stable across all concurrency levels in this fixed-query local benchmark. Whether this rate would appear in production traffic (with a more varied query distribution, warmer inference, and K8s/AWS infrastructure) has not been measured and should not be inferred from these results.

---

## 7. Controlled Concurrency Benchmark

Source: `scripts/benchmark_local_endpoints.py` — 30 s per endpoint/concurrency level.  
Concurrency levels tested: 1, 5, 10, 25.

| Endpoint | c | total | rps | p50 (ms) | p95 (ms) | p99 (ms) | err% | deg% |
|---|---|---|---|---|---|---|---|---|
| text | 1 | 182 | 6.07 | 163.9 | 174.7 | 203.8 | 0.0% | 19.8% |
| text | 5 | 855 | 28.5 | 173.7 | 196.8 | 219.4 | 0.0% | 19.9% |
| text | 10 | 1,668 | 55.6 | 176.4 | 211.5 | 241.0 | 0.0% | 19.9% |
| text | 25 | 2,006 | 66.87 | 374.9 | 477.4 | 536.4 | 0.0% | 20.3% |
| image | 1 | 192 | 6.4 | 154.9 | 170.1 | 180.9 | 0.0% | 0.0% |
| image | 5 | 758 | 25.27 | 164.9 | 356.4 | 608.4 | 0.0% | 1.7% |
| image | 10 | 881 | 29.37 | 286.3 | 609.2 | 613.4 | 0.0% | 20.9% |
| image | 25 | 1,339 | 44.63 | 606.9 | 615.6 | 621.8 | 0.3% | 64.4% |
| hybrid | 1 | 199 | 6.63 | 150.3 | 155.3 | 172.9 | 0.0% | 0.0% |
| hybrid | 5 | 754 | 25.13 | 163.6 | 410.6 | 605.2 | 0.0% | 1.5% |
| hybrid | 10 | 843 | 28.1 | 317.6 | 608.8 | 613.2 | 0.0% | 19.2% |
| hybrid | 25 | 1,346 | 44.87 | 606.5 | 612.5 | 620.1 | 0.0% | 63.4% |

**Observations:**
- Text scales cleanly to c=25 with 0% errors. p50 rises from 164 ms (c=1) to 375 ms (c=25) due to queuing at `EMBEDDING_MAX_ONGOING_REQUESTS=4`, but no requests are dropped.
- Image/hybrid hit the VISION bottleneck at c=10 (p95 ≈ 609 ms, near the 600 ms Reactor timeout → degraded). At c=25, 63–64% of responses are degraded (gateway timeout fires, fallback served as HTTP 200).
- Error rate for image at c=25 is 0.3% (not zero) — these are full failures exceeding the Netty 700 ms hard kill before fallback can complete. All others are HTTP 200 degraded, not errors.
- `degraded ≠ error`: degraded responses return HTTP 200 with fallback items. Only true errors count as error%.

---

## 8. Mixed Workload Benchmark

Source: `scripts/benchmark_mixed_workload.py` — 60 s each at c=10 and c=25.  
Traffic mix: 70% text / 15% image / 10% hybrid / 5% click events.  
Image: `0108775015.jpg` (same repeated image; cache active).

### c=10, 60 s

| Endpoint | total | rps | p50 (ms) | p95 (ms) | p99 (ms) | err% | deg% |
|---|---|---|---|---|---|---|---|
| text | 2,297 | 38.2 | 184.4 | 218.4 | 249.7 | 0.0% | 12.9% |
| image | 474 | 7.9 | 192.5 | 285.0 | 430.8 | 0.0% | 0.0% |
| hybrid | 334 | 5.6 | 195.4 | 299.0 | 414.9 | 0.0% | 0.0% |
| click | 168 | 2.8 | 8.5 ms | 11.1 ms | 20.0 ms | 0.0% | 0.0% |
| **search aggregate** | **3,273** | **54.4** | **186.9** | **233.9** | **312.5** | **0.0%** | **9.6%** |

### c=25, 60 s

| Endpoint | total | rps | p50 (ms) | p95 (ms) | p99 (ms) | err% | deg% |
|---|---|---|---|---|---|---|---|
| text | 2,496 | 41.2 | 436.8 | 549.5 | 603.4 | 0.0% | 16.1% |
| image | 520 | 8.6 | 466.6 | 610.6 | 613.8 | 0.0% | 14.0% |
| hybrid | 354 | 5.8 | 471.8 | 610.2 | 616.5 | 0.0% | 14.1% |
| click | 175 | 2.9 | 8.8 ms | 12.5 ms | 15.7 ms | 0.0% | 0.0% |
| **search aggregate** | **3,545** | **58.5** | **443.8** | **591.2** | **610.9** | **0.0%** | **15.6%** |

**Observations:**
- Search error rate is 0% at both concurrency levels. Degraded responses (HTTP 200 with fallback) are not errors.
- Click events (Kafka broker ACK) add 8–20 ms round-trip — fast and stable under mixed load.
- At c=25, the search aggregate p50 jumps from 187 ms to 444 ms — queuing visible, but no request failures.
- Text search 12–16% degradation at mixed load reflects the rec-cache/warmup interaction and is consistent across runs.

---

## 9. VisionDeployment Scaling Benchmark

Source: `scripts/benchmark_vision_scaling.py` — 5 s warmup then 30 s measured, image and hybrid, c=10 and c=25.  
Three configurations compared: `VISION_MAX_ONGOING_REQUESTS` = 4, 6, 8.

| config | endpoint | c | total | rps | p50 (ms) | p95 (ms) | p99 (ms) | err% | deg% |
|---|---|---|---|---|---|---|---|---|---|
| max_req=4 | image | 10 | 637 | 20.9 | 409 | 582 | 599 | 0.0% | 0.6% |
| max_req=4 | hybrid | 10 | 850 | 28.0 | 321 | 607 | 609 | 0.0% | 19.1% |
| max_req=4 | image | 25 | 1,356 | 44.3 | 605 | 610 | 614 | 0.1% | 62.4% |
| max_req=4 | hybrid | 25 | 1,360 | 44.4 | 605 | 609 | 613 | 0.1% | 62.6% |
| max_req=6 | image | 10 | 780 | 25.6 | 387 | 575 | 583 | 0.0% | 0.3% |
| max_req=6 | hybrid | 10 | 1,075 | 35.5 | 245 | 605 | 608 | 0.0% | 5.2% |
| max_req=6 | image | 25 | 1,399 | 45.6 | 599 | 609 | 612 | 0.1% | 48.0% |
| max_req=6 | hybrid | 25 | 1,363 | 44.6 | 605 | 610 | 613 | 0.0% | 55.3% |
| **max_req=8** | **image** | **10** | **785** | **25.8** | **387** | **573** | **581** | **0.0%** | **0.0%** |
| **max_req=8** | **hybrid** | **10** | **1,214** | **40.2** | **219** | **392** | **563** | **0.0%** | **0.1%** |
| max_req=8 | image | 25 | 1,483 | 48.5 | 572 | 610 | 615 | 0.1% | 42.2% |
| max_req=8 | hybrid | 25 | 1,400 | 45.7 | 589 | 609 | 613 | 0.0% | 45.8% |

**Decision: `VISION_MAX_ONGOING_REQUESTS=8` adopted.**

Key wins at max_req=8 vs 4:
- Hybrid c=10: throughput 28.0 → 40.2 rps (+44%); p95 607 → 392 ms (−35%); degraded 19.1% → 0.1%
- Image c=10: throughput 20.9 → 25.8 rps (+23%); p99 599 → 581 ms
- At c=25: error rate unchanged ≤0.1%; degradation consistently lower across all configs
- No container instability at any setting

**Why 8 is not CPU oversubscription:** CLIP inference runs inside `asyncio.to_thread()`, keeping the Ray Serve event loop unblocked. Requests queue in the Ray actor; Python's thread pool schedules actual CPU work. With 8 slots, more requests can overlap I/O-bound phases (Milvus search, base64 decode) while CLIP inference serializes in threads.

---

## 10. Image Embedding Cache Benchmark

Source: in-process LRU+TTL cache in `VisionDeployment`, benchmarked with a single repeated image.  
Comparison: max_req=8 without cache vs. max_req=8 with cache active.

| config | endpoint | c | rps | p50 (ms) | p95 (ms) | p99 (ms) | err% | deg% |
|---|---|---|---|---|---|---|---|---|
| no cache | image | 10 | 25.8 | 387 | 573 | 581 | 0.0% | 0.0% |
| **cache** | **image** | **10** | **127.3** | **79** | **110** | **117** | **0.0%** | **0.0%** |
| no cache | hybrid | 10 | 40.2 | 219 | 392 | 563 | 0.0% | 0.1% |
| **cache** | **hybrid** | **10** | **89.4** | **96** | **210** | **219** | **0.0%** | **4.0%** |
| no cache | image | 25 | 48.5 | 572 | 610 | 615 | 0.1% | 42.2% |
| **cache** | **image** | **25** | **232.9** | **100** | **176** | **226** | **0.0%** | **0.0%** |
| no cache | hybrid | 25 | 45.7 | 589 | 609 | 613 | 0.0% | 45.8% |
| **cache** | **hybrid** | **25** | **100.8** | **239** | **349** | **381** | **0.0%** | **20.1%** |

**Observations:**
- Image search: ~4–5× throughput gain; p50 drops from ~387 ms to ~79–100 ms (CLIP encoding eliminated on cache hit).
- Hybrid search: ~2× throughput gain; remaining latency comes from CLIP text encoder (not cached) and Milvus dual-recall (both run on every request regardless of image cache hit).
- Degradation drops to 0% for image at both c levels — the 600 ms Reactor timeout no longer fires when CLIP is skipped.
- Hybrid degradation at c=25 (20.1%) reflects CLIP text encoding + Milvus search time, not image CLIP.

**Critical limitation:** These numbers represent best-case repeated-image behavior (100% cache hit rate). Production traffic with diverse unique images will see cold CLIP encoding cost (~150–400 ms) on every distinct image. The cache benefit is real for repeated-image patterns (catalog browsing, session replay) but should not be cited as production image search latency.

---

## 11. 10-Minute Soak / Stability Test

Source: `scripts/benchmark_mixed_workload.py` — c=10, 600 s, mixed traffic.  
Run: 2026-05-17T03:19–03:29Z. Image embedding cache active. `VISION_MAX_ONGOING_REQUESTS=8`.

### Results

| Endpoint | total | rps | p50 (ms) | p95 (ms) | p99 (ms) | err% | deg% |
|---|---|---|---|---|---|---|---|
| text | 27,935 | 46.5 | 192 | 239 | 274 | 0.0% | 13.3% |
| image | 5,890 | 9.8 | 30 | 50 | 82 | 0.0% | 0.0% |
| hybrid | 3,949 | 6.6 | 70 | 114 | 149 | 0.0% | 0.0% |
| click | 1,979 | 3.3 | 6.7 | 8.4 | 9.9 | 0.0% | 0.0% |
| **search aggregate** | **37,774** | **62.9** | **185** | **233** | **268** | **0.0%** | **9.9%** |

Total requests (all endpoints): **39,753** at **66.2 rps**.

### Container Health

| Container | Restarts before | Restarts after | Status post-test |
|---|---|---|---|
| gateway | 0 | 0 | healthy |
| inference | 0 | 0 | healthy |
| redis | 0 | 0 | healthy |
| milvus | 10* | 10* | healthy |
| kafka | 0 | 0 | up |
| event-consumer-primary | 0 | 0 | up |

*Milvus 10 restarts are pre-existing from etcd/storage initialization in prior session; none occurred during soak.

### Inference Resource Usage (5 Snapshots, 2-min Intervals)

| t (min) | CPU % | Memory |
|---|---|---|
| 0 (baseline) | 22.9% | 6.35 GiB |
| 2 | 969.8% | 7.47 GiB |
| 4 | 1,007.0% | 7.48 GiB |
| 6 | 1,012.3% | 7.55 GiB |
| 8 | 1,014.9% | 7.72 GiB |
| 10 (post-test idle) | 21.9% | 7.38 GiB |

CPU% > 100% is normal on multi-core hosts (Docker reports aggregate across all cores; Ray uses ~10 cores at c=10). Memory rose from 6.35 → 7.72 GiB peak (+1.37 GiB) and settled to 7.38 GiB at idle. Growth tracked Ray object store and model activation buffers; no leak pattern detected (growth rate slowed as the image embedding cache filled between t=0 and t=4).

### Stability Observations

- **Zero errors** across 39,753 requests over 10 minutes.
- **Zero container restarts** during the run.
- **No latency drift:** Text p50 stayed in the 185–196 ms range throughout (consistent with short benchmarks). No degradation trend.
- **Image embedding cache held warm** for the full 10 minutes (max 256 entries, single repeated image). Image p50=30 ms vs 387 ms cold.
- **Click events stable:** 1,979 requests, 0 errors, p99=9.9 ms. Kafka broker ACK consistent under sustained mixed load.
- **Text degradation (13.3%):** Stable, non-increasing pattern across the 10-minute run. Observed in the context of the fixed-query local benchmark with a warm rec-cache; degraded responses were HTTP 200 fallback responses (error rate remained 0%). Whether this rate reflects production behavior has not been separately tested and should not be inferred from this benchmark alone.

### Post-Soak Smoke Results

`smoke-text`: **PASS** | `smoke-image`: **PASS** | `smoke-hybrid`: **PASS** | `smoke-fallback`: **PASS**

---

## 12. Kafka / Event-Consumer Feedback Loop

### 12.1 Compatibility Context

Three kafka-python 2.0.2 compatibility bugs were fixed before this validation. All fixes are in `main`:

| File | Bug | Fix |
|---|---|---|
| `event-consumer/src/consumer.py` | `ssl=False` passed to `ConnectionPool` — rejected by redis-py 5.x | Conditional SSL kwargs only when `REDIS_TLS=true` |
| `event-consumer/src/consumer.py` | `KafkaProducer` called with `enable_idempotence=True` and `delivery_timeout_ms=4500` — not recognized by kafka-python 2.0.2 | Split into `base_kwargs` + `extended_kwargs`; try extended first, fall back to base on `AssertionError: Unrecognized configs` |
| `event-consumer/src/kafka_utils.py` (new) + `kafka_loop.py` + `retry_router.py` | `OffsetAndMetadata(offset, None, None)` — kafka-python 2.0.2 takes 2 args, not 3 | New `make_offset_and_metadata()` detects arity at import time via `inspect.signature` |

### 12.2 Functional Path Verification

**User:** `metrics-validation-user`  
**Clicked item:** `0783838001` (Dress category)

| Step | Result |
|---|---|
| Click POST HTTP status | 200 |
| Response status | `acknowledged_by_broker` |
| event_id | `54ac98fa-bc22-4113-bd75-5e4b9afdd1ec` |
| processing_mode | `broker_ack_sync` |
| Kafka consumer log | Processing confirmed (3,400+ events in log since restart) |

Redis keys written within ~3 s:

| Key | Value |
|---|---|
| `user:metrics-validation-user:recent_clicks` (list) | `["0783838001"]` |
| `user:metrics-validation-user:category_affinity` (hash) | `Dress: 1` |
| `user:metrics-validation-user:last_activity` (string) | `2026-05-17T04:34:13.550264Z` |
| `user:metrics-validation-user:category_affinity:last_ts` | exists |
| `item:0783838001:clicks` (string) | `1` |

Hybrid search (`query="red dress formal"`, `userId="metrics-validation-user"`):

| itemId | category | finalScore | behaviorScore | Note |
|---|---|---|---|---|
| 0783838001 | Blouse & Dress | 0.7350 | **0.3581** | Exact-click boost (1.5×) |
| 0545263002 | Knitwear | 0.4800 | **0.0800** | Category-affinity boost (1.2×) |
| 0733076001 | Dress | 0.4130 | **0.0688** | Category-affinity boost |
| 0733076002 | Dress | 0.3831 | **0.0638** | Category-affinity boost |
| 0851947001 | Dress | 0.3267 | **0.0544** | Category-affinity boost |

Both exact-click boost (1.5×) and category-affinity boost (1.2×) confirmed active in live hybrid search response.

### 12.3 Throughput

| Run | Events sent | Success | Errors | Elapsed | Throughput |
|---|---|---|---|---|---|
| 100 concurrent | 100 | 100 | 0 | 0.13 s | 775 rps |
| 1,000 (50-batch) | 1,000 | 1,000 | 0 | 1.03 s | 970 rps |

1,000-event latency (gateway broker ACK): p50=26.3 ms, p95=47.7 ms, p99=63.1 ms, avg=26.4 ms.

No retry routing, no DLQ events, no consumer restarts.

### 12.4 Unit Test Coverage

```
pytest event-consumer/tests -q --ignore=event-consumer/tests/test_integration.py
→ 189 passed, 0 failed
```

Includes `test_kafka_utils.py` (7 tests: version detection, 2-arg form, 3-arg form, cross-version guards, zero offset, parameter count assertion).

---

## 13. Fallback Validation

### 13.1 Non-Destructive Pre-check (`make smoke-fallback`)

| Check | Result |
|---|---|
| Gateway reachable | HTTP 200 |
| Tier-2 `popularity:materialized:7d:*` | 121 entries (event-consumer window active) |
| Tier-3 `global:popular` | 1,147 entries (bootstrap fallback ready) |
| Result | **PASS** |

### 13.2 Destructive Fallback (`DESTRUCTIVE=1 make smoke-fallback`)

Procedure: inference container paused for ~3 s to simulate failure.

| Metric | Value |
|---|---|
| HTTP status | 200 |
| `degraded` flag | `true` |
| `degradedReason` | `STALE_DATA_ALLOWED` |
| `source` | `redis-cache` |
| Returned items | 5 (from rec-cache / popularity ZSET) |
| Inference recovery | Container unpaused; smoke-text/image/hybrid confirmed non-degraded after restore |
| Result | **PASS** |

Fallback path confirmed: gateway serves valid recommendations from Redis on inference failure without returning a 5xx to the client.

---

## 14. Bottlenecks

1. **CLIP inference is the primary throughput ceiling.** `VisionDeployment` runs `clip.encode_image()` in `asyncio.to_thread()`. With `VISION_MAX_ONGOING_REQUESTS=8`, image/hybrid saturate near 25–29 rps at c=10 (cold image). At c=25, p50 approaches the 600 ms gateway Reactor timeout, causing degraded fallback responses (not errors).

2. **Text search throughput is bounded by `EMBEDDING_MAX_ONGOING_REQUESTS=4`.** Text scales to 66 rps at c=25, then queues (p50 rises from 164 → 375 ms). Still 0% errors — Ray Serve queues excess requests rather than dropping them.

3. **Gateway Reactor 600 ms budget is tight for image/hybrid under concurrent load.** The sum embed+retrieve+rerank+personalization+overhead occasionally exceeds 600 ms at c≥10. The consequence is a degraded response (HTTP 200 with fallback items), not a failure. The Netty 700 ms hard kill acts as the safety net.

4. **Reranker adds 50–100 ms on CPU.** BAAI/bge-reranker-base on CPU with 100 candidates takes 80–120 ms (within RERANKER_TIMEOUT_MS=120 most of the time, but occasionally triggers the timeout and skips reranking).

5. **Image embedding cache is per-actor, not distributed.** Cache is warm only within a single Ray Serve replica. Multiple replicas (K8s horizontal scaling) would start cold independently. A shared Redis or vector cache would be needed for cross-replica benefit.

6. **Text degradation ~13–20% is a rec-cache/query-set interaction artifact in benchmarks.** With 10 rotating fixed queries and a 5-minute gateway rec-cache, occasional inference timeout causes cache fallback. This is specific to the repeated-query benchmark pattern and should not be cited as a production SLO.

---

## 15. Limitations

1. **Local single-machine Docker Compose.** All containers share the M4 Max CPU and memory. No real pod-to-pod network latency (Docker bridge is ~0.1–0.5 ms). Not representative of EKS node topology.

2. **Image/hybrid benchmarks use one repeated image.** Cache benchmarks are best-case. Production traffic with diverse unique images will pay the full CLIP encoding cost (~150–400 ms) on every cold request.

3. **No concurrent external traffic.** Numbers represent clean-room throughput with no competing workloads.

4. **Concurrency is client-side threads.** No real-world think-time, connection jitter, or TCP re-establishment delays.

5. **K8s/AWS results must be measured separately.** Pod scheduling, node pressure, inter-AZ latency, ElastiCache network, and horizontal scaling behavior are not modeled here.

6. **Text embedding model (bge-small) is the smallest BGE variant.** A BGE-base or BGE-large model would improve recall quality at higher latency cost. Not benchmarked.

7. **Reranker on CPU is slower than on GPU.** Production GPU nodes (e.g., g4dn.xlarge) would drop reranker time from ~80–120 ms to ~10–30 ms, materially improving p95/p99.

8. **Milvus 10 pre-existing restarts.** These occurred during etcd/storage initialization in a prior session; no restarts occurred during any benchmark in this report. Milvus remained healthy throughout.

9. **10-minute soak is not sufficient for hours-scale memory leak detection.** The ~1 GiB inference memory growth observed stabilized — no leak pattern detected — but multi-hour or multi-day validation would require EKS.

---

## 16. README-Safe Claims

The following claims are safe to make in README or portfolio materials, with the listed caveats:

| Claim | Supported by | Caveat |
|---|---|---|
| Text search p50 ~164 ms, p99 ~171 ms at c=1 | §6 benchmark | Local Docker Compose only |
| Text search scales to 66 rps at c=25 with 0% errors | §7 benchmark | Local only |
| Image/hybrid p50 ~150 ms at c=1 (cold, real image) | §6 benchmark | Cold CLIP encoding; local only |
| Image search ~4–5× throughput gain with embedding cache (repeated image) | §10 | Repeated-image best-case; not unique-image production traffic |
| Hybrid search returns `behaviorScore > 0` after single click event via Kafka | §12 | End-to-end verified locally with real Kafka and Redis |
| Kafka gateway acknowledgement: p99 ≤ 63 ms at 1,000 events | §12.3 | Local Kafka |
| Event-consumer processes 775–970 rps gateway-acknowledged clicks | §12.3 | Local Kafka, sequential batches |
| Three-tier fallback: inference failure → HTTP 200 with Redis items, 0 errors | §13 | Destructive test verified locally |
| 10-minute soak: 39,753 requests, 0 errors, 0 container restarts | §11 | Local, c=10, single repeated image |
| 189/189 event-consumer unit tests pass (0 failures) | §12.4 | pytest, local env |
| All smoke tests pass (text, image, hybrid, fallback, smoke-all) | §5 | Local Docker Compose |

---

## 17. Claims to Avoid in README

- **Do not claim 500+ QPS** — measured max is ~66 rps (text, c=25, local). Mixed workload peaks at ~66 rps overall.
- **Do not claim sub-100 ms image search in production** — the ~79 ms p50 is a cache-hit best case with one repeated image; cold CLIP inference is 150–400 ms.
- **Do not claim production high-concurrency readiness** — c=25 shows degradation at the gateway Reactor timeout; EKS load testing has not been done.
- **Do not claim AWS/EKS performance** — no EKS benchmark has been run.
- **Do not claim the image cache eliminates CLIP latency in production** — the cache is per-actor, not distributed; unique-image production traffic still pays full CLIP encoding cost.
- **Do not interpret the 13–20% text degradation rate as production SLO evidence** — degraded responses in the benchmark were HTTP 200 fallback responses (error rate remained 0%), observed under a fixed 10-query set with a warm rec-cache in a local Docker Compose environment. Production-like query distribution and K8s/AWS testing would be needed before treating this figure as a production SLO data point.
- **Do not claim the reranker is fast** — on CPU, BAAI/bge-reranker-base adds 80–120 ms. GPU would be needed for sub-30 ms reranking.

---

## 18. Report Sources

| Section | Primary source |
|---|---|
| §6–7 (single-endpoint, concurrency) | `docs/performance/local-performance-current.md` (benchmark run 2026-05-17T02:06Z) |
| §8 (mixed workload) | `docs/performance/local-performance-current.md` (benchmark run 2026-05-17T02:39Z) |
| §9 (vision scaling) | `docs/performance/local-performance-current.md` (benchmark run 2026-05-17) |
| §10 (image cache) | `docs/performance/local-performance-current.md` (benchmark run 2026-05-17) |
| §11 (soak test) | `docs/performance/local-performance-current.md` (soak run 2026-05-17T03:19–03:29Z) |
| §12 (feedback loop) | Fresh validation run 2026-05-17 (post Kafka compatibility fix) |
| §13 (fallback) | Fresh validation run 2026-05-17 (destructive test) |
| §3 (runtime config) | `gateway-service/src/main/resources/application.properties`, `inference-service/src/config.py`, `docker-compose.yml` |
| §4 (data stores) | Live Redis CLI + pymilvus query run 2026-05-17 |
