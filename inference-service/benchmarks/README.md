# Benchmarks

Standalone microbenchmarks for inference components. These do **not** require
the full Ray Serve / Milvus / Redis stack — they exercise model code in
isolation so you can compare configurations on commodity hardware before
committing to a deploy-config change.

## `embedding_comparison.py`

Compares query-embedding latency and top-K retrieval agreement across the BGE
family (`bge-large` / `bge-base` / `bge-small`). Mirrors the production code
path in `src/deployments/embedding.py` (CLS pooling, L2 normalization, BGE
asymmetric query prefix).

### Run

```bash
# From the inference-service/ directory
cd inference-service
pip install -r requirements.txt
python -m benchmarks.embedding_comparison
```

Or from the repo root via the Makefile target:

```bash
make benchmark-embeddings
```

### Outputs

Writes two artifacts to `docs/performance/`:

- `embedding-comparison.json` — raw machine-readable results (commit this)
- `embedding-comparison.md`   — human-readable table + budget interpretation

### Common variations

```bash
# Fast smoke run
python -m benchmarks.embedding_comparison --runs 30 --no-topk

# Just compare two models
python -m benchmarks.embedding_comparison --models bge-large bge-small

# Larger corpus for the top-K agreement check
python -m benchmarks.embedding_comparison --n-articles 500 --topk 20
```

### What this benchmark answers

1. **Can the embedding stage fit the gateway latency budget on this hardware?**
   The interpretation table in the markdown report subtracts p99 embed latency
   from the 600ms Reactor deadline and tells you the headroom for retrieval +
   reranking. Negative headroom → the popularity fallback path will dominate
   under load.

2. **How much candidate-set churn would a model swap introduce?** The top-K
   agreement section embeds a subset of the H&M articles corpus with each
   model and reports the Jaccard overlap of top-K results vs `bge-large`. High
   overlap (>0.7) means a swap is "safe" in the sense that the downstream
   cross-encoder reranker will absorb most of the disagreement.

### What it intentionally does not measure

- **Absolute retrieval quality.** No human relevance labels — only relative
  agreement between models. For absolute quality you need an offline eval
  with click-through or curated relevance data.
- **Concurrent throughput.** The deployment is single-replica with
  `max_ongoing_requests=4` and uses `asyncio.to_thread` to wrap the
  synchronous forward pass — concurrent requests queue rather than truly
  parallelize on CPU. Single-stream latency is the right number for the
  gateway-budget question.
- **GPU vs CPU comparison.** The script auto-detects CUDA but doesn't
  intentionally compare. Run it twice (once on a CPU node, once on a GPU
  node) and diff the results if you need that.
