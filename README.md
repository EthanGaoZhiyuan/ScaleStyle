# ScaleStyle

Real-time personalized fashion recommendation system. Accepts natural-language or image queries, retrieves candidates from a vector database, reranks with a cross-encoder, and applies a behavior boost derived from each user's recent click history and rolling popularity signals. Click events flow asynchronously through Kafka into Redis, so the next request immediately reflects the user's most recent behavior.

Fully runnable locally via Docker Compose. Kubernetes manifests (Minikube + EKS overlays) and Terraform for AWS are written; AWS is not deployed.

---

## Key capabilities

- **Text search** — BGE-small-en-v1.5 embedding → Milvus ANN retrieval → bge-reranker-base cross-encoder reranking
- **Image search** — CLIP ViT-B/32 embedding → Milvus ANN retrieval; opt-in via `VISION_ENABLED=1`
- **Hybrid search** — fused text + image dual recall with normalized score merge; requires `VISION_ENABLED=1`
- **Real-time personalization** — BehaviorBoost: click history (1.5×), category affinity (1.2×, time-decayed), popularity signals; max 3× cap
- **Kafka click ingestion** — idempotent producer; HTTP 200 only on confirmed broker ACK
- **Tiered retry + DLQ** — retry.1s → retry.10s → retry.60s; primary and retry consumers run independently
- **Three-tier popularity fallback** — 24h → 7d → `global:popular`; Resilience4j circuit breaker at 50% failure / 50-call window
- **Distributed tracing + observability** — W3C `traceparent`, Jaeger, Prometheus, Grafana

---

## Architecture

Java Spring Boot gateway → Ray Serve inference pipeline → Milvus (ANN retrieval) + Redis (feature store + popularity cache). Click events: Gateway → Kafka → Event Consumer → Redis in ~3 seconds.

![System architecture overview](docs/assets/architecture/production-recommendation-system-architecture.png)

![Runtime detail](docs/assets/architecture/detailed-runtime-architecture.png)

### Services

| Service | Stack | Responsibility |
|---|---|---|
| `gateway-service` | Java 21, Spring Boot, Reactor WebClient | Public API, Caffeine L1 + Redis L2 product cache, Kafka click publishing, Resilience4j circuit breaker |
| `inference-service` | Python, Ray Serve, FastAPI | Embedding, ANN retrieval, reranking, BehaviorBoost personalization; CLIP vision (opt-in), Qwen2 generation (opt-in) |
| `event-consumer` | Python, Kafka | Lua-atomic Redis writes for click history, category affinity, and popularity buckets; primary + retry consumers |
| `data-pipeline` | Python | One-time ETL: BGE-small embeddings → Milvus, item metadata → Redis, popularity seed |

### Models

~105,000 H&M article vectors per Milvus collection. Source Parquet files (pre-computed embeddings) are **not included**; obtain the H&M dataset from [Kaggle](https://www.kaggle.com/competitions/h-and-m-personalized-fashion-recommendations/data) and generate embeddings before running bootstrap.

| Role | Model | Default |
|---|---|---|
| Text embedding | `BAAI/bge-small-en-v1.5` | On |
| Image embedding | `openai/clip-vit-base-patch32` | Off — `VISION_ENABLED=1` to enable |
| Reranker | `BAAI/bge-reranker-base` | On |
| Generation | `Qwen/Qwen2-1.5B-Instruct` | Off — `GENERATION_ENABLED=1`, requires 12Gi |

---

## Quick start

### Prerequisites

- Docker + Docker Compose
- Python 3.10+
- H&M article Parquet files with BGE-small-en-v1.5 embeddings at `data-pipeline/data/processed/`

### 1. Bootstrap data

```bash
cd data-pipeline
pip install -r requirements.txt

# Start storage dependencies
docker compose up -d redis milvus etcd minio

# Load embeddings → Milvus, metadata → Redis
python src/bootstrap_data.py
```

### 2. Start the full stack

```bash
cd ..
docker compose up -d
```

| Service | Port |
|---|---|
| Gateway | 8080 |
| Inference | 8000 |
| Event Consumer (primary) | 8081 |
| Event Consumer (retry) | 8082 |
| Grafana | 3000 (`admin` / `admin`) |
| Jaeger | 16686 |
| Prometheus | 9090 |
| Milvus | 19530 |
| Redis | 6379 |
| Kafka | 9092 |

```bash
# Wait for inference to finish loading models (~30–60s):
docker compose logs -f inference | grep "Serve application is ready"
```

> Image and hybrid search require `VISION_ENABLED=1`. This is set in `docker-compose.yml` by default for local dev.

---

## API

### Text search

```bash
curl "http://localhost:8080/api/recommendation/search?query=slim+fit+jeans&k=5"

# With personalization
curl "http://localhost:8080/api/recommendation/search?query=summer+dress&userId=user123&k=10"
```

Response includes `behaviorScore` when personalization is active:

```json
{
  "status": "success",
  "data": [{ "article_id": "0108775015", "product_name": "Slim Fit Jeans",
             "score": 0.82, "behaviorScore": 0.3581, "rank": 1 }]
}
```

### Click event ingestion

```bash
# user_id and item_id are snake_case in this DTO
curl -X POST http://localhost:8080/api/events/click \
  -H "Content-Type: application/json" \
  -d '{"user_id":"user123","item_id":"0108775015","position":1,"source":"search","device":"web"}'
```

Returns HTTP 200 only after broker ACK. Returns HTTP 503 on timeout or rejection.

For image search, hybrid search, and cache diagnostics see [`docs/api/`](docs/api/).

---

## Testing

```bash
cd inference-service && pytest tests/ -v
cd event-consumer  && pytest tests/ -v --ignore=tests/test_integration.py
cd gateway-service && ./gradlew test
cd data-pipeline   && pytest tests/ -v --ignore=tests/integration/ \
                        --ignore=tests/test_generate_image_embeddings.py \
                        --ignore=tests/test_bootstrap_image_collection.py
```

Tests cover Lua-atomic Redis writes, BehaviorBoost scoring caps, feature reader exponential decay, personalization contract (fakeredis), all timeout values, Redis startup validation, circuit-breaker fallback, and end-to-end click ingestion. `test_generate_image_embeddings.py` and `test_bootstrap_image_collection.py` SIGSEGV on macOS with numpy 2.x / torchvision — host toolchain issue, not a code defect.

---

## Performance

> Local Docker Compose on Apple M4 Max (128 GB RAM), CPU-only inference. Not AWS/EKS numbers.

| Endpoint | p50 | p99 | Error rate |
|---|---|---|---|
| Text search | 163.9 ms | 171.0 ms | 0% |
| Image search | 150.4 ms | 164.7 ms | 0% |
| Hybrid search | 151.9 ms | 173.1 ms | 0% |

Mixed workload (70% text / 15% image / 10% hybrid / 5% click):

| Concurrency | Search RPS | Aggregate p99 | Hard error rate |
|---|---|---|---|
| c=10 | 54.4 | 312.5 ms | 0% |
| c=25 | 58.5 | 610.9 ms | 0% |

10-minute soak (c=10): 39,753 requests — 0 hard errors — 0 container restarts. "Degraded" responses are HTTP 200 with fallback items, not hard errors. Full report: [`docs/performance/final-technical-metrics.md`](docs/performance/final-technical-metrics.md)

---

## Resilience

Three-tier popularity fallback when inference is unavailable: 24h materialized → 7d materialized → `global:popular` (1,147 entries). Response shape is unchanged; `degradation_reason` field identifies the source. Verified live (inference container paused). Real-time feedback loop: click → Kafka → Redis within ~3s. See [`docs/DEGRADATION_REASONS.md`](docs/DEGRADATION_REASONS.md).

---

## Deployment status

| Layer | Status |
|---|---|
| K8s base manifests | Complete |
| Minikube overlay — static validation | Passes |
| Minikube overlay — local K8s test | Gateway path partially validated |
| EKS overlay — `kubectl --dry-run=client` | Passes (19 resources, 0 errors) |
| Full K8s E2E | Not performed |
| Terraform — `terraform validate` | Passes |
| AWS resources | **Not provisioned** |

ElastiCache cluster mode is disabled because the event-consumer's Lua script atomically updates keys across `user:*`, `popularity:*`, and `item:*` namespaces — Redis Cluster would produce `CROSSSLOT` errors. EFS model cache storage is not yet provisioned; once AWS is set up, see the `Makefile` for the full deploy sequence.

---

## Known limitations

- **No GPU runtime validated** — local inference is CPU-only; `NUM_GPUS` config exists but no GPU node is provisioned
- **Vision and generation are opt-in** — both disabled by default; generation requires 12Gi memory
- **No authentication** — all endpoints are open; X-API-Key filter is the planned next step
- **Data not included** — Parquet files must be sourced and placed at `data-pipeline/data/processed/` before bootstrap
- **AWS not deployed** — all benchmarks are local Docker Compose only; EKS/ElastiCache performance not measured
- **No offline evaluation** — NDCG/recall@k against ground truth is not wired

---

## Future work

- Offline NDCG / Recall@K evaluation against H&M ground-truth data
- GPU node group for EKS (env vars already in place)
- Multi-broker Kafka (3-node Strimzi for production durability)
- API authentication (X-API-Key gateway filter)

---

## Where to start

- [`gateway-service/.../RecommendationService.java`](gateway-service/src/main/java/com/scalestyle/gateway/service/RecommendationService.java) — timeout budget, circuit breaker fallback, metadata enrichment
- [`inference-service/src/deployments/ingress.py`](inference-service/src/deployments/ingress.py) — Ray Serve pipeline orchestration and degradation handling
- [`inference-service/src/personalization/behavior_boost.py`](inference-service/src/personalization/behavior_boost.py) — click, category affinity, and popularity boosting logic
- [`event-consumer/src/feature_update_handler.py`](event-consumer/src/feature_update_handler.py) — Kafka event to Redis feature writes
- [`docs/performance/final-technical-metrics.md`](docs/performance/final-technical-metrics.md) — measured local performance and validation results

---

## License

No license specified.
