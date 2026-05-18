# Runbook: Bootstrap Local Data (Milvus + Redis)

## Overview

`bootstrap_data.py` is the one-stop script that initialises all runtime
data stores from a pre-generated embedding parquet:

1. Exports product metadata to `product_metadata.json`
2. Loads item metadata hashes into Redis (`item:{id}` and `item:{id}:meta`)
3. Seeds the global popularity sorted set (`global:popular`)
4. Creates the Milvus collection, builds the IVF_FLAT index, and inserts vectors

**Run this after** generating (or downloading) a fresh embedding parquet.
See `docs/runbooks/regenerate-embeddings.md` if you need to generate the parquet first.

---

## Prerequisites

### 1. Raw data (embedding parquet)

```
data-pipeline/data/processed/article_embeddings_bge_small_v1_5_detail.parquet
```

If this file does not exist:

```bash
make generate-embeddings-smoke   # 100-row smoke test
# or:
make generate-embeddings-full    # full ~105K rows
```

### 2. Services running

```bash
docker-compose up -d milvus redis etcd minio
```

Wait for Milvus to become healthy (takes ~30s):

```bash
docker-compose ps   # milvus should be "healthy"
```

### 3. Python dependencies

```bash
cd data-pipeline && pip install -r requirements.txt
```

---

## Full bootstrap

```bash
python data-pipeline/src/bootstrap_data.py \
    --parquet data-pipeline/data/processed/article_embeddings_bge_small_v1_5_detail.parquet \
    --drop-existing
```

Or via Makefile:

```bash
make bootstrap-local-data
```

Expected output:

```
============================================================
ScaleStyle Data Bootstrap
============================================================
Redis:      localhost:6379
Milvus:     localhost:19530
Collection: scale_style_bge_small_v1_5
============================================================

Loading data from: data/processed/article_embeddings_bge_small_v1_5_detail.parquet
✓ Loaded 105126 rows, 14 columns

[1/4] Exporting metadata to JSON...
✓ Exported 105126 items to data-pipeline/data/processed/product_metadata.json

[2/4] Loading Redis data...
✓ Connected to Redis at localhost:6379
  ✓ Loaded 105126 item metadata
  ✓ Created global:popular (type=zset, count=1000)

[3/4] Loading Milvus data...
✓ Connected to Milvus at http://localhost:19530
  Collection:           scale_style_bge_small_v1_5
  Detected dimension:   384
  Expected dimension:   384
  Creating collection: scale_style_bge_small_v1_5
  ✓ Created index on bge_embedding
  Inserting 105126 vectors...
  ✓ Loaded collection into memory
  ✓ Collection contains 105126 entities

[4/4] Verifying data...
  ✓ Redis: 3 sample items found
  ✓ Redis: global:popular (type=zset, count=1000)
  ✓ Milvus: scale_style_bge_small_v1_5 (105126 vectors)

============================================================
✓ Bootstrap complete!
============================================================
```

---

## Skip flags

```bash
# Redis only (no Milvus)
python data-pipeline/src/bootstrap_data.py --skip-milvus

# Milvus only (no Redis)
python data-pipeline/src/bootstrap_data.py --skip-redis

# No final verification pass
python data-pipeline/src/bootstrap_data.py --no-verify
```

---

## Re-bootstrapping an existing collection

By default the script refuses to drop an existing Milvus collection:

```
❌ Milvus collection 'scale_style_bge_small_v1_5' already exists.
   Re-run with --drop-existing to drop and recreate it.
```

This is intentional — accidental drops during a live system are expensive.
To re-bootstrap deliberately:

```bash
python data-pipeline/src/bootstrap_data.py \
    --parquet data-pipeline/data/processed/article_embeddings_bge_small_v1_5_detail.parquet \
    --drop-existing
```

---

## Troubleshooting

| Symptom | Cause | Fix |
|---|---|---|
| `Dimension mismatch: parquet has 1024-dim` | Wrong parquet file (legacy BGE-large) | Use `article_embeddings_bge_small_v1_5_detail.parquet` |
| `No embedding column found` | Parquet has neither `bge_embedding` nor `embedding` | Regenerate with `generate_embeddings.py` |
| `Failed to connect to Milvus` | Milvus not running | `docker-compose up -d milvus etcd minio` |
| `Failed to connect to Redis` | Redis not running | `docker-compose up -d redis` |
| `Collection already exists` | Re-bootstrapping without flag | Add `--drop-existing` |
| Bootstrap hangs at vector insert | Milvus not healthy yet | Wait 30s and retry |

---

## After bootstrapping — smoke test the API

```bash
docker-compose up -d   # start all services (gateway, inference, etc.)

# Wait for inference service to become healthy (~60-120s for model loading)
docker-compose ps

# Smoke test
curl "http://localhost:8080/api/recommendation/search?query=black+dress&k=5" | jq .
```

A successful response returns 5 ranked recommendations with `article_id`, `product_name`, and `score`.
