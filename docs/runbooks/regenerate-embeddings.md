# Runbook: Regenerate Embeddings

## Overview

This runbook explains how to regenerate the embedding artifact that feeds
the ScaleStyle Milvus vector database.

**Active model:** `BAAI/bge-small-en-v1.5` (384-dim, CLS pooling, L2-normalised)  
**Active collection:** `scale_style_bge_small_v1_5`  
**Output artifact:** `data-pipeline/data/processed/article_embeddings_bge_small_v1_5_detail.parquet`

---

## Why the notebook is reference only

`generate_embeddings.ipynb` (repo root) is a Colab prototype. It:

- Requires a live Google Drive mount and Colab secrets
- Outputs column `embedding` instead of the canonical `bge_embedding`
- Is not reproducible without the exact Drive folder structure
- Used `BAAI/bge-large-en-v1.5` (1024-dim) — incompatible with the active collection

**Never use the notebook output to bootstrap the active `scale_style_bge_small_v1_5` collection.**
The formal pipeline is `data-pipeline/src/generate_embeddings.py`.

---

## Prerequisites

```bash
# Install data-pipeline dependencies (from project root)
cd data-pipeline
pip install torch --index-url https://download.pytorch.org/whl/cpu  # CPU-only
# OR:  pip install torch   (for CUDA-enabled GPU)
pip install -r requirements.txt
```

Ensure raw data exists:

```
data-pipeline/data/raw/articles.csv           # H&M article metadata
data-pipeline/data/raw/transactions_train.csv # Optional — for price enrichment
```

---

## Step 1 — Smoke test (100 articles)

Run a quick end-to-end check before committing to full generation:

```bash
cd /path/to/ScaleStyle
python data-pipeline/src/generate_embeddings.py --limit 100 --overwrite
```

Or via Makefile:

```bash
make generate-embeddings-smoke
```

Expected output:
```
✓ Embedding generation complete
  Model:   BAAI/bge-small-en-v1.5
  Device:  cpu  (dtype=torch.float32)
  Rows:    100
  Dim:     384
  Col:     bge_embedding
  Output:  data-pipeline/data/processed/article_embeddings_bge_small_v1_5_detail.parquet
  Sample[0]:  [0.012345, -0.023456, ...]
```

---

## Step 2 — Validate the smoke output

```bash
python data-pipeline/src/validate_embeddings.py \
    --input data-pipeline/data/processed/article_embeddings_bge_small_v1_5_detail.parquet \
    --expected-dim 384
```

Or via Makefile:

```bash
make validate-embeddings
```

All checks must pass before proceeding to full generation.

---

## Step 3 — Full embedding generation (local)

```bash
python data-pipeline/src/generate_embeddings.py --overwrite
```

With explicit paths:

```bash
python data-pipeline/src/generate_embeddings.py \
    --input  data-pipeline/data/raw/articles.csv \
    --transactions data-pipeline/data/raw/transactions_train.csv \
    --output data-pipeline/data/processed/article_embeddings_bge_small_v1_5_detail.parquet \
    --overwrite
```

The full H&M dataset (~105K articles) takes approximately:

| Hardware | Time |
|---|---|
| CPU (8-core) | ~15–30 min |
| GPU (T4 / A10) | ~2–5 min |

---

## Step 4 — Full generation in Colab (Google Drive)

Use this when local hardware is too slow. The output must be downloaded and
placed at the standard path before bootstrapping.

```python
# In Colab — install dependencies
!pip install torch transformers tqdm pandas pyarrow

# Mount Drive (if saving output there)
from google.colab import drive
drive.mount('/content/drive')

import subprocess
result = subprocess.run([
    "python", "/content/ScaleStyle/data-pipeline/src/generate_embeddings.py",
    "--input",  "/content/drive/MyDrive/ScaleStyle_Project/data/articles.csv",
    "--transactions", "/content/drive/MyDrive/ScaleStyle_Project/data/transactions_train.csv",
    "--output", "/content/drive/MyDrive/ScaleStyle_Project/data/processed/"
                "article_embeddings_bge_small_v1_5_detail.parquet",
    "--overwrite",
], check=True)
```

After generation, download the parquet and place it at:

```
data-pipeline/data/processed/article_embeddings_bge_small_v1_5_detail.parquet
```

---

## Step 5 — Validate the full artifact

```bash
python data-pipeline/src/validate_embeddings.py \
    --input data-pipeline/data/processed/article_embeddings_bge_small_v1_5_detail.parquet \
    --expected-dim 384
```

All of these checks must pass:

- ✓ File exists
- ✓ Parquet loaded: ~105,000 rows
- ✓ Required columns: article_id, bge_embedding
- ✓ No duplicate article_id
- ✓ No null embeddings
- ✓ Embedding dimension: 384 (expected 384)
- ✓ Sidecar metadata present

---

## Step 6 — Bootstrap Milvus and Redis

```bash
# docker-compose must be running (milvus + redis)
docker-compose up -d milvus redis

# Bootstrap
python data-pipeline/src/bootstrap_data.py \
    --parquet data-pipeline/data/processed/article_embeddings_bge_small_v1_5_detail.parquet \
    --drop-existing
```

Or via Makefile:

```bash
make bootstrap-local-data
```

---

## Avoiding BGE-large / BGE-small dimension mismatches

**Critical:** Never load a 1024-dim (BGE-large) parquet into the
`scale_style_bge_small_v1_5` collection — Milvus will reject the insert.

The safeguard is in `bootstrap_data.py`:

```
❌ Dimension mismatch: parquet has 1024-dim vectors but EXPECTED_EMBEDDING_DIM=384.
```

The embedding generation script also validates before writing:

```
AssertionError: Dimension mismatch: generated 1024-dim vectors, expected 384.
```

**If you see either error:**

1. Check which parquet file you are pointing to.
2. Look for `article_embeddings_bge_detail.parquet` — this is the legacy 1024-dim file.
3. Regenerate with `generate_embeddings.py` as described above.

---

## Artifact reference

| Item | Value |
|---|---|
| Active model | `BAAI/bge-small-en-v1.5` |
| Embedding dimension | 384 |
| Pooling | CLS (`last_hidden_state[:, 0]`) |
| Normalisation | L2 (unit length) |
| Embedding column | `bge_embedding` |
| Milvus collection | `scale_style_bge_small_v1_5` |
| Milvus index type | IVF_FLAT |
| Milvus metric | IP (inner product = cosine on unit vectors) |
| Output parquet | `data-pipeline/data/processed/article_embeddings_bge_small_v1_5_detail.parquet` |
| Sidecar metadata | `data-pipeline/data/processed/article_embeddings_bge_small_v1_5_detail.meta.json` |
| Legacy large parquet | `article_embeddings_bge_detail.parquet` — **do not use for new bootstraps** |
