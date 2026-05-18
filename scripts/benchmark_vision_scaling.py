#!/usr/bin/env python3
"""
VisionDeployment max_ongoing_requests scaling experiment.

Runs image and hybrid at c=10 and c=25 for 30s each, returning
clean per-run stats for comparison across concurrency configs.

Usage:
    python3 scripts/benchmark_vision_scaling.py --label "max_req=4"
    python3 scripts/benchmark_vision_scaling.py --label "max_req=6"
    python3 scripts/benchmark_vision_scaling.py --label "max_req=8"
"""

from __future__ import annotations

import argparse
import base64
import os
import statistics
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

try:
    import requests
except ImportError:
    print("pip install requests", file=sys.stderr)
    sys.exit(1)

GATEWAY = os.getenv("GATEWAY_URL", "http://localhost:8080")
DEFAULT_IMAGE = "data-pipeline/data/raw/images/010/0108775015.jpg"
HYBRID_QUERY = "similar but black"

TEXT_QUERIES = [
    "black dress", "summer dress", "casual shirt", "denim jeans", "winter coat",
    "sport jacket", "elegant blouse", "oversized hoodie", "floral skirt", "leather boots",
]


# ---------------------------------------------------------------------------

class _Bucket:
    def __init__(self):
        self._lock = threading.Lock()
        self.lats: list[float] = []
        self.errors = 0
        self.degraded = 0

    def record(self, ms: float, deg: bool):
        with self._lock:
            self.lats.append(ms)
            if deg:
                self.degraded += 1

    def error(self):
        with self._lock:
            self.errors += 1

    def stats(self) -> dict:
        with self._lock:
            lats = sorted(self.lats)
        n = len(lats)
        total = n + self.errors
        if not lats:
            return dict(total=total, ok=0, errors=self.errors, degraded=self.degraded,
                        p50=None, p95=None, p99=None, avg=None, rps=None, err_pct=100.0, deg_pct=0.0)
        return dict(
            total=total, ok=n, errors=self.errors, degraded=self.degraded,
            p50=round(lats[int(n * 0.50)], 1),
            p95=round(lats[int(n * 0.95)], 1),
            p99=round(lats[min(int(n * 0.99), n - 1)], 1),
            avg=round(statistics.mean(lats), 1),
            rps=None,  # filled after run
            err_pct=round(self.errors / max(total, 1) * 100, 1),
            deg_pct=round(self.degraded / max(n, 1) * 100, 1),
        )


def _is_degraded(body: dict) -> bool:
    data = body.get("data", body)
    if isinstance(data, list):
        return any(bool(i.get("degraded") or i.get("degradedReason")) for i in data)
    if isinstance(data, dict):
        return bool(data.get("degraded") or data.get("degraded_reason"))
    return False


def _worker_image(gw: str, image_b64: str, bucket: _Bucket, stop: threading.Event):
    with requests.Session() as sess:
        while not stop.is_set():
            try:
                t0 = time.perf_counter()
                r = sess.post(f"{gw}/api/recommendation/search/image",
                              json={"image_base64": image_b64, "k": 5, "mode": "image_to_image"},
                              timeout=10)
                ms = (time.perf_counter() - t0) * 1000
                r.raise_for_status()
                bucket.record(ms, _is_degraded(r.json()))
            except Exception:
                bucket.error()


def _worker_hybrid(gw: str, image_b64: str, bucket: _Bucket, stop: threading.Event, counter: list):
    with requests.Session() as sess:
        while not stop.is_set():
            with threading.Lock():
                idx = counter[0]
                counter[0] += 1
            query = TEXT_QUERIES[idx % len(TEXT_QUERIES)]
            try:
                t0 = time.perf_counter()
                r = sess.post(f"{gw}/api/recommendation/search/hybrid",
                              json={
                                  "image_base64": image_b64,
                                  "query": f"similar but {query.split()[-1]}",
                                  "k": 5,
                                  "userId": "vision-bench-user",
                                  "image_weight": 0.5,
                                  "text_weight": 0.4,
                                  "behavior_weight": 0.1,
                              },
                              timeout=10)
                ms = (time.perf_counter() - t0) * 1000
                r.raise_for_status()
                bucket.record(ms, _is_degraded(r.json()))
            except Exception:
                bucket.error()


def run_endpoint(endpoint: str, concurrency: int, duration: int,
                 gw: str, image_b64: str) -> dict:
    bucket = _Bucket()
    stop = threading.Event()
    counter = [0]
    counter_lock = threading.Lock()

    if endpoint == "image":
        target = _worker_image
        worker_args = (gw, image_b64, bucket, stop)
    else:
        target = _worker_hybrid
        worker_args = (gw, image_b64, bucket, stop, counter)

    t_start = time.monotonic()
    with ThreadPoolExecutor(max_workers=concurrency) as pool:
        futures = [pool.submit(target, *worker_args) for _ in range(concurrency)]
        time.sleep(duration)
        stop.set()
        for f in as_completed(futures):
            pass
    elapsed = time.monotonic() - t_start

    st = bucket.stats()
    st["rps"] = round(st["ok"] / elapsed, 1) if st["ok"] else 0.0
    st["elapsed"] = round(elapsed, 1)
    return st


def _fmt(v):
    return f"{v}" if v is not None else "N/A"


def print_result(label: str, endpoint: str, c: int, st: dict):
    print(f"  [{label}] {endpoint} c={c:>2}  "
          f"total={st['total']:>4}  rps={st['rps']:>5}  "
          f"p50={_fmt(st['p50']):>6}ms  p95={_fmt(st['p95']):>6}ms  p99={_fmt(st['p99']):>6}ms  "
          f"err={st['err_pct']:>4}%  deg={st['deg_pct']:>5}%")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--label", default="unlabeled", help="Config label e.g. max_req=4")
    ap.add_argument("--gateway-url", default=GATEWAY)
    ap.add_argument("--image-path", default=DEFAULT_IMAGE)
    ap.add_argument("--duration", type=int, default=30, help="Seconds per run")
    ap.add_argument("--warmup", type=int, default=5, help="Warmup seconds")
    args = ap.parse_args()

    gw = args.gateway_url
    img_path = Path(args.image_path)
    if not img_path.exists():
        print(f"ERROR: image not found: {img_path}", file=sys.stderr)
        sys.exit(1)

    try:
        r = requests.get(f"{gw}/actuator/health", timeout=3)
        r.raise_for_status()
    except Exception as e:
        print(f"ERROR: gateway not reachable: {e}", file=sys.stderr)
        sys.exit(1)

    image_b64 = base64.b64encode(img_path.read_bytes()).decode()
    print(f"\n{'═'*80}")
    print(f"  VisionDeployment scaling experiment — {args.label}")
    print(f"  Duration={args.duration}s  Warmup={args.warmup}s  Image={img_path.name}")
    print(f"{'═'*80}")

    results = {}

    for c in (10, 25):
        for ep in ("image", "hybrid"):
            key = f"{ep}_c{c}"
            if args.warmup > 0:
                print(f"  warming up {ep} c={c} ({args.warmup}s)...", end=" ", flush=True)
                run_endpoint(ep, c, args.warmup, gw, image_b64)
                print("done")
            print(f"  measuring {ep} c={c} ({args.duration}s)...", end=" ", flush=True)
            st = run_endpoint(ep, c, args.duration, gw, image_b64)
            results[key] = st
            print("done")
            print_result(args.label, ep, c, st)

    print(f"\n{'─'*80}")
    print("  SUMMARY")
    print(f"{'─'*80}")
    for key, st in results.items():
        ep, c_str = key.split("_c")
        print_result(args.label, ep, int(c_str), st)

    return results


if __name__ == "__main__":
    main()
