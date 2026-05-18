"""
Embedding model latency / quality benchmark for the ScaleStyle pipeline.

Compares CPU-bound real-time *query* embedding cost across the BGE family
(large / base / small). Mirrors the production embedding code path used in
``src/deployments/embedding.py``:

* CLS-token pooling on ``last_hidden_state[:, 0]``
* L2 normalization (unit-length vectors, cosine == dot product)
* BGE asymmetric query prefix on queries, no prefix on documents

Outputs two artifacts under ``--output-dir`` (default ``docs/performance``):

* ``embedding-comparison.json`` — machine-readable raw results
* ``embedding-comparison.md``   — human-readable table + interpretation

Usage::

    # From the inference-service/ directory
    python -m benchmarks.embedding_comparison

    # Pick which models to run (default: all three)
    python -m benchmarks.embedding_comparison --models bge-large bge-small

    # Tune sample count and skip the top-k agreement section
    python -m benchmarks.embedding_comparison --runs 200 --no-topk

The benchmark is intentionally single-stream / single-replica. That matches the
production deployment shape: ``EmbeddingDeployment`` runs as a single Ray Serve
replica with ``max_ongoing_requests=4`` and uses ``asyncio.to_thread`` to wrap
the synchronous PyTorch forward pass — concurrent requests queue rather than
truly parallelize on CPU.

Why these three models?

* ``BAAI/bge-small-en-v1.5`` (33M, 384-dim)   — active production model; CPU default
* ``BAAI/bge-base-en-v1.5``  (109M, 768-dim)  — middle ground
* ``BAAI/bge-large-en-v1.5`` (335M, 1024-dim) — benchmark candidate for GPU environments

All three use identical architecture (BERT-derived) and identical pooling /
normalization, so we can swap them without changing inference code — only
``EMBEDDING_MODEL`` / ``EMBEDDING_DIMENSION`` env vars and the Milvus
collection. That's the architectural property this benchmark is meant to
exploit and validate.
"""

from __future__ import annotations

import argparse
import csv
import gc
import json
import os
import platform
import statistics
import sys
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Optional

import torch
import torch.nn.functional as F
from transformers import AutoModel, AutoTokenizer

# ---------------------------------------------------------------------------
# Constants — kept aligned with src/config.py:EmbeddingConfig
# ---------------------------------------------------------------------------

QUERY_PREFIX = "Represent this sentence for searching relevant passages:"
MAX_LENGTH = 512

MODELS: dict[str, dict] = {
    "bge-large": {
        "name": "BAAI/bge-large-en-v1.5",
        "params_m": 335,
        "dim": 1024,
    },
    "bge-base": {
        "name": "BAAI/bge-base-en-v1.5",
        "params_m": 109,
        "dim": 768,
    },
    "bge-small": {
        "name": "BAAI/bge-small-en-v1.5",
        "params_m": 33,
        "dim": 384,
    },
}

# Representative fashion-search queries. Length distribution roughly matches
# what we'd expect from H&M-style traffic: short navigational tail + a few
# more descriptive natural-language queries.
QUERIES: list[str] = [
    "black dress",
    "white t-shirt",
    "summer floral dress for women",
    "men's slim fit jeans dark wash",
    "warm wool coat",
    "kids striped pajamas",
    "running shoes",
    "leather jacket",
    "cotton bra",
    "wool scarf grey",
    "denim shorts",
    "linen shirt men",
    "ankle boots",
    "puffer jacket",
    "silk scarf",
    "graphic hoodie",
    "yoga leggings",
    "lace bralette",
    "kids winter beanie",
    "knit cardigan",
]


# ---------------------------------------------------------------------------
# Result types
# ---------------------------------------------------------------------------


@dataclass
class LatencyResult:
    key: str
    name: str
    params_m: int
    dim: int
    device: str
    dtype: str
    runs: int
    load_ms: float
    warmup_avg_ms: float
    p50_ms: float
    p95_ms: float
    p99_ms: float
    mean_ms: float
    min_ms: float
    max_ms: float
    qps_serial: float
    rss_increase_mb: float


@dataclass
class TopKAgreement:
    """Top-K retrieval overlap, treating bge-large as ground truth."""

    baseline_key: str
    candidate_key: str
    k: int
    n_queries: int
    n_articles: int
    mean_jaccard: float
    median_jaccard: float
    min_jaccard: float


@dataclass
class BenchmarkReport:
    timestamp: str
    host: dict
    config: dict
    latency: list[LatencyResult]
    topk: list[TopKAgreement] = field(default_factory=list)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def percentile(xs: list[float], p: float) -> float:
    """Linear-interpolation percentile. Avoids the numpy import."""
    if not xs:
        return float("nan")
    s = sorted(xs)
    k = (len(s) - 1) * p / 100
    f = int(k)
    c = min(f + 1, len(s) - 1)
    if f == c:
        return s[f]
    return s[f] + (s[c] - s[f]) * (k - f)


def get_rss_mb() -> float:
    """Return resident set size in MB. Falls back to 0.0 if psutil missing."""
    try:
        import psutil  # noqa: WPS433 — optional dep
    except ImportError:
        return 0.0
    return psutil.Process(os.getpid()).memory_info().rss / (1024 * 1024)


def host_info() -> dict:
    info = {
        "platform": platform.platform(),
        "machine": platform.machine(),
        "python": platform.python_version(),
        "torch": torch.__version__,
        "cuda_available": torch.cuda.is_available(),
        "cpu_count": os.cpu_count(),
    }
    if torch.cuda.is_available():
        info["cuda_device"] = torch.cuda.get_device_name(0)
    # Best-effort: physical core count (psutil), else logical
    try:
        import psutil

        info["cpu_physical"] = psutil.cpu_count(logical=False)
    except ImportError:
        pass
    return info


# ---------------------------------------------------------------------------
# Core benchmark
# ---------------------------------------------------------------------------


def _build_embedder(model_name: str, device: str, dtype: torch.dtype):
    tokenizer = AutoTokenizer.from_pretrained(model_name)
    model = AutoModel.from_pretrained(model_name, torch_dtype=dtype).to(device).eval()

    def embed(text: str, is_query: bool) -> torch.Tensor:
        prepared = f"{QUERY_PREFIX} {text}" if is_query else text
        inputs = tokenizer(
            [prepared],
            max_length=MAX_LENGTH,
            padding=True,
            truncation=True,
            return_tensors="pt",
        )
        inputs = {k: v.to(device) for k, v in inputs.items()}
        with torch.no_grad():
            outputs = model(**inputs)
            emb = outputs.last_hidden_state[:, 0]
            emb = F.normalize(emb, p=2, dim=1)
        return emb.float().cpu()

    return tokenizer, model, embed


def benchmark_one(
    key: str,
    runs: int,
    articles: Optional[list[str]] = None,
) -> tuple[LatencyResult, Optional[torch.Tensor], Optional[torch.Tensor]]:
    """Run a full benchmark for one model. Returns latency + (optionally)
    article and query embeddings for downstream top-K agreement."""
    info = MODELS[key]
    name = info["name"]
    device = "cuda" if torch.cuda.is_available() else "cpu"
    dtype = torch.float16 if device == "cuda" else torch.float32

    rss_before = get_rss_mb()

    print(f"\n[{key}] Loading {name} on {device} ({dtype})…", flush=True)
    t0 = time.perf_counter()
    tokenizer, model, embed = _build_embedder(name, device, dtype)
    load_ms = (time.perf_counter() - t0) * 1000
    rss_after = get_rss_mb()
    print(f"[{key}] Loaded in {load_ms:.0f} ms (RSS +{rss_after - rss_before:.0f} MB)")

    # Warmup — first inference includes lazy-init costs (kernel selection,
    # MKL, etc). Measure but do not include in steady-state stats.
    warmup_runs = 3
    print(f"[{key}] Warming up ({warmup_runs} runs)…")
    warmups = []
    for _ in range(warmup_runs):
        t0 = time.perf_counter()
        _ = embed("warmup query", True)
        warmups.append((time.perf_counter() - t0) * 1000)
    warmup_avg = statistics.mean(warmups)
    print(
        f"[{key}] Warmup avg: {warmup_avg:.1f} ms (runs: {[f'{w:.0f}' for w in warmups]})"
    )

    # Steady-state measurement
    print(f"[{key}] Measuring {runs} query embeds…")
    latencies = []
    for i in range(runs):
        q = QUERIES[i % len(QUERIES)]
        t0 = time.perf_counter()
        _ = embed(q, True)
        latencies.append((time.perf_counter() - t0) * 1000)

    p50 = percentile(latencies, 50)
    p95 = percentile(latencies, 95)
    p99 = percentile(latencies, 99)
    mean = statistics.mean(latencies)
    print(
        f"[{key}] p50={p50:.1f}ms  p95={p95:.1f}ms  p99={p99:.1f}ms  "
        f"mean={mean:.1f}ms  qps≈{1000.0 / mean:.1f}"
    )

    # Optional: embed corpus + queries for top-K agreement check
    article_embs = None
    query_embs = None
    if articles:
        print(f"[{key}] Embedding {len(articles)} articles for top-K agreement…")
        t0 = time.perf_counter()
        # Batch in groups of 8 to avoid blowing memory on bge-large
        batch_size = 8
        article_chunks = []
        for i in range(0, len(articles), batch_size):
            batch = articles[i : i + batch_size]
            inputs = tokenizer(
                batch,
                max_length=MAX_LENGTH,
                padding=True,
                truncation=True,
                return_tensors="pt",
            )
            inputs = {k: v.to(device) for k, v in inputs.items()}
            with torch.no_grad():
                outs = model(**inputs)
                emb = outs.last_hidden_state[:, 0]
                emb = F.normalize(emb, p=2, dim=1)
            article_chunks.append(emb.float().cpu())
        article_embs = torch.cat(article_chunks, dim=0)
        article_ms = (time.perf_counter() - t0) * 1000
        print(f"[{key}] Article corpus embedded in {article_ms:.0f} ms")

        # Embed all queries once for agreement check (with query prefix)
        query_embs = torch.cat([embed(q, True) for q in QUERIES], dim=0)

    # Free model memory before next iteration
    del model, tokenizer, embed
    gc.collect()
    if torch.cuda.is_available():
        torch.cuda.empty_cache()

    return (
        LatencyResult(
            key=key,
            name=name,
            params_m=info["params_m"],
            dim=info["dim"],
            device=device,
            dtype=str(dtype).replace("torch.", ""),
            runs=runs,
            load_ms=load_ms,
            warmup_avg_ms=warmup_avg,
            p50_ms=p50,
            p95_ms=p95,
            p99_ms=p99,
            mean_ms=mean,
            min_ms=min(latencies),
            max_ms=max(latencies),
            qps_serial=1000.0 / mean,
            rss_increase_mb=rss_after - rss_before,
        ),
        article_embs,
        query_embs,
    )


def compute_topk_agreement(
    baseline_key: str,
    candidate_key: str,
    article_baseline: torch.Tensor,
    query_baseline: torch.Tensor,
    article_candidate: torch.Tensor,
    query_candidate: torch.Tensor,
    k: int,
) -> TopKAgreement:
    """Jaccard overlap between top-K article indices retrieved by two models.

    Caveat: this is a relative agreement metric — it does NOT measure absolute
    retrieval quality (no ground-truth relevance labels). It tells you whether
    swapping models would dramatically reorder candidate sets. Combined with
    the cross-encoder reranker over top 50, even moderate disagreement at the
    retrieval stage is usually absorbed."""
    # Cosine sim == dot product, since vectors are L2-normalized
    sim_a = (query_baseline @ article_baseline.T).numpy()
    sim_b = (query_candidate @ article_candidate.T).numpy()
    n_q, n_a = sim_a.shape

    overlaps = []
    for qi in range(n_q):
        # argpartition would be faster but argsort keeps the script
        # numpy-light. n_a is small (a few hundred).
        topk_a = set(sim_a[qi].argsort()[::-1][:k].tolist())
        topk_b = set(sim_b[qi].argsort()[::-1][:k].tolist())
        overlaps.append(len(topk_a & topk_b) / k)

    return TopKAgreement(
        baseline_key=baseline_key,
        candidate_key=candidate_key,
        k=k,
        n_queries=n_q,
        n_articles=n_a,
        mean_jaccard=statistics.mean(overlaps),
        median_jaccard=statistics.median(overlaps),
        min_jaccard=min(overlaps),
    )


# ---------------------------------------------------------------------------
# Article corpus loader (H&M articles.csv)
# ---------------------------------------------------------------------------


def load_articles(csv_path: Path, n: int = 200) -> list[str]:
    """Pull the first ``n`` distinct ``prod_name + detail_desc`` strings from
    the H&M articles.csv. Used as a synthetic retrieval corpus for the top-K
    agreement check. We dedupe on detail_desc because product variants share
    the same description across colors/sizes."""
    seen_desc: set[str] = set()
    texts: list[str] = []
    with open(csv_path, encoding="utf-8") as f:
        reader = csv.DictReader(f)
        for row in reader:
            desc = (row.get("detail_desc") or "").strip()
            name = (row.get("prod_name") or "").strip()
            if not desc or not name or desc in seen_desc:
                continue
            seen_desc.add(desc)
            texts.append(f"{name}. {desc}")
            if len(texts) >= n:
                break
    return texts


# ---------------------------------------------------------------------------
# Markdown rendering
# ---------------------------------------------------------------------------


def render_markdown(report: BenchmarkReport) -> str:
    lat = report.latency
    lines: list[str] = []
    lines.append("# Embedding Model Comparison")
    lines.append("")
    lines.append(f"_Generated: {report.timestamp}_")
    lines.append("")
    lines.append("## Host")
    lines.append("")
    for k, v in report.host.items():
        lines.append(f"- **{k}**: `{v}`")
    lines.append("")
    lines.append("## Configuration")
    lines.append("")
    for k, v in report.config.items():
        lines.append(f"- **{k}**: `{v}`")
    lines.append("")
    lines.append("## Per-Query Latency (single-stream)")
    lines.append("")
    lines.append(
        "| Model | Params | Dim | Load (ms) | p50 (ms) | p95 (ms) | "
        "p99 (ms) | Mean (ms) | QPS (1-thread) | RSS Δ (MB) |"
    )
    lines.append("|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|")
    for r in lat:
        lines.append(
            f"| `{r.name}` | {r.params_m}M | {r.dim} | "
            f"{r.load_ms:.0f} | {r.p50_ms:.1f} | {r.p95_ms:.1f} | "
            f"{r.p99_ms:.1f} | {r.mean_ms:.1f} | "
            f"{r.qps_serial:.1f} | {r.rss_increase_mb:.0f} |"
        )
    lines.append("")

    if report.topk:
        lines.append("## Top-K Retrieval Agreement vs `bge-large`")
        lines.append("")
        lines.append(
            "Treating `bge-large` as the reference, we embed a corpus of "
            f"{report.topk[0].n_articles} H&M articles and {report.topk[0].n_queries} "
            "fashion queries with each model and measure how much the top-K "
            "retrieval set overlaps. This is **not** an absolute quality "
            "metric (no human-labeled relevance), but it tells us how much "
            "candidate-set churn a model swap would introduce — most of "
            "which gets absorbed by the downstream cross-encoder reranker."
        )
        lines.append("")
        lines.append("| Candidate | k | Mean Jaccard | Median | Min |")
        lines.append("|---|---:|---:|---:|---:|")
        for t in report.topk:
            lines.append(
                f"| `{t.candidate_key}` | {t.k} | "
                f"{t.mean_jaccard:.2f} | {t.median_jaccard:.2f} | "
                f"{t.min_jaccard:.2f} |"
            )
        lines.append("")

    # Latency-budget interpretation. Hardcoded budget numbers come from
    # src/config.py: embed=500ms, retrieval=300ms, rerank=250ms, gateway 600ms Reactor deadline.
    lines.append("## Interpretation: latency budget fit")
    lines.append("")
    lines.append(
        "Production budget (`src/config.py`): `EMBEDDING_TIMEOUT_MS=500`, "
        "`RETRIEVAL_TIMEOUT_MS=300`, `RERANKER_TIMEOUT_MS=250`, with a "
        "600ms Reactor deadline at the gateway. The full serial path on "
        "CPU is dominated by embedding + retrieval, so query embedding p99 "
        "is the gating signal."
    )
    lines.append("")
    lines.append(
        "| Model | p99 embed (ms) | Fits 600ms Reactor deadline? | Headroom for retrieval+rerank |"
    )
    lines.append("|---|---:|:---:|---:|")
    for r in lat:
        gateway_budget = 600
        # Subtract a small reserve for gateway-internal work (≈30ms)
        retrieval_rerank_floor = 50  # reasonable warm-cache floor
        headroom = gateway_budget - r.p99_ms - retrieval_rerank_floor
        fits = "yes" if headroom > 0 else "no"
        lines.append(f"| `{r.name}` | {r.p99_ms:.0f} | {fits} | {headroom:.0f} ms |")
    lines.append("")
    lines.append(
        "_Headroom = 600ms − p99(embed) − 50ms (warm retrieval+rerank floor). "
        "Negative headroom means the full pipeline cannot complete within the "
        "gateway Reactor deadline on this hardware; the system will rely on the "
        "circuit-breaker → popularity fallback path under load._"
    )
    lines.append("")

    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Benchmark BGE embedding models on the production code path.",
    )
    parser.add_argument(
        "--models",
        nargs="+",
        choices=list(MODELS.keys()),
        default=list(MODELS.keys()),
        help="Models to benchmark (default: all).",
    )
    parser.add_argument(
        "--runs",
        type=int,
        default=100,
        help="Steady-state samples per model (default: 100).",
    )
    parser.add_argument(
        "--articles-csv",
        type=Path,
        default=Path(__file__).resolve().parents[2]
        / "data-pipeline"
        / "data"
        / "raw"
        / "articles.csv",
        help="Path to H&M articles.csv for the top-K agreement section.",
    )
    parser.add_argument(
        "--n-articles",
        type=int,
        default=200,
        help="Articles to embed for top-K agreement (default: 200).",
    )
    parser.add_argument(
        "--topk",
        type=int,
        default=10,
        help="K for top-K agreement (default: 10).",
    )
    parser.add_argument(
        "--no-topk",
        action="store_true",
        help="Skip the top-K agreement section (faster).",
    )
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=Path(__file__).resolve().parents[2] / "docs" / "performance",
        help="Where to write the JSON + Markdown report.",
    )
    args = parser.parse_args()

    # Decide whether to load the article corpus. We need it iff we plan to do
    # top-K agreement AND we have at least 2 models AND the CSV exists.
    do_topk = not args.no_topk and len(args.models) >= 2 and args.articles_csv.exists()
    articles: Optional[list[str]] = None
    if do_topk:
        try:
            articles = load_articles(args.articles_csv, n=args.n_articles)
            if len(articles) < 10:
                print(
                    f"Loaded only {len(articles)} articles; skipping top-K agreement.",
                    file=sys.stderr,
                )
                articles = None
                do_topk = False
            else:
                print(f"Loaded {len(articles)} articles from {args.articles_csv}")
        except Exception as e:  # noqa: BLE001
            print(
                f"Could not load articles: {e}; skipping top-K agreement.",
                file=sys.stderr,
            )
            articles = None
            do_topk = False
    elif not args.no_topk:
        print(
            f"Skipping top-K agreement: "
            f"models={len(args.models)}, csv_exists={args.articles_csv.exists()}",
            file=sys.stderr,
        )

    # Run benchmarks
    latency_results: list[LatencyResult] = []
    article_embs_by_key: dict[str, torch.Tensor] = {}
    query_embs_by_key: dict[str, torch.Tensor] = {}

    for key in args.models:
        result, art_emb, q_emb = benchmark_one(
            key=key,
            runs=args.runs,
            articles=articles if do_topk else None,
        )
        latency_results.append(result)
        if art_emb is not None and q_emb is not None:
            article_embs_by_key[key] = art_emb
            query_embs_by_key[key] = q_emb

    # Top-K agreement: compare every non-baseline against bge-large if present
    topk_results: list[TopKAgreement] = []
    baseline_key = (
        "bge-large"
        if "bge-large" in article_embs_by_key
        else (next(iter(article_embs_by_key)) if article_embs_by_key else None)
    )
    if baseline_key and len(article_embs_by_key) >= 2:
        for key in article_embs_by_key:
            if key == baseline_key:
                continue
            topk_results.append(
                compute_topk_agreement(
                    baseline_key=baseline_key,
                    candidate_key=key,
                    article_baseline=article_embs_by_key[baseline_key],
                    query_baseline=query_embs_by_key[baseline_key],
                    article_candidate=article_embs_by_key[key],
                    query_candidate=query_embs_by_key[key],
                    k=args.topk,
                )
            )

    report = BenchmarkReport(
        timestamp=time.strftime("%Y-%m-%d %H:%M:%S %Z"),
        host=host_info(),
        config={
            "runs_per_model": args.runs,
            "n_queries": len(QUERIES),
            "max_length": MAX_LENGTH,
            "query_prefix": QUERY_PREFIX,
            "topk_k": args.topk if do_topk else None,
            "topk_n_articles": len(articles) if articles else None,
        },
        latency=latency_results,
        topk=topk_results,
    )

    # Write artifacts
    args.output_dir.mkdir(parents=True, exist_ok=True)
    json_path = args.output_dir / "embedding-comparison.json"
    md_path = args.output_dir / "embedding-comparison.md"

    json_payload = {
        "timestamp": report.timestamp,
        "host": report.host,
        "config": report.config,
        "latency": [asdict(r) for r in report.latency],
        "topk": [asdict(t) for t in report.topk],
    }
    json_path.write_text(json.dumps(json_payload, indent=2))
    md_path.write_text(render_markdown(report))

    print()
    print(f"Wrote {json_path}")
    print(f"Wrote {md_path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
