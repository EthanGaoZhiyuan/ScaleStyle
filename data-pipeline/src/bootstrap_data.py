#!/usr/bin/env python3
"""
ScaleStyle Data Bootstrap Script

One-stop script to initialize all data stores for ScaleStyle:
1. Export metadata to JSON
2. Load Redis metadata (item:* hashes)
3. Load Redis popularity (global:popular ZSET)
4. Initialize Milvus collection and load vectors

Usage:
    python bootstrap_data.py [--parquet PATH] [--skip-milvus] [--skip-redis]

Examples:
    # Full initialization with default parquet
    python bootstrap_data.py

    # Use specific parquet file
    python bootstrap_data.py --parquet data/processed/article_embeddings_bge_detail.parquet

    # Skip Milvus (only load Redis)
    python bootstrap_data.py --skip-milvus

Environment Variables:
    REDIS_HOST, REDIS_PORT: Redis connection (default: localhost:6379)
    MILVUS_HOST, MILVUS_PORT: Milvus connection (default: localhost:19530)
    MILVUS_COLLECTION: Collection name (default: scale_style_bge_v2)
"""

import argparse
import json
import os
import sys
from pathlib import Path

import pandas as pd
import redis
from pymilvus import (
    Collection,
    CollectionSchema,
    DataType,
    FieldSchema,
    MilvusClient,
    connections,
)
from tqdm import tqdm

# Default configuration (can be overridden by environment variables)
REDIS_HOST = os.getenv("REDIS_HOST", "localhost")
REDIS_PORT = int(os.getenv("REDIS_PORT", "6379"))
MILVUS_HOST = os.getenv("MILVUS_HOST", "localhost")
MILVUS_PORT = os.getenv("MILVUS_PORT", "19530")
MILVUS_COLLECTION = os.getenv("MILVUS_COLLECTION", "scale_style_bge_v2")
POPULARITY_KEY = "global:popular"
POPULARITY_TOPN = 1000

# Default parquet paths (try in order)
DEFAULT_PARQUET_PATHS = [
    "data/processed/article_embeddings_bge_detail.parquet",  # Has prod_name - preferred
    "data/processed/article_embeddings_bge_v2.parquet",
    "data-pipeline/data/processed/article_embeddings_bge_detail.parquet",
    "data-pipeline/data/processed/article_embeddings_bge_v2.parquet",
]


def find_parquet_file(custom_path=None):
    """Find parquet file in order of preference"""
    if custom_path:
        if Path(custom_path).exists():
            return custom_path
        else:
            print(f"❌ Custom parquet not found: {custom_path}")
            sys.exit(1)

    for path in DEFAULT_PARQUET_PATHS:
        if Path(path).exists():
            print(f"✓ Found parquet: {path}")
            return path

    print("❌ No parquet file found. Tried:")
    for path in DEFAULT_PARQUET_PATHS:
        print(f"  - {path}")
    sys.exit(1)


def export_metadata(
    df, output_path="data-pipeline/data/processed/product_metadata.json"
):
    """Export metadata to JSON file"""
    print("\n[1/4] Exporting metadata to JSON...")

    metadata = {}
    for _, row in tqdm(df.iterrows(), total=len(df), desc="Exporting"):
        article_id = str(int(row["article_id"]))
        metadata[article_id] = {
            "article_id": article_id,
            "colour_group_name": str(row.get("colour_group_name", "")),
            "product_type_name": str(row.get("product_type_name", "")),
            "department_name": str(row.get("department_name", "")),
            "detail_desc": str(row.get("detail_desc", ""))[:5000],
        }

        if "price" in df.columns:
            metadata[article_id]["price"] = str(row.get("price", ""))
        if "image_url" in df.columns:
            metadata[article_id]["image_url"] = str(row.get("image_url", ""))

    # Ensure output directory exists
    os.makedirs(os.path.dirname(output_path), exist_ok=True)

    with open(output_path, "w", encoding="utf-8") as f:
        json.dump(metadata, f, ensure_ascii=False, indent=2)

    print(f"✓ Exported {len(metadata)} items to {output_path}")


def load_redis_data(df):
    """Load Redis metadata and popularity ZSET"""
    print("\n[2/4] Loading Redis data...")

    r = redis.Redis(host=REDIS_HOST, port=REDIS_PORT, decode_responses=True)

    # Test connection
    try:
        r.ping()
        print(f"✓ Connected to Redis at {REDIS_HOST}:{REDIS_PORT}")
    except redis.ConnectionError as e:
        print(f"❌ Failed to connect to Redis: {e}")
        sys.exit(1)

    # Load metadata (item:* hashes)
    print("  Loading item metadata...")
    pipe = r.pipeline()
    popularity_ids = []

    for _, row in tqdm(df.iterrows(), total=len(df), desc="Redis metadata"):
        # Pad article_id to 10 digits to match Gateway padArticleId()
        article_id = str(int(row["article_id"])).zfill(10)
        key = f"item:{article_id}"

        meta = {
            "article_id": article_id,
            "prod_name": str(row.get("prod_name", "Unknown Product")),
            "product_type_name": str(row.get("product_type_name", "")),
            "department_name": str(row.get("department_name", "")),
            "detail_desc": str(row.get("detail_desc", ""))[:5000],
        }

        if "price" in df.columns:
            meta["price"] = str(row.get("price", ""))
        if "image_url" in df.columns:
            meta["image_url"] = str(row.get("image_url", ""))

        pipe.hset(key, mapping=meta)

        # Collect for popularity list (use padded ID)
        if len(popularity_ids) < POPULARITY_TOPN:
            popularity_ids.append(article_id)

    pipe.execute()
    print(f"  ✓ Loaded {len(df)} item metadata")

    # Load popularity ZSET (consistent with Gateway and Inference)
    print(f"  Loading popularity ZSET ({len(popularity_ids)} items)...")
    r.delete(POPULARITY_KEY)
    if popularity_ids:
        # Use ZADD with scores: higher score = more popular
        popularity_mapping = {
            pid: len(popularity_ids) - i for i, pid in enumerate(popularity_ids)
        }
        r.zadd(POPULARITY_KEY, popularity_mapping)

    # Verify
    zset_type = r.type(POPULARITY_KEY)
    zset_count = r.zcard(POPULARITY_KEY)
    print(f"  ✓ Created {POPULARITY_KEY} (type={zset_type}, count={zset_count})")


def load_milvus_data(df):
    """Initialize Milvus collection and load vectors"""
    print("\n[3/4] Loading Milvus data...")

    uri = f"http://{MILVUS_HOST}:{MILVUS_PORT}"

    try:
        mc = MilvusClient(uri=uri)
        print(f"✓ Connected to Milvus at {uri}")
    except Exception as e:
        print(f"❌ Failed to connect to Milvus: {e}")
        sys.exit(1)

    # Drop existing collection if exists
    if mc.has_collection(MILVUS_COLLECTION):
        print(f"  Dropping existing collection: {MILVUS_COLLECTION}")
        mc.drop_collection(MILVUS_COLLECTION)

    # Infer embedding dimension from data
    first_embedding = df.iloc[0]["bge_embedding"]
    dim = len(first_embedding)
    print(f"  Detected embedding dimension: {dim}")

    # Create collection schema
    print(f"  Creating collection: {MILVUS_COLLECTION}")
    fields = [
        FieldSchema(name="article_id", dtype=DataType.INT64, is_primary=True),
        FieldSchema(name="bge_embedding", dtype=DataType.FLOAT_VECTOR, dim=dim),
    ]
    schema = CollectionSchema(fields, description="ScaleStyle BGE embeddings")

    # Use low-level connections API for schema creation
    connections.connect(alias="default", host=MILVUS_HOST, port=MILVUS_PORT)
    collection = Collection(MILVUS_COLLECTION, schema)

    # Create IVF_FLAT index (production-ready)
    index_params = {
        "index_type": "IVF_FLAT",
        "metric_type": "IP",  # Inner Product (for BGE embeddings)
        "params": {"nlist": 128},
    }
    collection.create_index("bge_embedding", index_params)
    print("  ✓ Created index on bge_embedding")

    # Load data in batches
    batch_size = 1000
    total_rows = len(df)
    print(f"  Inserting {total_rows} vectors...")

    for i in tqdm(range(0, total_rows, batch_size), desc="Milvus batches"):
        batch = df.iloc[i : i + batch_size]
        data = [
            {
                "article_id": int(row["article_id"]),
                "bge_embedding": row["bge_embedding"],
            }
            for _, row in batch.iterrows()
        ]
        collection.insert(data)

    # Load collection into memory
    collection.load()
    print("  ✓ Loaded collection into memory")

    # Verify
    count = collection.num_entities
    print(f"  ✓ Collection contains {count} entities")


def verify_data():
    """Verify all data stores are accessible"""
    print("\n[4/4] Verifying data...")

    # Verify Redis
    try:
        r = redis.Redis(host=REDIS_HOST, port=REDIS_PORT, decode_responses=True)
        r.ping()

        # Check sample item
        sample_keys = r.keys("item:*")[:3]
        print(f"  ✓ Redis: {len(sample_keys)} sample items found")

        # Check popularity
        pop_type = r.type(POPULARITY_KEY)
        pop_count = r.zcard(POPULARITY_KEY)
        print(f"  ✓ Redis: {POPULARITY_KEY} (type={pop_type}, count={pop_count})")

    except Exception as e:
        print(f"  ⚠️  Redis verification failed: {e}")

    # Verify Milvus
    try:
        uri = f"http://{MILVUS_HOST}:{MILVUS_PORT}"
        mc = MilvusClient(uri=uri)

        if mc.has_collection(MILVUS_COLLECTION):
            # Get collection stats
            connections.connect(alias="default", host=MILVUS_HOST, port=MILVUS_PORT)
            collection = Collection(MILVUS_COLLECTION)
            count = collection.num_entities
            print(f"  ✓ Milvus: {MILVUS_COLLECTION} ({count} vectors)")
        else:
            print(f"  ⚠️  Milvus: {MILVUS_COLLECTION} not found")

    except Exception as e:
        print(f"  ⚠️  Milvus verification failed: {e}")


def main():
    parser = argparse.ArgumentParser(
        description="Bootstrap all ScaleStyle data stores",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument(
        "--parquet",
        help="Path to parquet file with embeddings",
        default=None,
    )
    parser.add_argument(
        "--skip-milvus",
        action="store_true",
        help="Skip Milvus initialization (only load Redis)",
    )
    parser.add_argument(
        "--skip-redis",
        action="store_true",
        help="Skip Redis initialization (only load Milvus)",
    )
    parser.add_argument(
        "--no-verify",
        action="store_true",
        help="Skip final verification step",
    )

    args = parser.parse_args()

    print("=" * 60)
    print("ScaleStyle Data Bootstrap")
    print("=" * 60)
    print(f"Redis:  {REDIS_HOST}:{REDIS_PORT}")
    print(f"Milvus: {MILVUS_HOST}:{MILVUS_PORT}")
    print(f"Collection: {MILVUS_COLLECTION}")
    print("=" * 60)

    # Find and load parquet
    parquet_path = find_parquet_file(args.parquet)
    print(f"\nLoading data from: {parquet_path}")
    df = pd.read_parquet(parquet_path)
    print(f"✓ Loaded {len(df)} rows, {len(df.columns)} columns")

    # Step 1: Export metadata
    export_metadata(df)

    # Step 2: Load Redis
    if not args.skip_redis:
        load_redis_data(df)
    else:
        print("\n[2/4] Skipping Redis (--skip-redis)")

    # Step 3: Load Milvus
    if not args.skip_milvus:
        load_milvus_data(df)
    else:
        print("\n[3/4] Skipping Milvus (--skip-milvus)")

    # Step 4: Verify
    if not args.no_verify:
        verify_data()

    print("\n" + "=" * 60)
    print("✓ Bootstrap complete!")
    print("=" * 60)
    print("\nNext steps:")
    print("  1. Start services: docker-compose up -d")
    print(
        "  2. Test API: curl http://localhost:8080/api/recommendation/search?query=dress&k=5"
    )
    print("  3. View traces: http://localhost:16686")


if __name__ == "__main__":
    main()
