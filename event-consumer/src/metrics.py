"""
Metrics Server for Event Consumer

Exposes HTTP endpoint for observability:
- GET /metrics (Prometheus format)
- GET /health

Metrics exposed:
- events_processed_total
- events_failed_total
- events_deduped_total
- consumer_uptime_seconds
- processing_rate (events/sec)
"""

import time
from http.server import BaseHTTPRequestHandler, HTTPServer
from threading import Thread
import logging

logger = logging.getLogger(__name__)


class MetricsCollector:
    """Collects and stores metrics"""

    def __init__(self):
        self.start_time = time.time()
        self.processed = 0
        self.failed = 0
        self.deduped = 0

        # P1.7: Latency tracking
        self.processing_latencies = []  # Last 1000 samples
        self.max_latency_samples = 1000

        # P1.3: Feature-quality metrics
        self.category_lookup_hit = 0
        self.category_lookup_miss = 0
        self.unknown_category_events = 0
        self.lua_materialization_failures = 0

    def incr_processed(self):
        self.processed += 1

    def incr_failed(self):
        self.failed += 1

    def incr_deduped(self):
        self.deduped += 1

    def incr_category_hit(self):
        """Category lookup succeeded (P1.3)"""
        self.category_lookup_hit += 1

    def incr_category_miss(self):
        """Category lookup failed/unknown (P1.3)"""
        self.category_lookup_miss += 1

    def incr_unknown_category(self):
        """Event has unknown/missing category (P1.3)"""
        self.unknown_category_events += 1

    def incr_lua_failure(self):
        """Lua script materialization failed (P1.3)"""
        self.lua_materialization_failures += 1

    def observe_processing_latency(self, latency_ms: float):
        """Record processing latency (P1.7)"""
        self.processing_latencies.append(latency_ms)
        if len(self.processing_latencies) > self.max_latency_samples:
            self.processing_latencies.pop(0)

    def uptime(self):
        return time.time() - self.start_time

    def rate(self):
        uptime = self.uptime()
        return self.processed / uptime if uptime > 0 else 0.0

    def latency_stats(self):
        """Calculate latency percentiles (P1.7)"""
        if not self.processing_latencies:
            return {"p50": 0.0, "p95": 0.0, "p99": 0.0, "avg": 0.0}

        sorted_latencies = sorted(self.processing_latencies)
        n = len(sorted_latencies)
        p50_idx = int(n * 0.50)
        p95_idx = int(n * 0.95)
        p99_idx = int(n * 0.99)

        return {
            "p50": sorted_latencies[p50_idx] if p50_idx < n else 0.0,
            "p95": sorted_latencies[p95_idx] if p95_idx < n else 0.0,
            "p99": sorted_latencies[p99_idx] if p99_idx < n else 0.0,
            "avg": sum(sorted_latencies) / n,
        }

    def to_prometheus(self):
        """Format metrics in Prometheus exposition format"""
        metrics = []

        # Counter metrics
        metrics.append(
            "# HELP events_processed_total Total events successfully processed"
        )
        metrics.append("# TYPE events_processed_total counter")
        metrics.append(f"events_processed_total {self.processed}")

        metrics.append(
            "# HELP events_failed_total Total events that failed processing"
        )
        metrics.append("# TYPE events_failed_total counter")
        metrics.append(f"events_failed_total {self.failed}")

        metrics.append("# HELP events_deduped_total Total duplicate events skipped")
        metrics.append("# TYPE events_deduped_total counter")
        metrics.append(f"events_deduped_total {self.deduped}")

        # P1.3: Feature-quality metrics
        metrics.append(
            "# HELP category_lookup_hit_total Category lookups that found a valid category"
        )
        metrics.append("# TYPE category_lookup_hit_total counter")
        metrics.append(f"category_lookup_hit_total {self.category_lookup_hit}")

        metrics.append(
            "# HELP category_lookup_miss_total Category lookups that failed or returned unknown"
        )
        metrics.append("# TYPE category_lookup_miss_total counter")
        metrics.append(f"category_lookup_miss_total {self.category_lookup_miss}")

        metrics.append(
            "# HELP unknown_category_events_total Events processed with unknown/missing category"
        )
        metrics.append("# TYPE unknown_category_events_total counter")
        metrics.append(f"unknown_category_events_total {self.unknown_category_events}")

        metrics.append(
            "# HELP lua_materialization_failures_total Lua script execution failures"
        )
        metrics.append("# TYPE lua_materialization_failures_total counter")
        metrics.append(
            f"lua_materialization_failures_total {self.lua_materialization_failures}"
        )

        # Gauge metrics
        metrics.append("# HELP consumer_uptime_seconds Consumer uptime in seconds")
        metrics.append("# TYPE consumer_uptime_seconds gauge")
        metrics.append(f"consumer_uptime_seconds {self.uptime():.1f}")

        metrics.append("# HELP processing_rate_events_per_sec Current processing rate")
        metrics.append("# TYPE processing_rate_events_per_sec gauge")
        metrics.append(f"processing_rate_events_per_sec {self.rate():.2f}")

        # P1.7: Latency metrics
        latency = self.latency_stats()
        metrics.append(
            "# HELP event_processing_latency_p50_ms Event processing latency p50 in milliseconds"
        )
        metrics.append("# TYPE event_processing_latency_p50_ms gauge")
        metrics.append(f"event_processing_latency_p50_ms {latency['p50']:.2f}")

        metrics.append(
            "# HELP event_processing_latency_p95_ms Event processing latency p95 in milliseconds"
        )
        metrics.append("# TYPE event_processing_latency_p95_ms gauge")
        metrics.append(f"event_processing_latency_p95_ms {latency['p95']:.2f}")

        metrics.append(
            "# HELP event_processing_latency_p99_ms Event processing latency p99 in milliseconds"
        )
        metrics.append("# TYPE event_processing_latency_p99_ms gauge")
        metrics.append(f"event_processing_latency_p99_ms {latency['p99']:.2f}")

        metrics.append(
            "# HELP event_processing_latency_avg_ms Event processing latency average in milliseconds"
        )
        metrics.append("# TYPE event_processing_latency_avg_ms gauge")
        metrics.append(f"event_processing_latency_avg_ms {latency['avg']:.2f}")

        return "\n".join(metrics) + "\n"

    def to_json(self):
        """Format metrics as JSON"""
        import json

        latency = self.latency_stats()
        return json.dumps(
            {
                "events_processed_total": self.processed,
                "events_failed_total": self.failed,
                "events_deduped_total": self.deduped,
                "consumer_uptime_seconds": round(self.uptime(), 1),
                "processing_rate_events_per_sec": round(self.rate(), 2),
                "event_processing_latency_ms": {
                    "p50": round(latency["p50"], 2),
                    "p95": round(latency["p95"], 2),
                    "p99": round(latency["p99"], 2),
                    "avg": round(latency["avg"], 2),
                },
            },
            indent=2,
        )


class MetricsHandler(BaseHTTPRequestHandler):
    """HTTP request handler for metrics endpoint"""

    collector = None  # Set by MetricsServer

    def log_message(self, format, *args):
        """Suppress default HTTP server logs"""
        pass

    def do_GET(self):
        if self.path == "/metrics":
            self.send_response(200)
            self.send_header("Content-Type", "text/plain; version=0.0.4")
            self.end_headers()
            self.wfile.write(self.collector.to_prometheus().encode("utf-8"))

        elif self.path == "/metrics.json":
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(self.collector.to_json().encode("utf-8"))

        elif self.path == "/health":
            self.send_response(200)
            self.send_header("Content-Type", "text/plain")
            self.end_headers()
            self.wfile.write(b"OK\n")

        else:
            self.send_response(404)
            self.end_headers()
            self.wfile.write(b"Not Found\n")


class MetricsServer:
    """
    HTTP server for exposing metrics
    Runs in background thread
    """

    def __init__(self, collector: MetricsCollector, port: int = 8000):
        self.collector = collector
        self.port = port
        self.server = None
        self.thread = None

    def start(self):
        """Start metrics server in background thread"""
        MetricsHandler.collector = self.collector

        self.server = HTTPServer(("0.0.0.0", self.port), MetricsHandler)

        self.thread = Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

        logger.info(f"📊 Metrics server started on http://0.0.0.0:{self.port}/metrics")

    def stop(self):
        """Stop metrics server"""
        if self.server:
            self.server.shutdown()
            logger.info("📊 Metrics server stopped")
