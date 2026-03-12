"""Tests for MetricsServer liveness/readiness endpoint semantics.

Covers:
- /live  returns 200 when loop is alive and poll is fresh
- /live  returns 503 when the consumer loop is dead
- /live  returns 503 when poll is stale beyond the liveness threshold (300 s)
- /live  returns 200 during startup (poll_freshness pending) — must not cause false restart
- /ready returns 200 when all dependency checks pass
- /ready returns 503 when a dependency check (e.g. Kafka assignment) fails
- /health returns same HTTP semantics as /ready (backward compat)
- /metrics returns 200 (Prometheus format, smoke test)
- /unknown returns 404
"""

import json
import threading
import time
import urllib.request
from http.client import HTTPResponse
from unittest.mock import patch

import pytest

# Suppress prometheus registry duplicate-metric errors across test runs
from prometheus_client import REGISTRY

# ──────────────────────────────────────────────────────────────────────────────
# Helpers
# ──────────────────────────────────────────────────────────────────────────────

_USED_PORTS: set[int] = set()
_PORT_LOCK = threading.Lock()


def _allocate_port(start: int = 19100) -> int:
    """Pick a free port from a range that avoids prometheus-client's default 8000."""
    with _PORT_LOCK:
        port = start
        while port in _USED_PORTS:
            port += 1
        _USED_PORTS.add(port)
        return port


def _get(port: int, path: str) -> tuple[int, dict | str]:
    """Perform a GET request and return (status_code, parsed_body)."""
    url = f"http://127.0.0.1:{port}{path}"
    try:
        with urllib.request.urlopen(url, timeout=3) as resp:  # type: HTTPResponse
            raw = resp.read()
            ct = resp.headers.get("Content-Type", "")
            if "application/json" in ct:
                return resp.status, json.loads(raw)
            return resp.status, raw.decode()
    except urllib.error.HTTPError as exc:
        raw = exc.read()
        ct = exc.headers.get("Content-Type", "")
        if "application/json" in ct:
            return exc.code, json.loads(raw)
        return exc.code, raw.decode()


# ──────────────────────────────────────────────────────────────────────────────
# Fixture
# ──────────────────────────────────────────────────────────────────────────────

@pytest.fixture()
def server_factory():
    """
    Factory that creates and starts a MetricsServer on a fresh port for each
    test, injects the supplied liveness/readiness callables, and tears down
    the server after the test.
    """
    servers = []

    def _make(liveness_fn=None, readiness_fn=None):
        # Import here so prometheus_client gauges are already registered
        # (avoids duplicate-registration errors when the module is imported
        # at collection time in a clean registry).
        import sys
        # Ensure we load the src version, not a stale cached one
        sys.path.insert(0, "src")
        from metrics import MetricsServer  # noqa: PLC0415

        port = _allocate_port()
        srv = MetricsServer(port=port)
        if liveness_fn is not None:
            srv.set_liveness_check(liveness_fn)
        if readiness_fn is not None:
            srv.set_readiness_check(readiness_fn)
        srv.start()
        # Give the background thread a moment to bind the socket
        time.sleep(0.05)
        servers.append(srv)
        return srv, port

    yield _make

    for s in servers:
        try:
            s.stop()
        except Exception:
            pass


# ──────────────────────────────────────────────────────────────────────────────
# Liveness tests
# ──────────────────────────────────────────────────────────────────────────────

class TestLivenessEndpoint:

    def test_live_returns_200_when_loop_alive_and_poll_fresh(self, server_factory):
        """Happy-path liveness: loop alive, poll happened 1 s ago."""
        def liveness():
            return {
                "live": True,
                "checks": {
                    "consumer_loop": {"status": "ok"},
                    "poll_freshness": {"status": "ok", "last_poll_seconds_ago": 1.0},
                },
            }

        _, port = server_factory(liveness_fn=liveness)
        status, body = _get(port, "/live")

        assert status == 200
        assert body["live"] is True
        assert body["checks"]["consumer_loop"]["status"] == "ok"
        assert body["checks"]["poll_freshness"]["status"] == "ok"

    def test_live_returns_503_when_loop_dead(self, server_factory):
        """loop_alive=False must cause 503 — the process needs a restart."""
        def liveness():
            return {
                "live": False,
                "checks": {
                    "consumer_loop": {"status": "dead"},
                    "poll_freshness": {"status": "ok", "last_poll_seconds_ago": 5.0},
                },
            }

        _, port = server_factory(liveness_fn=liveness)
        status, body = _get(port, "/live")

        assert status == 503
        assert body["live"] is False
        assert body["checks"]["consumer_loop"]["status"] == "dead"

    def test_live_returns_503_when_poll_stale_beyond_liveness_threshold(self, server_factory):
        """Poll stale >300 s must cause 503 (genuine process hang)."""
        def liveness():
            return {
                "live": False,
                "checks": {
                    "consumer_loop": {"status": "ok"},
                    "poll_freshness": {
                        "status": "stale",
                        "last_poll_seconds_ago": 310.0,
                        "threshold_s": 300.0,
                    },
                },
            }

        _, port = server_factory(liveness_fn=liveness)
        status, body = _get(port, "/live")

        assert status == 503
        assert body["live"] is False
        assert body["checks"]["poll_freshness"]["status"] == "stale"

    def test_live_returns_200_during_startup_before_first_poll(self, server_factory):
        """poll_freshness=pending during startup must NOT fail liveness.

        Before the first consumer.poll() completes, last_poll_ts is None.
        Kubernetes must not restart the pod simply because it hasn't polled yet.
        """
        def liveness():
            return {
                "live": True,
                "checks": {
                    "consumer_loop": {"status": "ok"},
                    "poll_freshness": {"status": "pending"},
                },
            }

        _, port = server_factory(liveness_fn=liveness)
        status, body = _get(port, "/live")

        assert status == 200
        assert body["live"] is True
        assert body["checks"]["poll_freshness"]["status"] == "pending"

    def test_live_returns_200_with_default_when_no_fn_injected(self, server_factory):
        """If no liveness fn is set, /live must still return 200 (safe default)."""
        _, port = server_factory()  # no liveness_fn
        status, body = _get(port, "/live")

        assert status == 200
        assert body["live"] is True


# ──────────────────────────────────────────────────────────────────────────────
# Readiness tests
# ──────────────────────────────────────────────────────────────────────────────

class TestReadinessEndpoint:

    def test_ready_returns_200_when_all_checks_pass(self, server_factory):
        """Happy-path readiness: all dependencies healthy."""
        def readiness():
            return {
                "healthy": True,
                "checks": {
                    "consumer_loop": {"status": "ok"},
                    "poll_freshness": {"status": "ok", "last_poll_seconds_ago": 2.0},
                    "kafka": {"status": "ok", "assigned_partitions": 3},
                    "redis": {"status": "ok", "latency_ms": 1.2},
                    "paused_partitions": {"count": 0},
                },
            }

        _, port = server_factory(readiness_fn=readiness)
        status, body = _get(port, "/ready")

        assert status == 200
        assert body["healthy"] is True
        assert body["checks"]["kafka"]["assigned_partitions"] == 3

    def test_ready_returns_503_when_kafka_assignment_missing(self, server_factory):
        """Primary consumer with no Kafka assignment must be NotReady.

        This should NOT trigger a restart — the pod just exits the rolling-update
        readiness gate until partitions are re-assigned after the rebalance.
        """
        def readiness():
            return {
                "healthy": False,
                "checks": {
                    "consumer_loop": {"status": "ok"},
                    "poll_freshness": {"status": "ok", "last_poll_seconds_ago": 3.0},
                    "kafka": {
                        "status": "no_assignment",
                        "assigned_partitions": 0,
                        "detail": "primary consumer lost partition assignment",
                    },
                    "redis": {"status": "ok", "latency_ms": 0.9},
                    "paused_partitions": {"count": 0},
                },
            }

        _, port = server_factory(readiness_fn=readiness)
        status, body = _get(port, "/ready")

        assert status == 503
        assert body["healthy"] is False
        assert body["checks"]["kafka"]["status"] == "no_assignment"

    def test_ready_returns_200_when_redis_fails_informational_only(self, server_factory):
        """Redis failure is informational and must not flip the pod to NotReady.

        The consumer handles per-message Redis errors via its own retry/DLQ
        pipeline and must remain ready to process (and route to DLQ) even when
        Redis is temporarily unreachable.
        """
        def readiness():
            return {
                "healthy": True,
                "checks": {
                    "consumer_loop": {"status": "ok"},
                    "poll_freshness": {"status": "ok", "last_poll_seconds_ago": 1.0},
                    "kafka": {"status": "ok", "assigned_partitions": 3},
                    "redis": {"status": "error", "detail": "ConnectionError"},
                    "paused_partitions": {"count": 0},
                },
            }

        _, port = server_factory(readiness_fn=readiness)
        status, body = _get(port, "/ready")

        assert status == 200
        assert body["healthy"] is True
        assert body["checks"]["redis"]["status"] == "error"

    def test_ready_returns_200_with_default_when_no_fn_injected(self, server_factory):
        """If no readiness fn is set, /ready must still return 200 (safe default)."""
        _, port = server_factory()  # no readiness_fn
        status, body = _get(port, "/ready")

        assert status == 200
        assert body["healthy"] is True


# ──────────────────────────────────────────────────────────────────────────────
# Backward-compat /health
# ──────────────────────────────────────────────────────────────────────────────

class TestHealthBackwardCompat:

    def test_health_mirrors_ready_200(self, server_factory):
        """/health returns 200 when readiness check passes."""
        def readiness():
            return {
                "healthy": True,
                "checks": {"kafka": {"status": "ok", "assigned_partitions": 2}},
            }

        _, port = server_factory(readiness_fn=readiness)
        status_ready, _ = _get(port, "/ready")
        status_health, _ = _get(port, "/health")

        assert status_ready == 200
        assert status_health == 200

    def test_health_mirrors_ready_503(self, server_factory):
        """/health returns 503 when readiness check fails."""
        def readiness():
            return {
                "healthy": False,
                "checks": {"kafka": {"status": "no_assignment", "assigned_partitions": 0}},
            }

        _, port = server_factory(readiness_fn=readiness)
        status_ready, _ = _get(port, "/ready")
        status_health, _ = _get(port, "/health")

        assert status_ready == 503
        assert status_health == 503

    def test_set_health_check_is_alias_for_set_readiness_check(self, server_factory):
        """set_health_check() must be a backward-compat alias for set_readiness_check()."""
        import sys
        sys.path.insert(0, "src")
        from metrics import MetricsServer  # noqa: PLC0415

        port = _allocate_port()
        srv = MetricsServer(port=port)

        def readiness():
            return {"healthy": False, "checks": {}}

        srv.set_health_check(readiness)  # legacy API
        srv.start()
        time.sleep(0.05)

        try:
            status, body = _get(port, "/health")
            assert status == 503
            assert body["healthy"] is False

            status2, body2 = _get(port, "/ready")
            assert status2 == 503
        finally:
            srv.stop()


# ──────────────────────────────────────────────────────────────────────────────
# Other endpoints
# ──────────────────────────────────────────────────────────────────────────────

class TestOtherEndpoints:

    def test_metrics_returns_200(self, server_factory):
        """/metrics must remain functional (Prometheus scrape target)."""
        _, port = server_factory()
        status, body = _get(port, "/metrics")

        assert status == 200
        # Prometheus exposition format always starts with "# HELP" or "# TYPE"
        assert "# HELP" in body or "# TYPE" in body or "process_" in body

    def test_unknown_path_returns_404(self, server_factory):
        """Unknown paths must return 404, not 200 or a probe-like response."""
        _, port = server_factory()
        status, _ = _get(port, "/notapath")

        assert status == 404

    def test_liveness_and_readiness_are_independent(self, server_factory):
        """Liveness 200 with readiness 503 must be possible simultaneously.

        This is the critical Kafka-rebalance scenario: the process is alive
        (liveness OK) but has lost partition assignment (readiness NotReady).
        Kubernetes must NOT restart the pod in this case.
        """
        def liveness():
            return {
                "live": True,
                "checks": {
                    "consumer_loop": {"status": "ok"},
                    "poll_freshness": {"status": "ok", "last_poll_seconds_ago": 1.0},
                },
            }

        def readiness():
            return {
                "healthy": False,
                "checks": {
                    "kafka": {
                        "status": "no_assignment",
                        "assigned_partitions": 0,
                        "detail": "rebalance in progress",
                    }
                },
            }

        _, port = server_factory(liveness_fn=liveness, readiness_fn=readiness)
        live_status, live_body = _get(port, "/live")
        ready_status, ready_body = _get(port, "/ready")

        # Live = 200 (process is fine, must NOT be restarted)
        assert live_status == 200
        assert live_body["live"] is True

        # Ready = 503 (not ready to process, will be excluded from readiness gates)
        assert ready_status == 503
        assert ready_body["healthy"] is False
