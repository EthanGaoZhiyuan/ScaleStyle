"""
Health and liveness check logic for the event consumer.

Extracted from EventConsumer._liveness_check and EventConsumer._health_check
to allow independent testing without constructing a full EventConsumer.

All thresholds, check names, and response shapes are preserved exactly from
the original consumer.py implementation so that Kubernetes probes behave
identically.
"""

from __future__ import annotations

import logging
import time
from typing import Any, Callable, Dict, Optional

import metrics
import config

logger = logging.getLogger(__name__)


class HealthChecker:
    """Provides liveness and readiness check callables for injection into MetricsServer.

    Parameters
    ----------
    get_loop_alive : Callable[[], bool]
    get_last_poll_ts : Callable[[], Optional[float]]
    get_paused_partitions : Callable[[], dict]
    get_start_time : Callable[[], float]
    kafka_consumer :
        Used for partition assignment check and Redis is used via redis_client.
    redis_client :
        Used for Redis ping in the readiness check.
    """

    def __init__(
        self,
        *,
        get_loop_alive: Callable[[], bool],
        get_last_poll_ts: Callable[[], Optional[float]],
        get_paused_partitions: Callable[[], dict],
        get_start_time: Callable[[], float],
        kafka_consumer,
        redis_client,
    ) -> None:
        self._get_loop_alive = get_loop_alive
        self._get_last_poll_ts = get_last_poll_ts
        self._get_paused_partitions = get_paused_partitions
        self._get_start_time = get_start_time
        self._kafka_consumer = kafka_consumer
        self._redis_client = redis_client

    # ------------------------------------------------------------------
    # Liveness check (Kubernetes livenessProbe → /live)
    # ------------------------------------------------------------------

    def liveness_check(self) -> dict:
        """Return liveness status for the Kubernetes livenessProbe (/live).

        Checks ONLY in-process state.  Never calls Kafka, Redis, or any external
        dependency.  A failure here causes Kubernetes to restart the pod, so the
        bar must be high: only genuine process-level failures should return live=False.

        The poll-freshness threshold is deliberately conservative (300 s, matching
        the Kafka default max.poll.interval.ms) so that a prolonged rebalance or
        momentary broker outage does not trigger an unnecessary restart.

        Returns a dict with 'live' (bool) and 'checks' (per-component detail).
        """
        # 300 s: matches Kafka default max.poll.interval.ms.  Using a threshold
        # below this would risk restarting a pod whose loop is alive but blocked
        # inside consumer.poll() during a legitimate group rebalance.
        _LIVENESS_STALE_THRESHOLD_S = 300.0

        checks: Dict[str, Any] = {}
        live = True

        # 1. Consumer loop state — False only on fatal unhandled exception.
        if not self._get_loop_alive():
            checks["consumer_loop"] = {"status": "dead"}
            live = False
        else:
            checks["consumer_loop"] = {"status": "ok"}

        # 2. Poll freshness (conservative threshold).
        # "pending" means the loop has not yet completed its first poll; this is
        # expected during startup and must not fail liveness.
        last_poll_ts = self._get_last_poll_ts()
        if last_poll_ts is not None:
            stale_s = time.time() - last_poll_ts
            if stale_s > _LIVENESS_STALE_THRESHOLD_S:
                checks["poll_freshness"] = {
                    "status": "stale",
                    "last_poll_seconds_ago": round(stale_s, 1),
                    "threshold_s": _LIVENESS_STALE_THRESHOLD_S,
                }
                live = False
            else:
                checks["poll_freshness"] = {
                    "status": "ok",
                    "last_poll_seconds_ago": round(stale_s, 1),
                }
        else:
            checks["poll_freshness"] = {"status": "pending"}

        return {"live": live, "checks": checks}

    # ------------------------------------------------------------------
    # Health / readiness check (Kubernetes readinessProbe → /ready)
    # ------------------------------------------------------------------

    def health_check(self) -> dict:
        """Return readiness status for the Kubernetes readinessProbe (/ready)
        and the backward-compat /health endpoint.

        Checks whether the consumer is ready to process traffic: loop state,
        poll freshness, Kafka partition assignment, and Redis availability.
        Redis failures are informational only — a transient Redis outage marks
        the pod informational-unhealthy but does NOT flip ready=False.

        Returns a dict with 'healthy' (bool) and 'checks' (per-component detail).
        """
        checks: Dict[str, Any] = {}
        healthy = True

        # 1. Consumer loop state
        if not self._get_loop_alive():
            checks["consumer_loop"] = {"status": "dead"}
            healthy = False
        else:
            checks["consumer_loop"] = {"status": "ok"}

        # 2. Poll freshness — catches a hung loop that doesn't raise
        _STALE_THRESHOLD_S = 120.0
        last_poll_ts = self._get_last_poll_ts()
        if last_poll_ts is not None:
            stale_s = time.time() - last_poll_ts
            if stale_s > _STALE_THRESHOLD_S:
                checks["poll_freshness"] = {
                    "status": "stale",
                    "last_poll_seconds_ago": round(stale_s, 1),
                }
                healthy = False
            else:
                checks["poll_freshness"] = {
                    "status": "ok",
                    "last_poll_seconds_ago": round(stale_s, 1),
                }
        else:
            checks["poll_freshness"] = {"status": "pending"}

        # 3. Kafka consumer assignment
        _ASSIGNMENT_GRACE_S = 30.0
        try:
            partitions = self._kafka_consumer.assignment()
            partition_count = len(partitions)
            startup_elapsed = time.time() - self._get_start_time()

            if partition_count == 0 and startup_elapsed > _ASSIGNMENT_GRACE_S:
                if config.CONSUMER_MODE == "primary":
                    checks["kafka"] = {
                        "status": "no_assignment",
                        "assigned_partitions": 0,
                        "startup_elapsed_s": round(startup_elapsed, 1),
                        "detail": "primary consumer lost partition assignment",
                    }
                    healthy = False
                else:
                    checks["kafka"] = {
                        "status": "ok_empty",
                        "assigned_partitions": 0,
                        "note": "retry consumer — empty assignment is acceptable",
                    }
            else:
                checks["kafka"] = {
                    "status": "ok",
                    "assigned_partitions": partition_count,
                }
        except Exception as exc:
            checks["kafka"] = {"status": "error", "detail": type(exc).__name__}
            healthy = False

        # 4. Redis ping (informational — does not flip healthy to False)
        try:
            t0 = time.time()
            self._redis_client.ping()
            metrics.record_redis_success()
            checks["redis"] = {
                "status": "ok",
                "latency_ms": round((time.time() - t0) * 1000, 1),
            }
        except Exception as exc:
            metrics.record_redis_error()
            checks["redis"] = {"status": "error", "detail": type(exc).__name__}
        metrics.refresh_redis_unavailable_duration()

        # 5. Paused partitions count (informational)
        checks["paused_partitions"] = {"count": len(self._get_paused_partitions())}

        return {"healthy": healthy, "checks": checks}
