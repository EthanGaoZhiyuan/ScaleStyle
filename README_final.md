# ScaleStyle

**Cloud-native fashion recommendation system with retrieval, reranking, real-time behavior updates, and production-oriented runtime controls.**

---

## Project Overview

ScaleStyle is a recommendation platform for a fashion e-commerce setting. Its main goal is to serve relevant product recommendations with bounded latency while incorporating recent user behavior into ranking decisions.

The repository combines:

- a **Java gateway** for API handling, caching, fallback, and event publishing
- a **Python inference service** built around **Ray Serve** for embedding, retrieval, reranking, and optional augmentation
- a **Kafka-based event pipeline** that updates online user/item signals in **Redis**
- **Milvus** for vector retrieval
- **Kubernetes** and **Terraform** assets for containerized and cloud deployment
- **Prometheus / Jaeger / Grafana-related** observability assets

This README describes the project based on the **current repository snapshot**. Where integration is partial or environment-specific, that is called out explicitly.

---

## Key Capabilities

- Semantic text search over product data using embeddings and vector retrieval
- Reranking of retrieved candidates before final response assembly
- Real-time behavior-aware ranking adjustments using Redis-backed online signals
- Kafka-backed click event ingestion with retry and DLQ handling
- Popularity-based fallback when inference is unavailable or degraded
- Production-oriented timeout, circuit-breaker, and degradation handling in the gateway
- Ray Serve–based inference orchestration with distinct deployment roles
- Kubernetes manifests and AWS Terraform modules for deployment structure
- Metrics and trace propagation hooks across major services
- Test coverage for contracts such as degradation behavior, atomic Redis updates, and runtime boundaries

---

## System Architecture

At a high level, ScaleStyle is split into three runtime paths:

1. **Serving path**
   - The gateway receives recommendation requests.
   - It checks local and Redis-backed caches where applicable.
   - It calls the inference service over HTTP.
   - The inference service embeds the query, retrieves candidates, reranks them, and optionally applies personalization signals.
   - The gateway enriches returned item identifiers with metadata and sends the final response.

2. **Event / online feature path**
   - The gateway publishes click events to Kafka.
   - The event consumer processes those events with retry / DLQ behavior.
   - Redis is updated with user and item behavior signals used by later requests.

3. **Fallback / degradation path**
   - If inference is unavailable or times out, the gateway can degrade to popularity-oriented results.
   - If personalization features are unavailable, the inference service can continue serving without behavior boosts.
   - Degradation reasons are surfaced as part of the runtime contract rather than being silently ignored.

### Main Subsystems

**Gateway Service (`gateway-service/`)**  
Spring Boot service responsible for request validation, cache-aware response assembly, downstream inference calls, fallback behavior, and event publishing.

**Inference Service (`inference-service/`)**  
Python service organized around Ray Serve deployments. It contains the query serving flow, retrieval/reranking logic, popularity access, and optional generation / multimodal-related components.

**Event Consumer (`event-consumer/`)**  
Kafka consumer process that handles click-event ingestion, retry / DLQ routing, and atomic Redis feature updates.

**Data Pipeline (`data-pipeline/`)**  
Offline/bootstrap utilities for loading product metadata and vector-related data into runtime stores.

**Infrastructure (`infrastructure/`)**  
Kubernetes manifests and Terraform modules for local and AWS-oriented deployment.

**Observability (`observability/`)**  
Prometheus configuration, alert rules, and Grafana-related assets for service visibility.

---

## Architecture Diagram

```text
                        +----------------------+
                        |    Client / UI       |
                        +----------+-----------+
                                   |
                                   v
                    +--------------+--------------+
                    |     Gateway Service         |
                    |  Spring Boot / HTTP API     |
                    |  cache + fallback + events  |
                    +------+---------------+------+
                           |               |
                           | HTTP          | Kafka produce
                           v               v
              +------------+----+    +-----+----------------+
              | Inference Service |    |      Kafka         |
              | Ray Serve / FastAPI|    +-----+-------------+
              +------+-------------+          |
                     |                        v
                     |                +-------+--------+
                     |                | Event Consumer |
                     |                | retry / DLQ    |
                     |                +-------+--------+
                     |                        |
                     v                        v
              +------+--------+        +------+--------+
              |   Milvus      |        |    Redis      |
              | vector search |        | online state  |
              +---------------+        +---------------+

Additional deployment / ops assets:
- Kubernetes manifests and overlays
- Terraform for AWS infrastructure
- Prometheus / Jaeger / Grafana-related assets
```

---

## Repository Structure

```text
gateway-service/        Java API gateway, fallback, event publishing, cache-aware enrichment
inference-service/      Ray Serve inference stack, retrieval/reranking/popularity/personalization
event-consumer/         Kafka consumer, retry/DLQ handling, Redis atomic updates
data-pipeline/          ETL/bootstrap/load utilities
infrastructure/         Kubernetes manifests + Terraform modules
observability/          Prometheus, alerting, Grafana-related assets
tests/                  Cross-service and infrastructure contract tests
docker-compose.yml      Local multi-service runtime stack
```

### Suggested starting points

- `gateway-service/.../RecommendationService.java`
- `gateway-service/.../EventTrackingService.java`
- `inference-service/src/deployments/ingress.py`
- `inference-service/src/personalization/behavior_boost.py`
- `event-consumer/src/consumer.py`

---

## Request Lifecycle

### Text recommendation request

1. **Gateway entry**
   - A recommendation/search request arrives at the gateway.
   - Request-shape validation happens at the API boundary.
   - The gateway uses async response handling to avoid tying up request threads longer than necessary.

2. **Cache-aware handling**
   - The gateway consults its cache layers where relevant.
   - Cache policy is used for response enrichment and fallback-oriented reads rather than replacing the main inference path.

3. **Inference call**
   - The gateway calls the inference service over HTTP using a bounded timeout stack.
   - Circuit-breaker logic protects the hot path from inference instability.

4. **Inference orchestration**
   - The inference ingress coordinates embedding, retrieval, reranking, and optional behavior-based score adjustment.
   - Personalization is applied from a snapshot-style Redis read path rather than per-item fan-out.

5. **Response assembly**
   - Returned item identifiers are enriched with metadata.
   - The gateway returns a structured API response.
   - If the main inference path is unavailable, the system may return a bounded fallback response instead of failing open.

### Fallback / degraded behavior

The implementation contains explicit degraded-mode handling rather than silently masking failures. Examples include:

- gateway-side fallback to popularity-oriented results
- inference-side continuation without personalization if behavior features cannot be loaded
- traceable degradation reasons in logs / metrics / contract-level fields where implemented

---

## Event Pipeline / Online Feature Flow

The repository implements an asynchronous behavior-update path for click events.

### Flow

1. A click-tracking request is accepted by the gateway.
2. The gateway publishes the event to Kafka with acknowledgment-oriented producer settings.
3. The event consumer reads from Kafka and applies processing rules.
4. On success, Redis online features are updated.
5. On retryable failure, the message can be routed through retry tiers.
6. If retry is exhausted, the event can be routed to a DLQ.

### Redis feature semantics

The consumer code uses Redis update logic intended to preserve atomicity for related feature writes. The codebase also contains safeguards and tests around Redis behavior and incompatibility assumptions that matter to the chosen update strategy.

### Retry / DLQ behavior

The consumer implementation includes explicit retry / DLQ handling rather than treating all failures identically. This is meant to preserve correctness and operational clarity under message-processing failures.

---

## Technology Stack

### Backend / API
- Java 21
- Spring Boot
- Reactor WebClient
- Resilience4j

### Inference / Serving
- Python
- Ray Serve
- FastAPI
- Hugging Face model loading / inference components

### Messaging
- Kafka

### Storage / Cache / Retrieval
- Redis
- Milvus
- S3/MinIO-style object storage utilities in the data/bootstrap path

### Infrastructure / Deployment
- Docker Compose
- Kubernetes / Kustomize
- Terraform
- AWS-oriented deployment modules (EKS, ElastiCache, ECR, S3, IAM/VPC)

### Observability
- Prometheus
- Jaeger / OpenTelemetry-related wiring
- Grafana-related provisioning assets

### Testing
- Java unit / controller / service tests
- Python unit / integration-style tests
- infrastructure contract tests

---

## Core Design Decisions

### 1. Java gateway + Python inference split

The repository intentionally separates API/runtime boundary concerns from ML-serving concerns:

- **Gateway** handles HTTP contracts, cache-aware response assembly, fallback, and event publication.
- **Inference** handles embedding, retrieval, reranking, and behavior-aware ranking logic.

This split keeps the API service independent from the model-serving runtime and matches the structure visible in the codebase.

### 2. HTTP is the real serving-path contract in the current snapshot

The current hot path is gateway → inference over HTTP, not a pure gRPC serving path. Any older planning material should be treated as historical context rather than the current implementation contract.

### 3. Personalization uses Redis-backed online state

Behavior-aware ranking is driven by online signals read from Redis. The design favors snapshot-style loading during a request over repeated per-item remote lookups.

### 4. Kafka is used for asynchronous behavior ingestion

Click tracking is not coupled directly into the synchronous recommendation response path. Kafka decouples event ingestion from online feature materialization.

### 5. Degradation is explicit rather than silent

The codebase contains explicit fallback and degradation handling at multiple layers. This improves runtime clarity compared with systems that silently substitute partial results without signaling why.

### 6. Infrastructure is expressed as both local and cloud deployment intent

The repository supports local multi-service composition and also includes Kubernetes/Terraform artifacts for more production-like deployment structure. Some of those cloud-oriented assets are deployment intent and structure rather than proof of a specific live environment.

---

## Production / Runtime Characteristics

The current repository demonstrates several production-oriented runtime concerns:

- bounded timeout layering on the serving path
- circuit-breaker protection around inference calls
- fallback behavior when inference is unavailable
- Kafka retry / DLQ handling in the event pipeline
- atomic or contract-sensitive Redis update behavior
- readiness / liveness / startup probe configuration in Kubernetes manifests
- explicit scaling / resource-management intent in deployment assets
- metrics and tracing hooks across the main services

The exact numeric budgets and environment-specific defaults should be taken from the code/config files, not from this README.

---

## Local Development / How to Run

### Prerequisites

Exact setup depends on how much of the stack you want to run locally, but the repository clearly expects a combination of:

- Java toolchain for `gateway-service`
- Python environment for inference / consumer / data tools
- Docker / Docker Compose
- Redis
- Kafka
- Milvus
- optionally Kubernetes tools for local cluster-style deployment

### Typical local workflow

1. Start supporting services with Docker Compose.
2. Run the data/bootstrap step to populate required Redis / Milvus data.
3. Start or verify the gateway service.
4. Start or verify the inference service.
5. Start or verify the event consumer.
6. Send a recommendation request through the gateway.
7. Send a click event and verify the online feature path updates.

### Notes

- Use repository configs and service-specific README/comments to fill in exact environment variables and startup commands.
- Model-related components may require a warm startup and sufficient memory.
- Some optional features may be disabled by default depending on environment flags.

---

## Deployment

### Kubernetes

The repository includes:

- base manifests for the main services
- overlays for local/minikube-style deployment
- overlays for EKS-oriented deployment
- service-level probe/resource/scaling configuration

### Terraform

The Terraform modules indicate deployment intent for:

- EKS
- VPC / subnets / IAM
- ElastiCache Redis
- ECR
- S3
- related AWS plumbing

### What this means in practice

The repository is structured for containerized deployment beyond local development, but specific production readiness still depends on environment configuration, secrets, infrastructure ownership, and validation in a live environment.

---

## Observability

The observability assets in the repository indicate support for:

- Prometheus scraping/configuration
- alert rules for event-consumer-related health
- Jaeger / OpenTelemetry-style tracing
- Grafana-related provisioning/assets

### Practical debugging entrypoints

When something goes wrong, start here:

- **Gateway logs** for request fallback / event publication issues
- **Inference logs** for retrieval, reranking, readiness, or model-load problems
- **Event consumer logs** for retry/DLQ behavior and Redis update failures
- **Prometheus / Jaeger / Grafana assets** for metrics and trace-level debugging
- **Kubernetes probe behavior** if containers restart or never become ready

---

## Testing

The repository includes multiple test layers.

### Broad test categories

- gateway validation and service behavior
- inference logic and degradation handling
- personalization / behavior boost logic
- Redis contract and atomicity checks
- event-consumer retry / metrics / update behavior
- infrastructure/runtime contract checks
- integration-style scenarios across services

### Why that matters

The tests are one of the strongest signals in the repository that important runtime semantics are treated as contracts rather than informal expectations.

### Running tests

Use the service-specific test runners already present in the repository, for example:

- Java tests under `gateway-service`
- Python tests under `inference-service`, `event-consumer`, and `data-pipeline`
- top-level integration / infrastructure tests where present

---

## Known Limitations / Current Gaps

This repository is strong, but it is not pretending to be magically complete.

### Current limitations to keep in mind

- Some components are more fully integrated than others; presence in the repo does not always imply equal production maturity.
- Vision / multimodal-related code exists, but should be treated cautiously unless validated in the exact environment you care about.
- Generation-related functionality exists in the codebase but may be optional or disabled depending on runtime configuration.
- Cloud deployment assets express strong deployment intent, but a README cannot substitute for environment-by-environment deployment validation.
- Operational choices around Redis, Kafka topology, and model startup cost still matter heavily in real deployment conditions.
- Authentication / public-exposure hardening is not the focus of the current repository snapshot.

---

## Future Improvements

Reasonable next steps, grounded in the current repository direction, include:

- stronger offline evaluation/reporting alongside online serving
- more explicit experiment / A/B framework support
- broader end-to-end validation for vision/multimodal paths
- tighter auth / edge hardening for external exposure
- additional operational automation around deployment, scaling, and incident diagnosis

---

## Engineering Highlights

This project is useful because it demonstrates more than “model inference in a notebook.”

### Notable engineering themes in the codebase

- separating synchronous serving from asynchronous behavior ingestion
- keeping latency-sensitive request handling bounded
- using explicit degradation paths instead of silent failure masking
- combining retrieval and reranking rather than relying on a single stage
- feeding recent user behavior back into ranking through an online update loop
- treating Redis update semantics and cross-service contracts as testable concerns
- carrying observability through multiple services instead of bolting it on at the end

### What this project demonstrates

- service boundary design across Java and Python
- production-oriented request/failure handling
- event-driven feature materialization
- vector retrieval system integration
- deployment-aware engineering beyond local model demos

---

## Quick Start for Reviewers

If you only have a few minutes, start here:

### Read first
1. `inference-service/src/deployments/ingress.py`
2. `gateway-service/.../RecommendationService.java`
3. `event-consumer/src/consumer.py`

### Then inspect
- personalization logic under `inference-service/src/personalization/`
- gateway event publishing and fallback behavior
- Kubernetes overlays and Terraform modules
- tests covering degradation, Redis contracts, and consumer semantics

### Run first
- bring up the local stack
- initialize runtime data
- send one recommendation request
- send one click event
- verify that the event path and recommendation path are both alive

---

## License

No license specified in this repository.
