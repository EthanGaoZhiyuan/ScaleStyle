# Chaos Self-Healing Validation

This runbook turns the existing integration assets into a bounded validation flow for two pod-deletion recovery scenarios. It is not a general chaos engineering platform.

## Current Phase Status

### Implemented

- Scripted pod-deletion scenarios in `tests/integration/chaos_test.sh`
- Inference-serving continuity check during one inference pod deletion
- Consumer recovery check during one primary event-consumer pod deletion
- Post-recovery click-feedback probe after consumer disruption
- Markdown result artifact generation in `tests/_artifacts/`

### Partial

- Kafka lag verification is still operator-driven through Prometheus or Grafana
- The scenarios cover bounded deployment recovery, not broad failure injection across brokers, Redis, or infrastructure dependencies
- The feedback-loop portion is validated through the same live-stack assumptions as the dedicated click-feedback integration asset

### Next Steps

- Add environment-specific archived run results if reviewer-facing proof is needed
- Add broader failure scenarios only when the repository also ships the supporting verification hooks

This runbook currently covers two scenarios:

1. Delete one inference pod while search traffic is live and confirm the system keeps serving and the deployment self-heals.
2. Delete one primary event-consumer pod while click traffic is live and confirm the deployment recovers and the click-feedback loop resumes changing personalized search results.

## Scope

The automation in `tests/integration/chaos_test.sh` is intentionally bounded to checks the repository can verify directly:

- Kubernetes deployment replacement and available replica recovery
- continued gateway request handling during disruption
- continued broker-acknowledged click ingestion during consumer disruption
- post-recovery personalization catch-up through the click -> Kafka -> consumer -> Redis -> inference path

The script does not claim full automation of Kafka lag inspection. The repository exposes lag-related metrics and alert rules, but it does not ship a dedicated broker-admin helper or an always-available Prometheus query endpoint for shell-side assertions in every environment. Lag rise and catch-up therefore remain explicit operator verification steps.

## Prerequisites

- `kubectl`, `curl`, `jq`, and `python3` are installed locally.
- `tests/integration/common.sh` is configured for the active environment.
- `GATEWAY_URL` points at a live gateway endpoint.
- The target namespace is reachable through `kubectl`.
- `inference` has at least 2 replicas for scenario `inference`.
- `event-consumer-primary` has at least 2 replicas for scenario `consumer`.

## Run

Run both scenarios:

```bash
tests/integration/chaos_test.sh
```

Run only the inference scenario:

```bash
CHAOS_SCENARIO=inference tests/integration/chaos_test.sh
```

Run only the consumer scenario:

```bash
CHAOS_SCENARIO=consumer tests/integration/chaos_test.sh
```

Useful environment overrides:

```bash
NAMESPACE=scalestyle
GATEWAY_URL=http://localhost:8080
INFERENCE_RECOVERY_TIMEOUT_SEC=180
CONSUMER_RECOVERY_TIMEOUT_SEC=180
CONSUMER_PROBE_TIMEOUT_SEC=90
```

The script writes a markdown report into `tests/_artifacts/` with the exact timestamped filename printed on completion.

## Scenario H1

`CHAOS_SCENARIO=inference` performs the following:

1. Starts bounded concurrent search traffic through the gateway.
2. Deletes one pod from the `inference` deployment.
3. Waits for the deployment to return to its desired available replica count.
4. Analyzes request success, degraded responses, and post-delete error rate.

Pass conditions:

- post-delete search traffic still succeeds within the configured error-rate budget
- the `inference` deployment recovers to its desired available replicas within the timeout

Operator-visible signals:

- Grafana dashboard `ScaleStyle - Resilience`: degradation rate and Ray failure/fallback traffic may spike briefly
- Grafana dashboard `ScaleStyle - Recommendation Service Overview`: fallback rate may rise
- Kubernetes deployment status should return to steady-state available replicas

## Scenario H2

`CHAOS_SCENARIO=consumer` performs the following:

1. Starts bounded concurrent click traffic through the gateway click API.
2. Deletes one pod from the `event-consumer-primary` deployment.
3. Waits for the deployment to return to its desired available replica count.
4. Verifies click requests continue to receive broker acknowledgements within the configured error-rate budget.
5. Runs a post-recovery click-feedback probe for a dedicated user and waits for personalized ranking to change.

Pass conditions:

- click ingestion stays within the configured acknowledgement error-rate budget
- `event-consumer-primary` returns to its desired available replica count within the timeout
- the post-recovery feedback probe observes a ranking shift after fresh clicks

Operator-visible signals:

- Prometheus: `sum(kafka_consumer_lag{topic="scalestyle.clicks"})` should rise after deletion and then fall during catch-up
- Prometheus: `sum(rate(events_processed_total{result="applied"}[1m]))` should dip during rebalance and recover afterward
- alert rules in `observability/event-consumer-alerts.yaml` may fire or approach threshold:
  - `KafkaConsumerGroupLagHigh`
  - `KafkaConsumerGroupLagCritical`
  - `EventConsumerRetryLagHigh`

## Manual Lag Verification

Use Prometheus or Grafana to verify the lag curve during scenario H2:

```promql
sum(kafka_consumer_lag{topic="scalestyle.clicks"})
sum(rate(events_processed_total{result="applied"}[1m]))
```

What to look for:

- lag rises soon after pod deletion
- processing rate dips during rebalance
- lag returns toward baseline after the replacement pod is ready and processing resumes

## Failure Interpretation

If H1 fails:

- high post-delete error rate usually points to insufficient inference redundancy or slow pod readiness
- no degraded traffic at all is not necessarily a failure if the deployment recovers and serving continuity is maintained

If H2 fails:

- elevated click acknowledgement errors indicate the disruption is visible too far upstream
- recovery without a later ranking shift usually points to consumer recovery without successful end-to-end feature materialization
- flat lag with no recovery usually requires direct inspection of Kafka, consumer logs, and Redis update paths

## Related Assets

- `tests/integration/chaos_test.sh`
- `tests/integration/common.sh`
- `observability/event-consumer-alerts.yaml`
- `observability/grafana/dashboards/scalestyle-resilience.json`
- `observability/grafana/dashboards/scalestyle-overview.json`
