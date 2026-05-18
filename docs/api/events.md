# Event Contracts

Last updated: 2026-03-11

This document describes the event pipeline that is present in the repository today. It distinguishes between implemented behavior, partial validation, and planned follow-up work.

## Current Phase Status

### Implemented

- Gateway click-ingestion endpoint at `POST /api/events/click`
- Kafka-based click path: Gateway -> Strimzi Kafka -> event-consumer -> Redis -> inference-service
- Production Kafka topic manifests for:
  - `scalestyle.clicks`
  - `scalestyle.clicks.retry.1s`
  - `scalestyle.clicks.retry.10s`
  - `scalestyle.clicks.retry.60s`
  - `scalestyle.clicks.dlq`
- Event-consumer manual offset commit and Redis-backed dedupe for idempotent feature application
- Retry tiers with enforced retry delay and DLQ routing
- Redis online feature materialization used by inference-side personalization and popularity boosts
- Prometheus scrape config and event-consumer alert rules in the repository

### Partial

- Click-to-ranking-change validation exists as an integration test asset, but the repository does not include checked-in execution evidence for every deployment environment
- Chaos validation exists for bounded pod-deletion scenarios, but Kafka lag verification is still an explicit operator check rather than a fully automated assertion
- Event-consumer autoscaling is implemented only as resource-based HPA for the primary consumer, not as lag-driven autoscaling

### Next Steps

- Add replay and triage tooling for retry and DLQ topics
- Add impression events only if CTR-based ranking is required
- Add a lag-to-autoscaler bridge if Kafka lag should drive scale decisions
- Add schema-evolution tooling only if JSON contracts become insufficient

## Implemented Topology

The implemented click path is:

```text
Client -> Gateway -> Kafka -> event-consumer -> Redis -> inference-service
```

Production Kafka is Strimzi on EKS, not Amazon MSK. The repository contains Strimzi `KafkaTopic` manifests under `infrastructure/k8s/overlays/eks/kafka/` and does not contain `aws_msk_*` Terraform resources.

## Topics

### Implemented Topics

#### `scalestyle.clicks`

- Purpose: primary click-ingestion topic
- Producer: Gateway service
- Consumer: `event-consumer-primary`
- Production manifest: `infrastructure/k8s/overlays/eks/kafka/topic-clicks.yaml`
- Production settings from manifest:
  - partitions: `12`
  - replicas: `3`
  - `min.insync.replicas: 2`
  - retention: `7d`

#### Retry topics

- `scalestyle.clicks.retry.1s`
- `scalestyle.clicks.retry.10s`
- `scalestyle.clicks.retry.60s`

These are implemented in production manifests and are used by the retry topology in `event-consumer/src/consumer.py`.

#### `scalestyle.clicks.dlq`

- Purpose: terminal sink for poison messages or max-retry exhaustion
- Semantics: at-least-once delivery to DLQ with advisory Redis marker for duplicate-noise reduction
- Production manifest: `infrastructure/k8s/overlays/eks/kafka/topic-clicks-dlq.yaml`

### Not Implemented

#### `scalestyle.impressions`

Impression events are not implemented in the current repository snapshot.

- No production `KafkaTopic` manifest for impressions is present
- No gateway impression publish path is evidenced here
- No consumer-side CTR materialization path is present

Because of that, impression-driven CTR features should be treated as future work, not current behavior.

## Click Event Contract

The implemented click payload is JSON-based.

Representative fields used by the current code path:

```json
{
  "event_type": "click",
  "event_id": "uuid-v4",
  "user_id": "string",
  "item_id": "string",
  "timestamp": "ISO 8601 UTC",
  "session_id": "string",
  "source": "search | browse | recommendation | image_search",
  "query": "string (optional)",
  "image_hash": "string (optional)",
  "position": "int (optional)",
  "device": "string (optional)"
}
```

Current validation is application-level in the gateway and event-consumer. The repository does not currently ship Avro schemas, a Schema Registry integration, or versioned schema enforcement tooling.

## Delivery Semantics

### Implemented Semantics

- Gateway waits for broker acknowledgement before returning success for click ingestion
- Event-consumer uses manual offset commit
- Offsets are committed only after the downstream success boundary is reached
- Duplicate feature application is prevented with Redis dedupe keyed by `event_id`
- Retry routing is tiered: main topic -> `retry.1s` -> `retry.10s` -> `retry.60s` -> DLQ
- Retry delay enforcement is implemented in the retry consumer, with an explicit unsafe local override for development only

### Practical Meaning

The repository implements at-least-once transport with effectively-once Redis feature materialization for the normal click path. It does not implement distributed exactly-once semantics across Kafka and Redis.

## Redis Materialization

The event-consumer updates online state used by inference. The implemented feature set includes:

- `user:{user_id}:recent_clicks`
- `user:{user_id}:last_activity`
- `user:{user_id}:category_affinity`
- session click history
- item click signals
- rolling popularity signals and materialized popularity buckets
- dedupe keys for processed events

The inference service reads request-scoped personalization snapshots rather than doing unbounded per-feature Redis fan-out in the hot path.

## Observability

### Implemented Assets

- Prometheus scrape config in `observability/prometheus.yml`
- Event-consumer alert rules in `observability/event-consumer-alerts.yaml`
- In-process event-consumer metrics and health endpoints
- OpenTelemetry / Jaeger configuration in the Kubernetes configmap and manifests

### Partial / External Dependencies

- The repository references lag-based alerts and operator queries, but not every cluster-side exporter or adapter needed to expose consumer-group lag everywhere
- The repository does not include a lag-to-HPA adapter such as KEDA or `prometheus-adapter`

## Non-Claims

The current repository snapshot should not be described as implementing any of the following:

- impression-event ingestion
- CTR-based ranking
- Feast integration
- MSK-based Kafka deployment
- Schema-Registry-backed contract enforcement
- lag-driven autoscaling
- universal proof that every environment shows immediate click-to-next-result ranking change

## Evidence in Repo

- Gateway click path and tests: `gateway-service`
- Event-consumer implementation: `event-consumer/src/consumer.py`
- Production Kafka topic manifests: `infrastructure/k8s/overlays/eks/kafka/`
- Feedback-loop integration asset: `tests/integration/test_click_feedback_loop.py`
- Chaos validation asset: `tests/integration/chaos_test.sh`
