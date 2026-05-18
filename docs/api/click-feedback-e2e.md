# Click Feedback Loop E2E Validation

This document describes the integration test asset that exercises the online click path when run against a live stack. It should be read as executable validation scope, not as a standing proof that every deployment environment already exhibits the same result.

## Current Phase Status

### Implemented

- Integration test asset at `tests/integration/test_click_feedback_loop.py`
- Baseline search capture for a dedicated user
- Click ingestion through the gateway click API
- Redis-side evidence checks for consumer processing
- Polling for measurable ranking shift after clicks

### Partial

- The repository does not include checked-in execution reports for this test
- Results depend on a live stack with functioning Gateway, Kafka, event-consumer, Redis, and inference-service
- The test demonstrates that the loop can be validated end to end, but it is not by itself a permanent production proof artifact

### Next Steps

- Run and archive this test in the target environment if reviewer-facing evidence is needed
- Add environment-specific reports if the team wants repeatable proof beyond the test asset itself

## What The Test Checks

When executed successfully against a live stack, the test checks the following path:

1. `GET /api/recommendation/search` captures baseline ranking for a user.
2. `POST /api/events/click` emits biased click events through Gateway to Kafka.
3. Event consumer processes Kafka and updates Redis online features.
4. The same search call is retried until ranking shifts in a measurable direction.

## Test File

- `tests/integration/test_click_feedback_loop.py`

## Local Run

Prerequisites:

- Full local stack running (Gateway, Kafka, event-consumer, Redis, inference-service)
- Example: `docker-compose up -d`

Run:

```bash
pytest tests/integration/test_click_feedback_loop.py -m integration -v
```

Optional tuning via environment variables:

- `GATEWAY_URL` (default `http://localhost:8080`)
- `REDIS_HOST` (default `localhost`)
- `REDIS_PORT` (default `6379`)
- `E2E_CLICK_FEEDBACK_QUERY` (default `dress`)
- `E2E_CLICK_FEEDBACK_K` (default `20`)
- `E2E_CLICK_FEEDBACK_CLICKS` (default `6`)
- `E2E_CLICK_FEEDBACK_REDIS_TIMEOUT_SEC` (default `20`)
- `E2E_CLICK_FEEDBACK_RANKING_TIMEOUT_SEC` (default `25`)
- `E2E_CLICK_FEEDBACK_POLL_INTERVAL_SEC` (default `0.8`)
- `E2E_CLICK_FEEDBACK_USER` (default `feedback-loop`)

## Assertion Strategy

The test avoids brittle exact-order checks. It requires:

- Consumer processing evidence in Redis for the user and clicked category.
- Ranking order change in top-10 for the same query and user.
- At least one explainable directional signal:
  - clicked item rank improves, or
  - clicked-category concentration in top-5 increases.

## Boundaries

- This asset validates the click-to-feature-to-ranking path only when the full stack is running.
- It does not prove impression handling, CTR features, or Feast integration.
- It does not replace environment-specific operational evidence such as archived CI runs or staged reports.
