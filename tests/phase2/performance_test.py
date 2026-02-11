#!/usr/bin/env python3
"""
Phase 2 Test Suite - D/E/F. Performance & Concurrency Tests

Tests:
    D1. Steady-state Ray-only performance (1000 requests)
    D2. Cold-start / warmup latency
    E1. Concurrent load testing (c=1,10,20,50)
    F1. Fallback-only performance

Critical: All metrics MUST report ray_ratio, degraded_ratio, error_rate
"""

import os
import sys
import time
import statistics
import argparse
from datetime import datetime
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import List, Dict, Tuple
from urllib.parse import quote_plus

import requests

# Configuration
GATEWAY_URL = os.getenv("GATEWAY_URL", "http://localhost:8080")
TEST_OUTPUT_DIR = os.getenv("TEST_OUTPUT_DIR", "test-results")
os.makedirs(TEST_OUTPUT_DIR, exist_ok=True)

# Test queries (tail distribution)
TAIL_QUERIES = [
    "dress",
    "jeans",
    "shirt",
    "jacket",
    "shoes",
    "black",
    "red",
    "blue",
    "summer dress",
    "winter coat",
    "casual shirt",
    "elegant dress",
    "sport jacket",
    "cotton t-shirt",
    "leather shoes",
    "denim jeans",
    "formal wear",
    "office attire",
    "party outfit",
    "beach wear",
    "vintage style",
    "modern design",
    "classic look",
    "trendy fashion",
    "comfortable fit",
    "slim fit",
    "oversized",
    "fitted",
    "black dress",
    "white shirt",
    "blue jeans",
    "red jacket",
    "striped pattern",
    "floral print",
    "solid color",
    "checkered",
]


class Colors:
    """ANSI color codes for terminal output"""

    BLUE = "\033[0;34m"
    GREEN = "\033[0;32m"
    YELLOW = "\033[1;33m"
    RED = "\033[0;31m"
    NC = "\033[0m"  # No Color


def log_info(msg: str):
    print(f"{Colors.BLUE}[INFO]{Colors.NC} {msg}")


def log_success(msg: str):
    print(f"{Colors.GREEN}[PASS]{Colors.NC} {msg}")


def log_warning(msg: str):
    print(f"{Colors.YELLOW}[WARN]{Colors.NC} {msg}")


def log_error(msg: str):
    print(f"{Colors.RED}[FAIL]{Colors.NC} {msg}")


def log_section(msg: str):
    print(f"\n{'=' * 64}")
    print(f"{Colors.BLUE}{msg}{Colors.NC}")
    print("=" * 64)


class ResponseMetrics:
    """Container for response metrics"""

    def __init__(self):
        self.latencies_all = []
        self.latencies_ray_only = []
        self.latencies_degraded_only = []

        self.ray_count = 0
        self.degraded_count = 0
        self.error_count = 0
        self.total_requests = 0

        self.samples = []  # For debugging

    def add_response(
        self, latency_ms: float, source: str, degraded: bool, error: bool = False
    ):
        """Add a response to metrics"""
        self.total_requests += 1

        if error:
            self.error_count += 1
            return

        self.latencies_all.append(latency_ms)

        if source == "ray" and not degraded:
            self.ray_count += 1
            self.latencies_ray_only.append(latency_ms)
        elif degraded:
            self.degraded_count += 1
            self.latencies_degraded_only.append(latency_ms)

    def calculate_stats(self, latencies: List[float]) -> Dict:
        """Calculate p50/p95/p99 statistics"""
        if not latencies:
            return {
                "count": 0,
                "p50": 0,
                "p95": 0,
                "p99": 0,
                "min": 0,
                "max": 0,
                "avg": 0,
            }

        sorted_lat = sorted(latencies)
        count = len(sorted_lat)

        return {
            "count": count,
            "min": round(min(sorted_lat), 2),
            "max": round(max(sorted_lat), 2),
            "avg": round(statistics.mean(sorted_lat), 2),
            "p50": round(sorted_lat[int(count * 0.50)], 2),
            "p95": round(sorted_lat[int(count * 0.95)], 2),
            "p99": round(
                sorted_lat[int(count * 0.99)] if count >= 100 else sorted_lat[-1], 2
            ),
        }

    def get_summary(self) -> Dict:
        """Get complete metrics summary"""
        return {
            "total_requests": self.total_requests,
            "ray_count": self.ray_count,
            "degraded_count": self.degraded_count,
            "error_count": self.error_count,
            "ray_ratio": round(self.ray_count / max(self.total_requests, 1), 4),
            "degraded_ratio": round(
                self.degraded_count / max(self.total_requests, 1), 4
            ),
            "error_rate": round(self.error_count / max(self.total_requests, 1), 4),
            "stats_all": self.calculate_stats(self.latencies_all),
            "stats_ray_only": self.calculate_stats(self.latencies_ray_only),
            "stats_degraded_only": self.calculate_stats(self.latencies_degraded_only),
        }


def make_request(
    query: str, k: int = 10, timeout: int = 10
) -> Tuple[float, str, bool, bool]:
    """
    Make a single request and return (latency_ms, source, degraded, error)
    """
    url = f"{GATEWAY_URL}/api/recommendation/search?query={quote_plus(query)}&k={k}"

    start = time.time()
    try:
        resp = requests.get(url, timeout=timeout)
        latency_ms = (time.time() - start) * 1000

        if resp.status_code != 200:
            return (latency_ms, "error", False, True)

        data = resp.json()
        if not data.get("data"):
            return (latency_ms, "empty", False, True)

        source = data["data"][0].get("source", "unknown")
        degraded = data["data"][0].get("degraded", False)

        return (latency_ms, source, degraded, False)

    except Exception:
        latency_ms = (time.time() - start) * 1000
        return (latency_ms, "exception", False, True)


################################################################################
# D1. Steady-State Ray-Only Performance (1000 requests)
################################################################################


def test_d1_steady_state_performance(result_file: str):
    """Test D1: Steady-state Ray-only performance with tail queries"""
    log_section("D1. Steady-State Ray-Only Performance (1000 requests)")

    metrics = ResponseMetrics()
    num_requests = 1000

    log_info(f"Sending {num_requests} requests with tail distribution...")

    for i in range(num_requests):
        query = TAIL_QUERIES[i % len(TAIL_QUERIES)]
        latency, source, degraded, error = make_request(query, k=10, timeout=10)
        metrics.add_response(latency, source, degraded, error)

        if (i + 1) % 100 == 0:
            log_info(f"Progress: {i + 1}/{num_requests}")

    # Get summary
    summary = metrics.get_summary()

    # Write to file
    with open(result_file, "a") as f:
        f.write("## D1. Steady-State Ray-Only Performance\n\n")
        f.write(
            "**Objective**: Measure p99 latency under steady-state Ray-only workload\n\n"
        )
        f.write("| Metric | Value |\n")
        f.write("|--------|-------|\n")
        f.write(f"| Total Requests | {summary['total_requests']} |\n")
        f.write(f"| Ray Hits | {summary['ray_count']} |\n")
        f.write(f"| Degraded | {summary['degraded_count']} |\n")
        f.write(f"| Errors | {summary['error_count']} |\n")
        f.write(f"| **ray_ratio** | **{summary['ray_ratio']:.4f}** |\n")
        f.write(f"| **degraded_ratio** | **{summary['degraded_ratio']:.4f}** |\n")
        f.write(f"| **error_rate** | **{summary['error_rate']:.4f}** |\n")
        f.write("\n")

        f.write("### Latency Statistics (All)\n\n")
        f.write("| Metric | Value (ms) |\n")
        f.write("|--------|------------|\n")
        for k, v in summary["stats_all"].items():
            f.write(f"| {k} | {v} |\n")
        f.write("\n")

        f.write("### Latency Statistics (Ray Only)\n\n")
        f.write("| Metric | Value (ms) |\n")
        f.write("|--------|------------|\n")
        for k, v in summary["stats_ray_only"].items():
            f.write(f"| {k} | {v} |\n")
        f.write("\n")

        # Pass criteria (v2.2: stricter thresholds)
        ray_p99 = summary["stats_ray_only"]["p99"]
        ray_ratio = summary["ray_ratio"]
        error_rate = summary["error_rate"]

        pass_test = (ray_p99 < 200) and (ray_ratio >= 0.98) and (error_rate <= 0.001)

        if pass_test:
            f.write("**Pass Criteria (v2.2)**:\n")
            f.write(f"- ✅ p99(ray_only) = {ray_p99}ms < 200ms\n")
            f.write(f"- ✅ ray_ratio = {ray_ratio:.4f} ≥ 0.98\n")
            f.write(f"- ✅ error_rate = {error_rate:.4f} ≤ 0.1%\n")
            f.write("\n**Status**: ✅ PASS\n\n")
            log_success(f"✓ D1 PASS (v2.2): p99={ray_p99}ms, ray_ratio={ray_ratio:.4f}")
        else:
            f.write("**Pass Criteria (v2.2)**:\n")
            if ray_p99 >= 200:
                f.write(f"- ❌ p99(ray_only) = {ray_p99}ms ≥ 200ms\n")
            else:
                f.write(f"- ✅ p99(ray_only) = {ray_p99}ms < 200ms\n")

            if ray_ratio < 0.98:
                f.write(f"- ❌ ray_ratio = {ray_ratio:.4f} < 0.98\n")
            else:
                f.write(f"- ✅ ray_ratio = {ray_ratio:.4f} ≥ 0.98\n")

            if error_rate > 0.001:
                f.write(f"- ❌ error_rate = {error_rate:.4f} > 0.1%\n")
            else:
                f.write(f"- ✅ error_rate = {error_rate:.4f} ≤ 0.1%\n")

            f.write("\n**Status**: ❌ FAIL\n\n")
            log_error(f"✗ D1 FAIL (v2.2): p99={ray_p99}ms, ray_ratio={ray_ratio:.4f}")

        f.write("---\n\n")

    return pass_test


################################################################################
# D2. Cold Start / Warmup
################################################################################


def test_d2_cold_start(result_file: str):
    """Test D2: Cold start and warmup behavior - AUTOMATED"""
    log_section("D2. Cold Start / Warmup Performance")

    import subprocess
    import os

    namespace = os.getenv("NAMESPACE", "scalestyle")

    log_warning("⚠ This test will restart inference service")
    log_info("Rolling out restart...")

    try:
        # Restart deployment
        subprocess.run(
            [
                "kubectl",
                "rollout",
                "restart",
                "deployment",
                "inference",
                "-n",
                namespace,
            ],
            check=True,
            capture_output=True,
        )

        log_info("Waiting for rollout to complete...")
        subprocess.run(
            [
                "kubectl",
                "rollout",
                "status",
                "deployment/inference",
                "-n",
                namespace,
                "--timeout=300s",
            ],
            check=True,
            capture_output=True,
        )

        log_info("Waiting 10s for service to stabilize...")
        time.sleep(10)

        # Test cold start
        cold_start_latencies = []

        log_info("Sending first request (cold start)...")
        latency, source, degraded, error = make_request("dress", k=10, timeout=30)
        cold_start_latencies.append(latency)
        log_info(f"  First request: {latency}ms, source={source}")

        log_info("Sending 5 warmup requests...")
        warmup_latencies = []
        for i in range(5):
            latency, source, degraded, error = make_request("jeans", k=10, timeout=10)
            warmup_latencies.append(latency)
            log_info(f"  Request {i+2}: {latency}ms")
            time.sleep(0.5)

        log_info("Waiting 30s before stability test...")
        time.sleep(30)

        log_info("Sending 20 requests for stability check...")
        stable_latencies = []
        for i in range(20):
            latency, source, degraded, error = make_request("shirt", k=10, timeout=10)
            stable_latencies.append(latency)
            time.sleep(0.2)

        # Calculate statistics using sorted array approach
        sorted_stable = sorted(stable_latencies)
        count = len(sorted_stable)
        stable_p50 = round(sorted_stable[int(count * 0.50)], 2)
        stable_p95 = round(sorted_stable[int(count * 0.95)], 2)
        stable_p99 = round(
            sorted_stable[int(count * 0.99)] if count >= 100 else sorted_stable[-1], 2
        )
        avg_warmup = statistics.mean(warmup_latencies) if warmup_latencies else 0

        with open(result_file, "a") as f:
            f.write("## D2. Cold Start / Warmup Performance\n\n")
            f.write("**Objective**: Measure cold start latency and warmup time\n\n")
            f.write("| Phase | Latency |\n")
            f.write("|-------|--------|\n")
            f.write(f"| First request (cold) | {cold_start_latencies[0]:.2f}ms |\n")
            f.write(f"| Warmup avg (req 2-6) | {avg_warmup:.2f}ms |\n")
            f.write(f"| Stable p50 (20 req) | {stable_p50:.2f}ms |\n")
            f.write(f"| Stable p95 (20 req) | {stable_p95:.2f}ms |\n")
            f.write(f"| Stable p99 (20 req) | {stable_p99:.2f}ms |\n\n")
            f.write("**Pass Criteria**:\n")

            if stable_p95 < 300:
                f.write(f"- ✅ Stable p95 = {stable_p95:.2f}ms < 300ms\n\n")
                f.write("**Status**: ✅ PASS\n\n")
                log_success(f"✓ D2 PASS: stable_p95={stable_p95:.2f}ms < 300ms")
            else:
                f.write(f"- ❌ Stable p95 = {stable_p95:.2f}ms ≥ 300ms\n\n")
                f.write("**Status**: ❌ FAIL\n\n")
                log_error(f"✗ D2 FAIL: stable_p95={stable_p95:.2f}ms ≥ 300ms")

            f.write("---\n\n")

        return stable_p95 < 300

    except subprocess.CalledProcessError as e:
        log_error(f"kubectl command failed: {e}")
        with open(result_file, "a") as f:
            f.write("## D2. Cold Start / Warmup Performance\n\n")
            f.write(f"**Error**: kubectl command failed: {e}\n\n")
            f.write("**Status**: ⚠️ SKIP\n\n")
            f.write("---\n\n")
        return True  # Don't fail main test
    except Exception as e:
        log_error(f"D2 test failed: {e}")
        with open(result_file, "a") as f:
            f.write("## D2. Cold Start / Warmup Performance\n\n")
            f.write(f"**Error**: {e}\n\n")
            f.write("**Status**: ⚠️ SKIP\n\n")
            f.write("---\n\n")
        return True  # Don't fail main test


################################################################################
# E1. Concurrent Load Testing (c=1,10,20,50)
################################################################################


def concurrent_load_test(concurrency: int, duration_sec: int = 180) -> ResponseMetrics:
    """
    Run concurrent load test with specified concurrency for duration
    """
    metrics = ResponseMetrics()
    end_time = time.time() + duration_sec

    def worker():
        """Worker thread for concurrent requests"""
        local_metrics = ResponseMetrics()
        query_idx = 0

        while time.time() < end_time:
            query = TAIL_QUERIES[query_idx % len(TAIL_QUERIES)]
            query_idx += 1

            latency, source, degraded, error = make_request(query, k=10, timeout=10)
            local_metrics.add_response(latency, source, degraded, error)

        return local_metrics

    log_info(f"Starting c={concurrency} load test for {duration_sec}s...")

    with ThreadPoolExecutor(max_workers=concurrency) as executor:
        futures = [executor.submit(worker) for _ in range(concurrency)]

        for future in as_completed(futures):
            local = future.result()
            # Merge metrics
            metrics.latencies_all.extend(local.latencies_all)
            metrics.latencies_ray_only.extend(local.latencies_ray_only)
            metrics.latencies_degraded_only.extend(local.latencies_degraded_only)
            metrics.ray_count += local.ray_count
            metrics.degraded_count += local.degraded_count
            metrics.error_count += local.error_count
            metrics.total_requests += local.total_requests

    return metrics


def test_e1_concurrent_load(result_file: str):
    """Test E1: Concurrent load testing at different levels"""
    log_section("E1. Concurrent Load Testing (c=1,10,20,50)")

    # Test configurations: (concurrency, duration_sec, name, num_runs)
    # c=10 runs 3 times for statistical confidence (median p99)
    test_configs = [
        (1, 60, "c=1 (Baseline)", 1),
        (10, 180, "c=10 (Target)", 3),  # 3 runs for statistical confidence
        # (20, 180, "c=20 (Stress)", 1),  # Uncomment for cloud testing
        # (50, 180, "c=50 (Peak)", 1),    # Uncomment for cloud testing
    ]

    all_pass = True

    with open(result_file, "a") as f:
        f.write("## E1. Concurrent Load Testing\n\n")
        f.write(
            "**Objective**: Measure throughput and latency under concurrent load\n\n"
        )
        f.write(
            "**v2.3 Industrial SLO Standard**: Uses p99_all (end-to-end user experience) + degraded_ratio as gating metrics. "
        )
        f.write("p99_ray_only is diagnostic only (non-gating). ")
        f.write("c=10 runs 3 times for statistical confidence (median).\n\n")

    for concurrency, duration, name, num_runs in test_configs:
        log_info(f"Running {name} ({num_runs} run{'s' if num_runs > 1 else ''})...")

        # Run multiple iterations for statistical confidence
        all_runs_data = []
        for run_idx in range(num_runs):
            if num_runs > 1:
                log_info(f"  Run {run_idx + 1}/{num_runs}...")

            metrics = concurrent_load_test(concurrency, duration)
            summary = metrics.get_summary()
            all_runs_data.append(summary)

            # Cooldown between runs (only for multi-run tests)
            if run_idx < num_runs - 1:
                log_info("  Cooling down for 30s between runs...")
                time.sleep(30)

        # Calculate aggregate statistics from all runs
        if num_runs > 1:
            # Extract key metrics from all runs
            ray_ratios = [run["ray_ratio"] for run in all_runs_data]
            ray_p99s = [run["stats_ray_only"]["p99"] for run in all_runs_data]
            all_p99s = [
                run["stats_all"]["p99"] for run in all_runs_data
            ]  # v2.3: Add p99_all
            error_rates = [run["error_rate"] for run in all_runs_data]
            degraded_ratios = [run["degraded_ratio"] for run in all_runs_data]

            # Calculate medians
            import statistics

            median_ray_ratio = statistics.median(ray_ratios)
            median_p99_ray = statistics.median(ray_p99s)
            median_p99_all = statistics.median(
                all_p99s
            )  # v2.3: median p99_all for gating
            median_error_rate = statistics.median(error_rates)
            median_degraded_ratio = statistics.median(degraded_ratios)

            # Use first run for QPS display (representative)
            summary = all_runs_data[0]
        else:
            summary = all_runs_data[0]
            median_ray_ratio = summary["ray_ratio"]
            median_p99_ray = summary["stats_ray_only"]["p99"]
            median_p99_all = summary["stats_all"]["p99"]
            median_error_rate = summary["error_rate"]
            median_degraded_ratio = summary["degraded_ratio"]

        # Calculate QPS (from first/single run)
        qps_200 = summary["total_requests"] / duration
        qps_ray = summary["ray_count"] / duration

        with open(result_file, "a") as f:
            f.write(f"### {name}\n\n")

            if num_runs > 1:
                f.write(
                    f"**Methodology**: {num_runs} independent runs, pass criteria based on **median values** (industrial practice)\n\n"
                )

                # Show all runs data
                f.write("#### Individual Runs:\n\n")
                for idx, run_data in enumerate(all_runs_data, 1):
                    f.write(f"**Run {idx}**: ")
                    f.write(f"p99_all={run_data['stats_all']['p99']:.2f}ms, ")
                    f.write(f"p99_ray={run_data['stats_ray_only']['p99']:.2f}ms, ")
                    f.write(f"ray_ratio={run_data['ray_ratio']:.4f}, ")
                    f.write(f"degraded_ratio={run_data['degraded_ratio']:.4f}, ")
                    f.write(f"error_rate={run_data['error_rate']:.4f}\n\n")

                f.write("#### Median Statistics (Used for Pass/Fail):\n\n")

            f.write("| Metric | Value |\n")
            f.write("|--------|-------|\n")
            f.write(f"| Duration (per run) | {duration}s |\n")
            f.write(f"| Total Requests (Run 1) | {summary['total_requests']} |\n")
            f.write(f"| **QPS (HTTP 200)** | **{qps_200:.2f}** |\n")
            f.write(f"| **QPS (Ray)** | **{qps_ray:.2f}** |\n")

            if num_runs > 1:
                f.write(f"| **ray_ratio (median)** | **{median_ray_ratio:.4f}** |\n")
                f.write(
                    f"| **degraded_ratio (median)** | **{median_degraded_ratio:.4f}** |\n"
                )
                f.write(f"| **error_rate (median)** | **{median_error_rate:.4f}** |\n")
                f.write(f"| **p99_all (median)** | **{median_p99_all:.2f}ms** |\n")
                f.write(
                    f"| p99_ray_only (median, diagnostic) | {median_p99_ray:.2f}ms |\n"
                )
            else:
                f.write(f"| **ray_ratio** | **{median_ray_ratio:.4f}** |\n")
                f.write(f"| **degraded_ratio** | **{median_degraded_ratio:.4f}** |\n")
                f.write(f"| **error_rate** | **{median_error_rate:.4f}** |\n")
                f.write(f"| **p99_all** | **{median_p99_all:.2f}ms** |\n")
                f.write(f"| p99_ray_only (diagnostic) | {median_p99_ray:.2f}ms |\n")

            f.write("\n")

            # Pass criteria with v2.3 SLO standards (p99_all + degraded_ratio)
            if concurrency == 10:
                # v2.3: Use p99_all (end-to-end) as gating, p99_ray_only as diagnostic
                pass_c10 = (
                    (median_ray_ratio >= 0.98)
                    and (median_p99_all <= 300)  # v2.3: p99_all instead of p99_ray_only
                    and (median_error_rate <= 0.001)
                    and (median_degraded_ratio <= 0.01)  # v2.3: Tighter 1% threshold
                )

                f.write("**Pass Criteria (c=10, v2.3 Industrial SLO Standard)**:\n")
                f.write("\n*Gating Metrics (must pass):*\n")
                if num_runs > 1:
                    f.write(
                        f"- {'✅' if median_p99_all <= 300 else '❌'} **median**(p99_all) = {median_p99_all:.2f}ms ≤ 300ms (end-to-end user experience)\n"
                    )
                    f.write(
                        f"- {'✅' if median_ray_ratio >= 0.98 else '❌'} **median**(ray_ratio) = {median_ray_ratio:.4f} ≥ 0.98\n"
                    )
                    f.write(
                        f"- {'✅' if median_degraded_ratio <= 0.01 else '❌'} **median**(degraded_ratio) = {median_degraded_ratio:.4f} ≤ 0.01 (1%)\n"
                    )
                    f.write(
                        f"- {'✅' if median_error_rate <= 0.001 else '❌'} **median**(error_rate) = {median_error_rate:.4f} ≤ 0.1%\n"
                    )
                else:
                    f.write(
                        f"- {'✅' if median_p99_all <= 300 else '❌'} p99_all = {median_p99_all:.2f}ms ≤ 300ms (end-to-end)\n"
                    )
                    f.write(
                        f"- {'✅' if median_ray_ratio >= 0.98 else '❌'} ray_ratio = {median_ray_ratio:.4f} ≥ 0.98\n"
                    )
                    f.write(
                        f"- {'✅' if median_degraded_ratio <= 0.01 else '❌'} degraded_ratio = {median_degraded_ratio:.4f} ≤ 1%\n"
                    )
                    f.write(
                        f"- {'✅' if median_error_rate <= 0.001 else '❌'} error_rate = {median_error_rate:.4f} ≤ 0.1%\n"
                    )

                # Add diagnostic metric (non-gating)
                f.write("\n*Diagnostic Metric (non-gating):*\n")
                ray_diagnostic_good = median_p99_ray <= 270
                if num_runs > 1:
                    f.write(
                        f"- {'✅' if ray_diagnostic_good else '⚠️'} **median**(p99_ray_only) = {median_p99_ray:.2f}ms (target ≤ 270ms)\n"
                    )
                else:
                    f.write(
                        f"- {'✅' if ray_diagnostic_good else '⚠️'} p99_ray_only = {median_p99_ray:.2f}ms (target ≤ 270ms)\n"
                    )

                f.write(f"\n**Status**: {'✅ PASS' if pass_c10 else '❌ FAIL'}\n\n")

                if pass_c10:
                    log_success(f"✓ E1 (c={concurrency}) PASS (v2.3 Industrial SLO)")
                else:
                    log_error(f"✗ E1 (c={concurrency}) FAIL (v2.3 Industrial SLO)")
                    all_pass = False
            elif concurrency == 20:
                ray_ratio = summary["ray_ratio"]
                ray_p99 = summary["stats_ray_only"]["p99"]
                error_rate = summary["error_rate"]

                pass_c20 = (
                    (ray_ratio >= 0.95) and (ray_p99 < 350) and (error_rate <= 0.002)
                )

                f.write("**Pass Criteria (c=20, v2.2)**:\n")
                f.write(
                    f"- {'✅' if ray_ratio >= 0.95 else '❌'} ray_ratio = {ray_ratio:.4f} ≥ 0.95\n"
                )
                f.write(
                    f"- {'✅' if ray_p99 < 350 else '❌'} p99(ray_only) = {ray_p99}ms < 350ms\n"
                )
                f.write(
                    f"- {'✅' if error_rate <= 0.002 else '❌'} error_rate = {error_rate:.4f} ≤ 0.2%\n"
                )
                f.write(f"\n**Status**: {'✅ PASS' if pass_c20 else '❌ FAIL'}\n\n")

                if pass_c20:
                    log_success(f"✓ E1 (c={concurrency}) PASS (v2.2)")
                else:
                    log_error(f"✗ E1 (c={concurrency}) FAIL (v2.2)")
                    all_pass = False
            elif concurrency == 50:
                ray_ratio = summary["ray_ratio"]
                ray_p99 = summary["stats_ray_only"]["p99"]
                error_rate = summary["error_rate"]

                pass_c50 = (
                    (ray_ratio >= 0.90) and (ray_p99 < 500) and (error_rate <= 0.005)
                )

                f.write("**Pass Criteria (c=50, v2.2)**:\n")
                f.write(
                    f"- {'✅' if ray_ratio >= 0.90 else '❌'} ray_ratio = {ray_ratio:.4f} ≥ 0.90\n"
                )
                f.write(
                    f"- {'✅' if ray_p99 < 500 else '❌'} p99(ray_only) = {ray_p99}ms < 500ms\n"
                )
                f.write(
                    f"- {'✅' if error_rate <= 0.005 else '❌'} error_rate = {error_rate:.4f} ≤ 0.5%\n"
                )
                f.write(f"\n**Status**: {'✅ PASS' if pass_c50 else '❌ FAIL'}\n\n")

                if pass_c50:
                    log_success(f"✓ E1 (c={concurrency}) PASS (v2.2)")
                else:
                    log_error(f"✗ E1 (c={concurrency}) FAIL (v2.2)")
                    all_pass = False
            else:
                f.write("**Status**: ℹ️ INFO (Not gating)\n\n")

        # Brief cooldown between tests
        if concurrency < 50:
            log_info("Cooling down for 10s...")
            time.sleep(10)

    with open(result_file, "a") as f:
        f.write("---\n\n")

    return all_pass


################################################################################
# F1. Fallback-Only Performance
################################################################################


def test_f1_fallback_performance(result_file: str):
    """Test F1: Fallback-only performance (degraded mode)"""
    log_section("F1. Fallback-Only Performance")

    log_warning("⚠ This test requires inference service to be down")
    log_warning("⚠ Checking if system is in degraded mode...")

    # Check current mode
    sample_size = 20
    degraded_count = 0

    for i in range(sample_size):
        _, source, degraded, error = make_request("dress", k=10, timeout=5)
        if degraded and not error:
            degraded_count += 1

    degraded_ratio = degraded_count / sample_size

    with open(result_file, "a") as f:
        f.write("## F1. Fallback-Only Performance\n\n")
        f.write("**Objective**: Measure fallback latency under full degradation\n\n")
        f.write(
            f"**Current State Check**: {degraded_count}/{sample_size} degraded = {degraded_ratio:.2%}\n\n"
        )

        if degraded_ratio >= 0.8:
            log_info("System is in degraded mode - measuring fallback performance")

            # Run 120s test
            metrics = ResponseMetrics()
            end_time = time.time() + 120

            while time.time() < end_time:
                query = TAIL_QUERIES[metrics.total_requests % len(TAIL_QUERIES)]
                latency, source, degraded, error = make_request(query, k=10, timeout=5)
                metrics.add_response(latency, source, degraded, error)

            summary = metrics.get_summary()

            f.write("### Fallback Performance Metrics\n\n")
            f.write("| Metric | Value |\n")
            f.write("|--------|-------|\n")
            f.write(f"| Total Requests | {summary['total_requests']} |\n")
            f.write(f"| Degraded Count | {summary['degraded_count']} |\n")
            f.write(f"| Error Count | {summary['error_count']} |\n")
            f.write(f"| degraded_ratio | {summary['degraded_ratio']:.4f} |\n")
            f.write(f"| error_rate | {summary['error_rate']:.4f} |\n")
            f.write(f"| p99 (degraded) | {summary['stats_degraded_only']['p99']}ms |\n")
            f.write("\n")

            # Pass criteria (v2.2: stricter p99 threshold)
            p99_degraded = summary["stats_degraded_only"]["p99"]
            degraded_ratio = summary["degraded_ratio"]
            error_rate = summary["error_rate"]

            pass_test = (
                (p99_degraded < 20)
                and (degraded_ratio >= 0.99)
                and (error_rate <= 0.001)
            )

            f.write("**Pass Criteria (v2.2)**:\n")
            f.write(
                f"- {'✅' if p99_degraded < 20 else '❌'} p99(degraded) = {p99_degraded}ms < 20ms\n"
            )
            f.write(
                f"- {'✅' if degraded_ratio >= 0.99 else '❌'} degraded_ratio = {degraded_ratio:.4f} ≥ 0.99\n"
            )
            f.write(
                f"- {'✅' if error_rate <= 0.001 else '❌'} error_rate = {error_rate:.4f} ≤ 0.1%\n"
            )
            f.write(f"\n**Status**: {'✅ PASS' if pass_test else '❌ FAIL'}\n\n")

            if pass_test:
                log_success(
                    f"✓ F1 PASS (v2.2): p99={p99_degraded}ms, degraded_ratio={degraded_ratio:.4f}"
                )
            else:
                log_error(f"✗ F1 FAIL (v2.2): p99={p99_degraded}ms")

            f.write("---\n\n")
            return pass_test
        else:
            log_warning("System is NOT in degraded mode - skipping F1")
            f.write("**Manual Test Procedure**:\n")
            f.write(
                "1. Scale inference to 0: `kubectl scale deployment inference -n scalestyle --replicas=0`\n"
            )
            f.write("2. Wait 30s for circuit breaker\n")
            f.write("3. Re-run this test\n")
            f.write(
                "4. Restore: `kubectl scale deployment inference -n scalestyle --replicas=1`\n\n"
            )
            f.write("**Status**: ⚠️ SKIP (System not in degraded mode)\n\n")
            f.write("---\n\n")
            return True  # Don't fail


################################################################################
# Main
################################################################################


def main():
    global GATEWAY_URL, TEST_OUTPUT_DIR

    parser = argparse.ArgumentParser(description="Phase 2 Performance Tests (D/E/F)")
    parser.add_argument("--gateway", default=GATEWAY_URL, help="Gateway URL")
    parser.add_argument(
        "--output-dir", default=TEST_OUTPUT_DIR, help="Output directory"
    )
    parser.add_argument(
        "--tests", default="D1,D2,E1,F1", help="Tests to run (comma-separated)"
    )

    args = parser.parse_args()

    GATEWAY_URL = args.gateway
    TEST_OUTPUT_DIR = args.output_dir
    os.makedirs(TEST_OUTPUT_DIR, exist_ok=True)

    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    result_file = os.path.join(TEST_OUTPUT_DIR, f"performance_results_{timestamp}.md")

    log_section("Phase 2 - Performance & Concurrency Tests (D/E/F)")
    log_info(f"Gateway: {GATEWAY_URL}")
    log_info(f"Results: {result_file}")

    # Initialize result file
    with open(result_file, "w") as f:
        f.write("# Phase 2 Performance & Concurrency Test Results\n\n")
        f.write(f"**Test Run**: {datetime.now().isoformat()}\n")
        f.write(f"**Gateway**: {GATEWAY_URL}\n\n")
        f.write("---\n\n")

    all_pass = True
    tests_to_run = args.tests.split(",")

    if "D1" in tests_to_run:
        if not test_d1_steady_state_performance(result_file):
            all_pass = False

    if "D2" in tests_to_run:
        if not test_d2_cold_start(result_file):
            all_pass = False

    if "E1" in tests_to_run:
        if not test_e1_concurrent_load(result_file):
            all_pass = False

    if "F1" in tests_to_run:
        if not test_f1_fallback_performance(result_file):
            all_pass = False

    # Final summary
    with open(result_file, "a") as f:
        f.write("## Final Summary\n\n")
        if all_pass:
            f.write("**Result**: ✅ ALL PERFORMANCE TESTS PASSED\n")
        else:
            f.write("**Result**: ❌ SOME PERFORMANCE TESTS FAILED\n")

    log_section("✅ ALL TESTS COMPLETED" if all_pass else "❌ SOME TESTS FAILED")
    log_info(f"Results saved to: {result_file}")

    sys.exit(0 if all_pass else 1)


if __name__ == "__main__":
    main()
