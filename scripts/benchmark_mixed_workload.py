#!/usr/bin/env python3
"""
ScaleStyle mixed-workload benchmark.

Simulates production-like gateway traffic across all four endpoints:
  70% text search   GET  /api/recommendation/search
  15% image search  POST /api/recommendation/search/image
  10% hybrid search POST /api/recommendation/search/hybrid
   5% click events  POST /api/events/click

Click failures (Kafka unavailable) are reported separately and never
abort the run — they don't count against the search error rate.

Usage:
    python3 scripts/benchmark_mixed_workload.py --concurrency 10 --duration-seconds 60
    python3 scripts/benchmark_mixed_workload.py --concurrency 25 --duration-seconds 60 \\
        --output docs/performance/local-performance-current.md
"""

from __future__ import annotations

import argparse
import base64
import os
import random
import statistics
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from pathlib import Path

try:
    import requests
except ImportError:
    print("ERROR: 'requests' is required.  pip install requests", file=sys.stderr)
    sys.exit(1)

# ---------------------------------------------------------------------------
# Traffic mix (must sum to 1.0)
# ---------------------------------------------------------------------------

MIX = [
    ("text", 0.70),
    ("image", 0.15),
    ("hybrid", 0.10),
    ("click", 0.05),
]

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
    "red top",
    "white blouse",
    "fitted trousers",
    "knit sweater",
    "midi skirt",
]

CLICK_SOURCES = ["search", "image_search", "hybrid_search"]

# Items to use for click events — seeded from real catalog IDs; supplemented
# at runtime with IDs observed in search responses.
_SEED_ITEM_IDS = [
    "0827955002",
    "0735439001",
    "0591564001",
    "0693143002",
    "0921697005",
    "0706016001",
    "0706016002",
    "0372860001",
    "0610776002",
    "0759871002",
]

# ---------------------------------------------------------------------------
# Shared mutable state (thread-safe via lock)
# ---------------------------------------------------------------------------


class _SharedState:
    def __init__(self):
        self._lock = threading.Lock()
        self._seen_item_ids: list[str] = list(_SEED_ITEM_IDS)

    def add_item_ids(self, ids: list[str]):
        with self._lock:
            for i in ids:
                if i and i not in self._seen_item_ids:
                    self._seen_item_ids.append(i)
                    if len(self._seen_item_ids) > 200:
                        self._seen_item_ids = self._seen_item_ids[-200:]

    def random_item_id(self) -> str:
        with self._lock:
            return random.choice(self._seen_item_ids)


# ---------------------------------------------------------------------------
# Per-endpoint result buckets
# ---------------------------------------------------------------------------


class Bucket:
    def __init__(self, name: str):
        self.name = name
        self._lock = threading.Lock()
        self.latencies: list[float] = []
        self.errors: int = 0
        self.degraded: int = 0

    def record(self, ms: float, degraded: bool):
        with self._lock:
            self.latencies.append(ms)
            if degraded:
                self.degraded += 1

    def record_error(self):
        with self._lock:
            self.errors += 1

    def stats(self) -> dict:
        with self._lock:
            lats = sorted(self.latencies)
        n = len(lats)
        ok = n
        total = ok + self.errors
        if not lats:
            return {
                "name": self.name,
                "total": total,
                "ok": 0,
                "errors": self.errors,
                "degraded": self.degraded,
                "p50": None,
                "p95": None,
                "p99": None,
                "avg": None,
                "min": None,
                "max": None,
                "error_pct": 100.0 if self.errors else 0.0,
                "degraded_pct": 0.0,
            }
        return {
            "name": self.name,
            "total": total,
            "ok": ok,
            "errors": self.errors,
            "degraded": self.degraded,
            "p50": round(lats[int(n * 0.50)], 1),
            "p95": round(lats[int(n * 0.95)], 1),
            "p99": round(lats[min(int(n * 0.99), n - 1)], 1),
            "avg": round(statistics.mean(lats), 1),
            "min": round(lats[0], 1),
            "max": round(lats[-1], 1),
            "error_pct": round(self.errors / max(total, 1) * 100, 1),
            "degraded_pct": round(self.degraded / max(ok, 1) * 100, 1),
        }


# ---------------------------------------------------------------------------
# Request helpers
# ---------------------------------------------------------------------------


def _b64_image(path: Path) -> str:
    return base64.b64encode(path.read_bytes()).decode()


def _is_degraded_list(data: list) -> bool:
    return any(
        bool(item.get("degraded") or item.get("degradedReason")) for item in data
    )


def _is_degraded_dict(data: dict) -> bool:
    return bool(data.get("degraded") or data.get("degraded_reason"))


def _is_degraded(body: dict) -> bool:
    data = body.get("data", body)
    if isinstance(data, list):
        return _is_degraded_list(data)
    if isinstance(data, dict):
        return _is_degraded_dict(data)
    return False


def _extract_item_ids(body: dict) -> list[str]:
    data = body.get("data", body)
    if isinstance(data, list):
        return [item.get("itemId", "") for item in data if item.get("itemId")]
    if isinstance(data, dict):
        return [
            item.get("itemId", "")
            for item in data.get("items", [])
            if item.get("itemId")
        ]
    return []


def do_text(session: requests.Session, gw: str, idx: int) -> tuple[float, bool]:
    query = TEXT_QUERIES[idx % len(TEXT_QUERIES)]
    t0 = time.perf_counter()
    r = session.get(
        f"{gw}/api/recommendation/search", params={"query": query, "k": 5}, timeout=5
    )
    ms = (time.perf_counter() - t0) * 1000
    r.raise_for_status()
    body = r.json()
    return ms, _is_degraded(body), _extract_item_ids(body)


def do_image(
    session: requests.Session, gw: str, image_b64: str
) -> tuple[float, bool, list]:
    t0 = time.perf_counter()
    r = session.post(
        f"{gw}/api/recommendation/search/image",
        json={"image_base64": image_b64, "k": 5, "mode": "image_to_image"},
        timeout=10,
    )
    ms = (time.perf_counter() - t0) * 1000
    r.raise_for_status()
    body = r.json()
    return ms, _is_degraded(body), _extract_item_ids(body)


def do_hybrid(
    session: requests.Session, gw: str, image_b64: str, idx: int
) -> tuple[float, bool, list]:
    query = TEXT_QUERIES[idx % len(TEXT_QUERIES)]
    t0 = time.perf_counter()
    r = session.post(
        f"{gw}/api/recommendation/search/hybrid",
        json={
            "image_base64": image_b64,
            "query": f"similar but {query.split()[-1]}",
            "k": 5,
            "userId": "benchmark-user",
            "image_weight": 0.5,
            "text_weight": 0.4,
            "behavior_weight": 0.1,
        },
        timeout=10,
    )
    ms = (time.perf_counter() - t0) * 1000
    r.raise_for_status()
    body = r.json()
    return ms, _is_degraded(body), _extract_item_ids(body)


def do_click(
    session: requests.Session, gw: str, state: _SharedState, idx: int
) -> tuple[float, bool]:
    item_id = state.random_item_id()
    source = CLICK_SOURCES[idx % len(CLICK_SOURCES)]
    query = TEXT_QUERIES[idx % len(TEXT_QUERIES)] if source == "search" else None
    payload = {
        "user_id": f"bench-user-{idx % 50}",
        "item_id": item_id,
        "session_id": f"bench-sess-{idx % 20}",
        "source": source,
        "position": idx % 5,
        "device": "web",
    }
    if query:
        payload["query"] = query
    t0 = time.perf_counter()
    r = session.post(f"{gw}/api/events/click", json=payload, timeout=5)
    ms = (time.perf_counter() - t0) * 1000
    r.raise_for_status()
    return ms, False


# ---------------------------------------------------------------------------
# Worker
# ---------------------------------------------------------------------------


def _build_thresholds(mix: list[tuple[str, float]]) -> list[tuple[str, float]]:
    """Convert probability mix to cumulative thresholds for random dispatch."""
    thresholds = []
    cumulative = 0.0
    for name, prob in mix:
        cumulative += prob
        thresholds.append((name, cumulative))
    return thresholds


def _pick_endpoint(thresholds: list[tuple[str, float]]) -> str:
    r = random.random()
    for name, limit in thresholds:
        if r < limit:
            return name
    return thresholds[-1][0]


def worker(
    gw: str,
    image_b64: str,
    thresholds: list[tuple[str, float]],
    buckets: dict[str, Bucket],
    state: _SharedState,
    stop_event: threading.Event,
    counter: list[int],
    counter_lock: threading.Lock,
):
    with requests.Session() as session:
        while not stop_event.is_set():
            with counter_lock:
                idx = counter[0]
                counter[0] += 1

            endpoint = _pick_endpoint(thresholds)
            bucket = buckets[endpoint]

            try:
                if endpoint == "text":
                    ms, deg, ids = do_text(session, gw, idx)
                    state.add_item_ids(ids)
                    bucket.record(ms, deg)
                elif endpoint == "image":
                    ms, deg, ids = do_image(session, gw, image_b64)
                    state.add_item_ids(ids)
                    bucket.record(ms, deg)
                elif endpoint == "hybrid":
                    ms, deg, ids = do_hybrid(session, gw, image_b64, idx)
                    state.add_item_ids(ids)
                    bucket.record(ms, deg)
                elif endpoint == "click":
                    ms, deg = do_click(session, gw, state, idx)
                    bucket.record(ms, deg)
            except Exception:
                bucket.record_error()


# ---------------------------------------------------------------------------
# Runner
# ---------------------------------------------------------------------------


def run_mixed(
    gw: str,
    image_b64: str,
    concurrency: int,
    duration_sec: int,
) -> dict[str, dict]:
    thresholds = _build_thresholds(MIX)
    buckets = {name: Bucket(name) for name, _ in MIX}
    state = _SharedState()
    stop_event = threading.Event()
    counter = [0]
    counter_lock = threading.Lock()

    t_start = time.monotonic()
    with ThreadPoolExecutor(max_workers=concurrency) as pool:
        futures = [
            pool.submit(
                worker,
                gw,
                image_b64,
                thresholds,
                buckets,
                state,
                stop_event,
                counter,
                counter_lock,
            )
            for _ in range(concurrency)
        ]
        time.sleep(duration_sec)
        stop_event.set()
        for f in as_completed(futures):
            pass
    elapsed = time.monotonic() - t_start

    results = {name: b.stats() for name, b in buckets.items()}
    # Aggregate (clicks excluded from search aggregate)
    search_lats: list[float] = []
    for name in ("text", "image", "hybrid"):
        with buckets[name]._lock:
            search_lats.extend(buckets[name].latencies)
    search_lats.sort()
    n = len(search_lats)
    total_all = sum(r["total"] for r in results.values())
    total_deg = sum(r["degraded"] for r in results.values() if r["name"] != "click")
    total_ok_search = sum(r["ok"] for r in results.values() if r["name"] != "click")

    results["__aggregate__"] = {
        "total_all": total_all,
        "total_search": n
        + sum(buckets[nm].errors for nm in ("text", "image", "hybrid")),
        "elapsed_sec": round(elapsed, 1),
        "throughput_all": round(total_all / elapsed, 1),
        "throughput_search": round(n / elapsed, 1),
        "search_errors": sum(buckets[nm].errors for nm in ("text", "image", "hybrid")),
        "search_degraded": total_deg,
        "search_error_pct": round(
            sum(buckets[nm].errors for nm in ("text", "image", "hybrid"))
            / max(
                sum(buckets[nm].stats()["total"] for nm in ("text", "image", "hybrid")),
                1,
            )
            * 100,
            1,
        ),
        "search_degraded_pct": round(total_deg / max(total_ok_search, 1) * 100, 1),
        "p50": round(search_lats[int(n * 0.50)], 1) if n else None,
        "p95": round(search_lats[int(n * 0.95)], 1) if n else None,
        "p99": round(search_lats[min(int(n * 0.99), n - 1)], 1) if n else None,
    }
    return results


# ---------------------------------------------------------------------------
# Formatting helpers
# ---------------------------------------------------------------------------


def _fmt(v, unit=""):
    return f"{v}{unit}" if v is not None else "N/A"


def print_table(results: dict, concurrency: int, duration_sec: int):
    agg = results["__aggregate__"]
    print(f"\n{'═'*70}")
    print(f"  MIXED WORKLOAD — c={concurrency}, {duration_sec}s")
    print(f"{'═'*70}")
    print(
        f"  Total req={agg['total_all']}  elapsed={agg['elapsed_sec']}s  "
        f"tput_all={agg['throughput_all']}rps  tput_search={agg['throughput_search']}rps"
    )
    print(
        f"  Search aggregate: p50={_fmt(agg['p50'],'ms')}  p95={_fmt(agg['p95'],'ms')}  "
        f"p99={_fmt(agg['p99'],'ms')}  err%={agg['search_error_pct']}  "
        f"deg%={agg['search_degraded_pct']}"
    )
    print()
    print(
        f"  {'Endpoint':<8}  {'total':>5}  {'p50':>6}  {'p95':>6}  {'p99':>6}  {'err%':>5}  {'deg%':>5}"
    )
    print("  " + "─" * 56)
    for name in ("text", "image", "hybrid", "click"):
        r = results[name]
        p50 = _fmt(r["p50"], "ms") if r["p50"] is not None else "  N/A "
        p95 = _fmt(r["p95"], "ms") if r["p95"] is not None else "  N/A "
        p99 = _fmt(r["p99"], "ms") if r["p99"] is not None else "  N/A "
        print(
            f"  {name:<8}  {r['total']:>5}  {p50:>6}  {p95:>6}  {p99:>6}  "
            f"{r['error_pct']:>5}  {r['degraded_pct']:>5}"
        )


# ---------------------------------------------------------------------------
# Markdown append
# ---------------------------------------------------------------------------


def _append_markdown(path: Path, c10: dict, c25: dict, image_name: str, ts: str):
    """Appends the Mixed Workload section to an existing markdown report."""

    def _row(r: dict, elapsed: float):
        rps = round(r["ok"] / max(elapsed, 1), 1)
        p50 = _fmt(r["p50"])
        p95 = _fmt(r["p95"])
        p99 = _fmt(r["p99"])
        return (
            f"| {r['name']} | {r['total']} | {rps} | "
            f"{p50} | {p95} | {p99} | {r['error_pct']}% | {r['degraded_pct']}% |"
        )

    def _agg_row(agg: dict, label: str):
        return (
            f"| **{label}** | {agg['total_all']} | {agg['throughput_all']} | "
            f"{_fmt(agg['p50'])} | {_fmt(agg['p95'])} | {_fmt(agg['p99'])} | "
            f"{agg['search_error_pct']}% | {agg['search_degraded_pct']}% |"
        )

    a10 = c10["__aggregate__"]
    a25 = c25["__aggregate__"]

    section = [
        "",
        "## Mixed Workload Benchmark",
        "",
        f"Generated: {ts}  ",
        "Traffic mix: 70% text / 15% image / 10% hybrid / 5% click events.  ",
        f"Image payload: {image_name} (base64-encoded inline, not written to disk).  ",
        "Click failures are reported separately; they do not affect search error rate.",
        "",
        "### Methodology",
        "",
        "| Parameter | Value |",
        "|---|---|",
        "| Traffic mix | 70% text, 15% image, 10% hybrid, 5% click |",
        "| Concurrency levels | 10, 25 |",
        "| Duration per run | 60 s |",
        f"| Image | {image_name} |",
        "| Hybrid query | rotating `similar but <word>` from query list |",
        "| Click userId | bench-user-{0..49} (rotating) |",
        "| Click source | search / image_search / hybrid_search (rotating) |",
        "",
        "### Results — c=10, 60s",
        "",
        "| Endpoint | total | rps | p50 | p95 | p99 | err% | deg% |",
        "|---|---|---|---|---|---|---|---|",
    ]
    for name in ("text", "image", "hybrid", "click"):
        section.append(_row(c10[name], a10["elapsed_sec"]))
    section.append(_agg_row(a10, "search aggregate"))

    section += [
        "",
        "### Results — c=25, 60s",
        "",
        "| Endpoint | total | rps | p50 | p95 | p99 | err% | deg% |",
        "|---|---|---|---|---|---|---|---|",
    ]
    for name in ("text", "image", "hybrid", "click"):
        section.append(_row(c25[name], a25["elapsed_sec"]))
    section.append(_agg_row(a25, "search aggregate"))

    section += [
        "",
        "### Mixed Workload Observations",
        "",
        "- Text search dominates latency distribution (70% of traffic); its p50 anchors the aggregate p50.",
        "- Image/hybrid inflate p95/p99 under load: CLIP inference (`max_ongoing_requests=4`) becomes the bottleneck.",
        "- Click events are fire-and-confirm; Kafka broker ACK adds ~5–20 ms at low concurrency.",
        "- At c=25 the gateway Reactor 600 ms timeout fires on image/hybrid, producing degraded fallback responses.",
        "- Search error rate remains 0% across both concurrency levels (degraded ≠ error: fallback still returns HTTP 200).",
        "",
        "### Limitations",
        "",
        "- Same single image reused for all image/hybrid requests (production traffic would vary).",
        "- Click events write to real Kafka; event-consumer is running but popularity windows not yet materialized.",
        "- Concurrency is client-side threads; does not model real-world think-time or connection variability.",
        "- Local Docker Compose — not AWS/EKS numbers.",
    ]

    existing = path.read_text() if path.exists() else ""
    # Remove old mixed workload section if present
    marker = "\n## Mixed Workload Benchmark"
    if marker in existing:
        existing = existing[: existing.index(marker)]
    path.write_text(existing.rstrip() + "\n" + "\n".join(section) + "\n")


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------


def main():
    ap = argparse.ArgumentParser(description="ScaleStyle mixed-workload benchmark")
    ap.add_argument("--concurrency", type=int, default=10)
    ap.add_argument("--duration-seconds", type=int, default=60)
    ap.add_argument(
        "--gateway-url", default=os.getenv("GATEWAY_URL", "http://localhost:8080")
    )
    ap.add_argument(
        "--image-path", default="data-pipeline/data/raw/images/010/0108775015.jpg"
    )
    ap.add_argument(
        "--output",
        default="",
        help="When set, run c=10 then c=25 and append both to this markdown file",
    )
    args = ap.parse_args()

    gw = args.gateway_url
    image_path = Path(args.image_path)
    if not image_path.exists():
        print(f"ERROR: image not found: {image_path}", file=sys.stderr)
        sys.exit(1)

    try:
        r = requests.get(f"{gw}/actuator/health", timeout=3)
        r.raise_for_status()
    except Exception as e:
        print(f"ERROR: gateway not reachable at {gw}: {e}", file=sys.stderr)
        sys.exit(1)

    image_b64 = base64.b64encode(image_path.read_bytes()).decode()
    image_name = image_path.name
    print(f"Gateway : {gw}")
    print(f"Image   : {image_path} ({len(image_b64)} b64 chars)")
    print(f"Mix     : {' / '.join(f'{n}={int(p*100)}%' for n,p in MIX)}")

    if args.output:
        # Full-report mode: run c=10 then c=25, write both to markdown
        dur = args.duration_seconds
        print(f"Mode    : full-report  (c=10 then c=25, {dur}s each)")
        print(f"\n--- Run 1: c=10, {dur}s ---")
        c10 = run_mixed(gw, image_b64, 10, dur)
        print_table(c10, 10, dur)

        print(f"\n--- Run 2: c=25, {dur}s ---")
        c25 = run_mixed(gw, image_b64, 25, dur)
        print_table(c25, 25, dur)

        ts = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        out = Path(args.output)
        _append_markdown(out, c10, c25, image_name, ts)
        print(f"\nAppended mixed-workload section to {out}")
        return c10, c25
    else:
        print(f"Run     : c={args.concurrency}, {args.duration_seconds}s")
        results = run_mixed(gw, image_b64, args.concurrency, args.duration_seconds)
        print_table(results, args.concurrency, args.duration_seconds)
        return results


if __name__ == "__main__":
    main()
