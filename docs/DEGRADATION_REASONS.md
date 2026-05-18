# Canonical Degradation Reason Vocabulary

Single cross-service contract for degradation/fallback reason strings. Used by
Gateway, Inference, logs, metrics, and API response metadata.

## Canonical Reasons

| Reason | Service(s) | When Used |
|--------|------------|-----------|
| `REDIS_TIMEOUT` | Gateway, Inference | Redis command exceeded socket timeout |
| `REDIS_UNAVAILABLE` | Gateway, Inference | Redis connection error |
| `PERSONALIZATION_UNAVAILABLE` | Gateway, Inference | Personalization/feature load failed |
| `INFERENCE_TIMEOUT` | Gateway, Inference | Inference HTTP timeout or embedding/retrieval timeout |
| `INFERENCE_UNAVAILABLE` | Gateway, Inference | Inference non-timeout failure |
| `CACHE_MISS` | Gateway | Rec-cache lookup miss |
| `STALE_DATA_ALLOWED` | Gateway | Served cached result despite staleness |
| `DOWNSTREAM_CIRCUIT_OPEN` | Gateway, Inference | Circuit breaker open |
| `DOWNSTREAM_CAPACITY_REJECTED` | Gateway | Bulkhead saturated, request rejected |
| `EMPTY_RESULTS_ALLOWED` | Inference | Retrieval returned empty, served popularity fallback |

## Rules

- Use enum names exactly; no ad-hoc strings.
- Metrics label: `reason=<CANONICAL_NAME>` (e.g. `reason=INFERENCE_TIMEOUT`).
- API response: `degradedReason` / `degraded_reason` = canonical string.
- Logs: `degrade_reason=<CANONICAL_NAME>`.
