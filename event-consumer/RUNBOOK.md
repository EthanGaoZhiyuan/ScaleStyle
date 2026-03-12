# Event Consumer Runbook

This runbook covers the production-facing Kafka click-event consumers:

- `event-consumer-primary`: feature updater for `scalestyle.clicks`
- `event-consumer-retry`: delayed retry / DLQ processor

## Current Phase Status

### Implemented

- Primary click-consumer deployment and separate retry consumer deployment
- Resource-based HPA for `event-consumer-primary`
- Prometheus scrape config, alert rules, and consumer metrics endpoints
- Retry-tier and DLQ operational path in the consumer code

### Partial

- Autoscaling is resource-based only; Kafka lag does not directly drive scale
- The retry consumer is not autoscaled by the shipped HPA
- Cluster support for `metrics-server` is assumed, not installed by this repository
- Some lag-oriented alerts depend on metrics that may require extra cluster-side exporter plumbing

### Next Steps

- Add a cluster-supported lag metric source if lag-driven scaling is required
- Add KEDA or `prometheus-adapter` only if custom or external-metric scaling is actually needed

## Autoscaling

The primary consumer is autoscaled by a standard Kubernetes Horizontal Pod Autoscaler:

- HPA: `event-consumer-primary-hpa`
- Target deployment: `event-consumer-primary`
- Metrics:
  - CPU average utilization: `60%`
  - Memory average utilization: `80%`
- Bounds:
  - `minReplicas: 2`
  - `maxReplicas: 8`

Why these metrics were chosen:

- They are the only autoscaling inputs the repository supports end-to-end today.
- The repo has Prometheus scrape for consumer metrics and alert rules for lag/health, but it does **not** include:
  - KEDA
  - `prometheus-adapter`
  - any other custom/external metrics adapter for HPA
- Without one of those adapters, Kubernetes HPA cannot scale from Kafka lag or Prometheus lag gauges.

Why the bounds were chosen:

- `minReplicas: 2` preserves HA and matches the current primary deployment baseline.
- `maxReplicas: 8` stays below the `12` partitions configured for `scalestyle.clicks`, so scale-out remains useful without overshooting the partition ceiling.
- Scale-up is intentionally bounded to avoid rebalance storms.

## Operational Limitations

This is a clean baseline, not true lag-driven autoscaling.

Important limitations:

- Resource utilization is an indirect proxy for backlog. Lag can grow before CPU or memory crosses the HPA target.
- The retry consumer is **not** autoscaled by this HPA. It stays explicitly sized and should be scaled separately if retry lag grows.
- HPA requires Kubernetes `metrics-server` in the cluster. This repo does not install `metrics-server`; the platform must provide it.

Current lag-metric gap for autoscaling:

- The event consumer exports `kafka_consumer_lag{topic,partition}` from the process itself.
- The alert rules also reference `kafka_consumer_group_lag{topic,consumergroup}`, but this repository does not contain the exporter, recording rule, or adapter that would make that metric available to HPA.
- Because of that gap, claiming lag-driven autoscaling here would be inaccurate.

## Observability Signals

Current observability assets already in the repo:

- Prometheus scrape config: `observability/prometheus.yml`
- Alert rules: `observability/event-consumer-alerts.yaml`
- In-process metrics and probe endpoints: `event-consumer/src/metrics.py`

Most useful metrics during incidents:

- `kafka_consumer_lag`
- `processing_rate_events_per_sec`
- `events_processed_total`
- `events_retry_routed_total`
- `events_dlq_sent_total`
- `events_commit_failed_total`
- `consumer_terminations_total`
- `redis_consecutive_errors`
- `redis_unavailable_duration_seconds`
- `consumer_health`

## Intentional Fail-Fast On Commit Uncertainty

The consumer intentionally exits non-zero when a downstream Kafka write is already
broker-acknowledged but the source offset commit fails immediately afterward.

This is expected fail-safe behavior for these cases:

- Retry copy published, then source commit fails
- DLQ write published, then source commit fails
- Batch offset commit fails after at least one retry/DLQ publish in the batch

Why the pod exits instead of continuing:

- Continuing would leave the source offset uncommitted after a durable downstream write.
- The broker can then re-deliver the source message on restart or rebalance.
- Fail-fast keeps that state explicit, lets Kubernetes restart the pod immediately,
  and relies on idempotency/duplicate suppression in retry and DLQ flows.

How to confirm this is an intentional terminal state and not a stuck pod:

1. Check the termination counters:

```promql
sum by (reason) (rate(consumer_terminations_total[5m]))
sum by (reason) (rate(events_commit_failed_total[5m]))
```

Expected `events_commit_failed_total` reasons:

- `retry_routed_terminating`
- `dlq_send_success`
- `batch_commit_failed`

Expected `consumer_terminations_total` reasons:

- `downstream_commit_uncertain`
- `fatal_loop_error`

2. Inspect the pod logs for the terminal record. It is emitted at `CRITICAL` with
   machine-readable fields:

- `reason`
- `downstream_action`
- `topic`
- `partition`
- `offset`
- `event_id`
- `trace_id`
- `retry_count`

3. Correlate with Kubernetes restarts:

```bash
kubectl get pods -n scalestyle -l component=event-consumer
kubectl logs -n scalestyle deploy/event-consumer-primary --since=15m | grep 'downstream write acknowledged but source commit failed'
```

Operator guidance:

- If these signals are isolated, treat them as safe fail-fast recovery.
- If they repeat, investigate Kafka coordinator stability, commit latency, broker availability,
  and any correlated rebalance churn before changing consumer semantics.
- Do not suppress the exit path unless the downstream write/commit contract is redesigned.

## Verify Scale-Out

1. Watch the HPA:

```bash
kubectl get hpa event-consumer-primary-hpa -n scalestyle -w
```

1. Watch the deployment replica count:

```bash
kubectl get deployment event-consumer-primary -n scalestyle -w
```

1. Inspect HPA decisions:

```bash
kubectl describe hpa event-consumer-primary-hpa -n scalestyle
```

1. Confirm pod resource pressure exists:

```bash
kubectl top pods -n scalestyle -l component=event-consumer,consumer-mode=primary
```

1. Correlate with consumer health and lag in Prometheus:

```promql
sum(kafka_consumer_lag{topic="scalestyle.clicks"})
sum(rate(events_processed_total{result="applied"}[5m]))
max(redis_consecutive_errors)
```

## Verify Scale-In

1. Reduce load and keep watching the HPA:

```bash
kubectl get hpa event-consumer-primary-hpa -n scalestyle -w
```

1. Confirm scale-down waits for the stabilization window:

- `scaleDown.stabilizationWindowSeconds: 300`

1. Verify lag and retry traffic stay controlled while replicas fall:

```promql
sum(kafka_consumer_lag{topic="scalestyle.clicks"})
sum(rate(events_retry_routed_total[5m]))
```

## Follow-Up Needed For Lag-Driven Scaling

To move from resource-based HPA to real lag-driven autoscaling, add all of the following:

1. A reliable lag metric source per consumer group.
   Example: Kafka exporter, Burrow-style exporter, or a Prometheus recording rule that produces a stable group-level lag metric.

2. A bridge from that metric into Kubernetes autoscaling.
   Choose one:
   - KEDA with Kafka or Prometheus scaler
   - `prometheus-adapter` exposing custom/external metrics to HPA

3. A clear autoscaling contract.
   Define whether scaling follows:
   - total consumer-group lag
   - lag per partition
   - lag per replica

4. Replica limits aligned with partition count.
   For the primary topic today, the hard useful ceiling is the partition count unless the topology changes.
