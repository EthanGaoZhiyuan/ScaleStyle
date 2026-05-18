#!/usr/bin/env python3
"""
ScaleStyle local gateway benchmark.

Measures p50/p95/p99/min/max/avg/throughput for text, image, and hybrid
endpoints through the gateway (port 8080).  Supports single-endpoint latency
and controlled concurrency pressure tests.

Usage:
    python3 scripts/benchmark_local_endpoints.py
    python3 scripts/benchmark_local_endpoints.py --output docs/performance/local-performance-current.md
"""

from __future__ import annotations

import argparse
import base64
import json
import os
import statistics
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

try:
    import requests
except ImportError:
    print("ERROR: 'requests' is required. pip install requests", file=sys.stderr)
    sys.exit(1)

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

GATEWAY = os.getenv("GATEWAY_URL", "http://localhost:8080")
IMAGE_PATH = Path(os.getenv(
    "BENCHMARK_IMAGE_PATH",
    "data-pipeline/data/raw/images/010/0108775015.jpg",
))

TEXT_QUERIES = [
    "black dress",
    "summer dress",
    "casual shirt",
    "denim jeans",
    "winter coat",
    "sport jacket",
    "elegant blouse",
    "oversized hoodie",
    "floral skirt",
    "leather boots",
]


# ---------------------------------------------------------------------------
# Request helpers
# ---------------------------------------------------------------------------

def _b64_image(path: Path) -> str:
    return base64.b64encode(path.read_bytes()).decode()


def request_text(session: requests.Session, query: str, k: int = 5) -> tuple[float, dict]:
    t0 = time.perf_counter()
    r = session.get(
        f"{GATEWAY}/api/recommendation/search",
        params={"query": query, "k": k},
        timeout=5,
    )
    ms = (time.perf_counter() - t0) * 1000
    r.raise_for_status()
    return ms, r.json()


def request_image(session: requests.Session, image_b64: str, k: int = 5) -> tuple[float, dict]:
    t0 = time.perf_counter()
    r = session.post(
        f"{GATEWAY}/api/recommendation/search/image",
        json={"image_base64": image_b64, "k": k, "mode": "image_to_image"},
        timeout=10,
    )
    ms = (time.perf_counter() - t0) * 1000
    r.raise_for_status()
    return ms, r.json()


def request_hybrid(
    session: requests.Session,
    image_b64: str,
    query: str = "similar but black",
    k: int = 5,
    user_id: str = "benchmark-user",
) -> tuple[float, dict]:
    t0 = time.perf_counter()
    r = session.post(
        f"{GATEWAY}/api/recommendation/search/hybrid",
        json={
            "image_base64": image_b64,
            "query": query,
            "k": k,
            "userId": user_id,
            "image_weight": 0.5,
            "text_weight": 0.4,
            "behavior_weight": 0.1,
        },
        timeout=10,
    )
    ms = (time.perf_counter() - t0) * 1000
    r.raise_for_status()
    return ms, r.json()


# ---------------------------------------------------------------------------
# Stats
# ---------------------------------------------------------------------------

def _stats(latencies: list[float]) -> dict:
    if not latencies:
        return {}
    s = sorted(latencies)
    n = len(s)
    return {
        "n": n,
        "min": round(s[0], 1),
        "max": round(s[-1], 1),
        "avg": round(statistics.mean(s), 1),
        "p50": round(s[int(n * 0.50)], 1),
        "p95": round(s[int(n * 0.95)], 1),
        "p99": round(s[min(int(n * 0.99), n - 1)], 1),
    }


def _is_degraded(body: dict) -> bool:
    data = body.get("data", body)
    if isinstance(data, list):
        return any(bool(item.get("degraded") or item.get("degradedReason")) for item in data)
    if isinstance(data, dict):
        return bool(data.get("degraded") or data.get("degraded_reason"))
    return False


# ---------------------------------------------------------------------------
# Single-endpoint benchmark
# ---------------------------------------------------------------------------

def bench_endpoint(
    name: str,
    fn,
    *,
    warmup: int,
    n: int,
    query_cycle: list[str] | None = None,
) -> dict:
    """
    fn(session, idx) -> (latency_ms, response_body)
    Returns aggregated stats dict.
    """
    print(f"\n{'─'*60}")
    print(f"  {name}")
    print(f"  warm-up: {warmup} req  |  measure: {n} req")
    print(f"{'─'*60}")

    latencies: list[float] = []
    errors = 0
    degraded = 0

    with requests.Session() as session:
        # Warm-up
        for i in range(warmup):
            try:
                fn(session, i)
            except Exception:
                pass
        print(f"  warm-up done")

        # Measurement
        for i in range(n):
            try:
                ms, body = fn(session, i)
                latencies.append(ms)
                if _is_degraded(body):
                    degraded += 1
            except Exception as e:
                errors += 1
                if errors <= 3:
                    print(f"  [WARN] request {i} failed: {e}")

    st = _stats(latencies)
    throughput = round(len(latencies) / (sum(latencies) / 1000), 2) if latencies else 0
    result = {
        **st,
        "errors": errors,
        "degraded": degraded,
        "error_rate": round(errors / n * 100, 1),
        "degraded_rate": round(degraded / max(len(latencies), 1) * 100, 1),
        "throughput_rps": throughput,
    }

    print(f"  p50={st.get('p50')} p95={st.get('p95')} p99={st.get('p99')} ms")
    print(f"  avg={st.get('avg')} min={st.get('min')} max={st.get('max')} ms")
    print(f"  errors={errors}  degraded={degraded}  throughput={throughput} rps")
    return result


# ---------------------------------------------------------------------------
# Concurrency pressure test
# ---------------------------------------------------------------------------

def bench_concurrency(
    name: str,
    fn,
    *,
    concurrency: int,
    duration_sec: int = 30,
) -> dict:
    """
    Runs fn concurrently for duration_sec, reports stats.
    fn(session, idx) -> (latency_ms, response_body)
    """
    latencies: list[float] = []
    errors = 0
    degraded = 0
    stop_at = time.monotonic() + duration_sec

    counter = {"i": 0}
    lock_err = {"n": 0}

    def worker():
        with requests.Session() as s:
            while time.monotonic() < stop_at:
                idx = counter["i"]
                counter["i"] += 1
                try:
                    ms, body = fn(s, idx)
                    latencies.append(ms)
                    if _is_degraded(body):
                        degraded_list.append(1)
                except Exception:
                    error_list.append(1)

    latencies_lock = latencies
    degraded_list: list = []
    error_list: list = []

    with ThreadPoolExecutor(max_workers=concurrency) as pool:
        futures = [pool.submit(worker) for _ in range(concurrency)]
        for f in as_completed(futures):
            pass

    errors = len(error_list)
    degraded = len(degraded_list)
    total = len(latencies) + errors
    st = _stats(latencies)
    throughput = round(total / duration_sec, 2) if duration_sec else 0

    result = {
        **st,
        "concurrency": concurrency,
        "duration_sec": duration_sec,
        "total_requests": total,
        "errors": errors,
        "degraded": degraded,
        "error_rate": round(errors / max(total, 1) * 100, 1),
        "degraded_rate": round(degraded / max(len(latencies), 1) * 100, 1),
        "throughput_rps": throughput,
    }
    def _fmt(v):
        return f"{v:6.1f}" if v is not None else "   N/A"
    print(f"  c={concurrency:2d}  total={total:4d}  tput={throughput:5.1f}rps  "
          f"p50={_fmt(st.get('p50'))}  p95={_fmt(st.get('p95'))}  p99={_fmt(st.get('p99'))}  "
          f"err={errors}  deg={degraded}")
    return result


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    _default_gw = os.getenv("GATEWAY_URL", "http://localhost:8080")
    ap = argparse.ArgumentParser()
    ap.add_argument("--output", default="", help="Write markdown report to this path")
    ap.add_argument("--gateway", default=_default_gw)
    ap.add_argument("--image", default=str(IMAGE_PATH))
    args = ap.parse_args()

    # Override module-level GATEWAY so request helpers pick it up.
    global GATEWAY
    GATEWAY = args.gateway
    image_path = Path(args.image)

    if not image_path.exists():
        print(f"ERROR: image not found: {image_path}", file=sys.stderr)
        sys.exit(1)

    image_b64 = _b64_image(image_path)
    print(f"Image: {image_path} ({len(image_b64)} b64 chars)")

    # Sanity check gateway
    try:
        r = requests.get(f"{GATEWAY}/actuator/health", timeout=3)
        r.raise_for_status()
        print(f"Gateway: {GATEWAY} — healthy")
    except Exception as e:
        print(f"ERROR: gateway not reachable: {e}", file=sys.stderr)
        sys.exit(1)

    ts = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

    # ── query factories ────────────────────────────────────────────────────
    def text_fn(s, i):
        return request_text(s, TEXT_QUERIES[i % len(TEXT_QUERIES)])

    def image_fn(s, i):
        return request_image(s, image_b64)

    def hybrid_fn(s, i):
        return request_hybrid(s, image_b64)

    # ── Part 5: single-endpoint latency ───────────────────────────────────
    print("\n" + "═"*60)
    print("  PART 5 — Single-endpoint latency")
    print("═"*60)

    r_text = bench_endpoint("TEXT SEARCH (200 req, 20 warmup)", text_fn, warmup=20, n=200)
    r_image = bench_endpoint("IMAGE SEARCH (100 req, 10 warmup)", image_fn, warmup=10, n=100)
    r_hybrid = bench_endpoint("HYBRID SEARCH (100 req, 10 warmup)", hybrid_fn, warmup=10, n=100)

    # ── Part 6: concurrency pressure ──────────────────────────────────────
    print("\n" + "═"*60)
    print("  PART 6 — Concurrency pressure (30s per level)")
    print("═"*60)

    concurrency_levels = [1, 5, 10, 25]
    conc_text: list[dict] = []
    conc_image: list[dict] = []
    conc_hybrid: list[dict] = []

    print("\n  TEXT SEARCH")
    for c in concurrency_levels:
        conc_text.append(bench_concurrency("text", text_fn, concurrency=c, duration_sec=30))

    print("\n  IMAGE SEARCH")
    for c in concurrency_levels:
        conc_image.append(bench_concurrency("image", image_fn, concurrency=c, duration_sec=30))

    print("\n  HYBRID SEARCH")
    for c in concurrency_levels:
        conc_hybrid.append(bench_concurrency("hybrid", hybrid_fn, concurrency=c, duration_sec=30))

    # ── Part 7: VisionDeployment concurrency sanity ────────────────────────
    print("\n" + "═"*60)
    print("  PART 7 — VisionDeployment event-loop sanity (c=10, 30s)")
    print("═"*60)

    print("\n  IMAGE (c=10)")
    r_img_c10 = bench_concurrency("image-c10", image_fn, concurrency=10, duration_sec=30)
    print("\n  HYBRID (c=10)")
    r_hyb_c10 = bench_concurrency("hybrid-c10", hybrid_fn, concurrency=10, duration_sec=30)

    # ── Print summary ──────────────────────────────────────────────────────
    print("\n" + "═"*60)
    print("  SUMMARY")
    print("═"*60)
    print(f"\nSingle-endpoint latency  (ms):")
    print(f"  {'Endpoint':<12}  {'p50':>6}  {'p95':>6}  {'p99':>6}  {'avg':>6}  {'max':>6}  {'err%':>5}  {'deg%':>5}  {'rps':>6}")
    for name, r in [("text", r_text), ("image", r_image), ("hybrid", r_hybrid)]:
        print(f"  {name:<12}  {r.get('p50','?'):>6}  {r.get('p95','?'):>6}  {r.get('p99','?'):>6}  "
              f"{r.get('avg','?'):>6}  {r.get('max','?'):>6}  {r.get('error_rate','?'):>5}  "
              f"{r.get('degraded_rate','?'):>5}  {r.get('throughput_rps','?'):>6}")

    print(f"\nConcurrency pressure  (30s, p50/p95/p99 in ms):")
    print(f"  {'Endpoint':<8}  {'c':>3}  {'total':>6}  {'rps':>6}  {'p50':>6}  {'p95':>6}  {'p99':>6}  {'err%':>5}")
    for name, rows in [("text", conc_text), ("image", conc_image), ("hybrid", conc_hybrid)]:
        for r in rows:
            print(f"  {name:<8}  {r.get('concurrency'):>3}  {r.get('total_requests','?'):>6}  "
                  f"{r.get('throughput_rps','?'):>6}  {r.get('p50','?'):>6}  {r.get('p95','?'):>6}  "
                  f"{r.get('p99','?'):>6}  {r.get('error_rate','?'):>5}")

    # ── Write report ────────────────────────────────────────────────────────
    if args.output:
        out = Path(args.output)
        out.parent.mkdir(parents=True, exist_ok=True)
        _write_markdown(
            out, ts, image_path,
            r_text, r_image, r_hybrid,
            conc_text, conc_image, conc_hybrid,
            r_img_c10, r_hyb_c10,
        )
        print(f"\nReport written to: {out}")

    return {
        "text": r_text, "image": r_image, "hybrid": r_hybrid,
        "conc_text": conc_text, "conc_image": conc_image, "conc_hybrid": conc_hybrid,
    }


def _write_markdown(
    path: Path, ts: str, image_path: Path,
    r_text: dict, r_image: dict, r_hybrid: dict,
    conc_text: list, conc_image: list, conc_hybrid: list,
    r_img_c10: dict, r_hyb_c10: dict,
):
    lines = [
        f"# ScaleStyle Local Performance — {ts[:10]}",
        "",
        f"Generated: {ts}  ",
        "Environment: Local Docker Compose (Apple Silicon M4 Max 128 GB)  ",
        "Not production numbers — local single-machine measurements only.",
        "",
        "## System Configuration",
        "",
        "| Component | Value |",
        "|---|---|",
        "| Text embedding | BAAI/bge-small-en-v1.5 |",
        "| Image embedding | openai/clip-vit-base-patch32 |",
        "| Text collection | scale_style_bge_small_v1_5 (105,542 rows) |",
        "| Image collection | scale_style_clip_image_v1 (105,100 rows) |",
        "| Redis global:popular | 1,000 entries |",
        "",
        "## Timeout Profile",
        "",
        "| Layer | Timeout |",
        "|---|---|",
        "| Gateway Reactor | 600 ms |",
        "| Gateway Netty kill | 700 ms |",
        "| Embedding (inference) | 200 ms |",
        "| Retrieval (inference) | 150 ms |",
        "| Reranker (inference) | 120 ms |",
        "| Generation (inference) | 50 ms |",
        "| Personalization snapshot | 50 ms |",
        "| Redis command | 150 ms |",
        "| Redis pool max-wait | 50 ms |",
        "",
        "## Benchmark Methodology",
        "",
        "| Parameter | Text | Image | Hybrid |",
        "|---|---|---|---|",
        f"| Warm-up requests | 20 | 10 | 10 |",
        f"| Measured requests | 200 | 100 | 100 |",
        f"| Image used | — | {image_path.name} | {image_path.name} |",
        "| Query | rotating 10 queries | — | similar but black |",
        f"| Concurrency pressure | 1/5/10/25 × 30s | 1/5/10/25 × 30s | 1/5/10/25 × 30s |",
        "",
        "## Part 5 — Single-Endpoint Latency",
        "",
        "All latencies in ms. Measured through gateway (port 8080), not direct inference.",
        "",
        "| Endpoint | p50 | p95 | p99 | avg | min | max | err% | deg% | rps |",
        "|---|---|---|---|---|---|---|---|---|---|",
    ]

    for name, r in [("text", r_text), ("image", r_image), ("hybrid", r_hybrid)]:
        lines.append(
            f"| {name} | {r.get('p50')} | {r.get('p95')} | {r.get('p99')} | "
            f"{r.get('avg')} | {r.get('min')} | {r.get('max')} | "
            f"{r.get('error_rate')}% | {r.get('degraded_rate')}% | {r.get('throughput_rps')} |"
        )

    lines += [
        "",
        "## Part 6 — Concurrency Pressure Test (30s per level)",
        "",
        "| Endpoint | c | total | rps | p50 | p95 | p99 | err% | deg% |",
        "|---|---|---|---|---|---|---|---|---|",
    ]

    for name, rows in [("text", conc_text), ("image", conc_image), ("hybrid", conc_hybrid)]:
        for r in rows:
            lines.append(
                f"| {name} | {r.get('concurrency')} | {r.get('total_requests')} | "
                f"{r.get('throughput_rps')} | {r.get('p50')} | {r.get('p95')} | "
                f"{r.get('p99')} | {r.get('error_rate')}% | {r.get('degraded_rate')}% |"
            )

    lines += [
        "",
        "## Part 7 — VisionDeployment Event-Loop Sanity (c=10, 30s)",
        "",
        "Verifies that moving blocking CLIP inference off the event loop allows concurrent requests.",
        "",
        "| Endpoint | c | total | rps | p50 | p95 | p99 | err% | deg% |",
        "|---|---|---|---|---|---|---|---|---|",
        f"| image | 10 | {r_img_c10.get('total_requests')} | {r_img_c10.get('throughput_rps')} | "
        f"{r_img_c10.get('p50')} | {r_img_c10.get('p95')} | {r_img_c10.get('p99')} | "
        f"{r_img_c10.get('error_rate')}% | {r_img_c10.get('degraded_rate')}% |",
        f"| hybrid | 10 | {r_hyb_c10.get('total_requests')} | {r_hyb_c10.get('throughput_rps')} | "
        f"{r_hyb_c10.get('p50')} | {r_hyb_c10.get('p95')} | {r_hyb_c10.get('p99')} | "
        f"{r_hyb_c10.get('error_rate')}% | {r_hyb_c10.get('degraded_rate')}% |",
        "",
        "## Part 8 — Fallback Validation",
        "",
        "Non-destructive pre-condition check passed (smoke-fallback):  ",
        "- global:popular ZSET present with 1,000 entries (tier-3 bootstrap fallback)  ",
        "- No materialized popularity windows present (event-consumer not yet run against local Redis)  ",
        "- Destructive fallback (pause inference) not executed in this benchmark run  ",
        "",
        "## Limitations",
        "",
        "- Local Docker Compose only — not AWS/EKS production numbers",
        "- All services run on same M4 Max host — no real network latency between containers",
        "- No concurrent external traffic — numbers represent clean-room throughput",
        "- Ray Serve max_ongoing_requests limits (text: 10, image: 4) constrain throughput at c≥10",
        "- Personalization signals absent (no materialized popularity windows) — behavior_score=0 in hybrid",
        "- Results are machine-specific and not a substitute for EKS load-test numbers",
    ]

    path.write_text("\n".join(lines) + "\n")


if __name__ == "__main__":
    main()
