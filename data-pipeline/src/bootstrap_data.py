#!/usr/bin/env python3
"""
Loads serving artifacts into Milvus and Redis.

Serving bootstrap pipeline:
  article_embeddings_bge_small_v1_5_detail.parquet  →  Milvus (text vectors)
  article_embeddings_bge_small_v1_5_detail.parquet  →  Redis item:* metadata hashes
  top_items.parquet                                  →  Redis global:popular ZSET

Usage:
    # Run from repo root
    python data-pipeline/src/bootstrap_data.py --skip-milvus   # Redis only
    python data-pipeline/src/bootstrap_data.py --drop-existing  # Full re-bootstrap

    # Explicit paths
    python data-pipeline/src/bootstrap_data.py \\
        --parquet data-pipeline/data/processed/article_embeddings_bge_small_v1_5_detail.parquet \\
        --top-items data-pipeline/data/processed/top_items.parquet \\
        --drop-existing

Environment Variables:
    REDIS_HOST, REDIS_PORT     Redis connection (default: localhost:6379)
    MILVUS_HOST, MILVUS_PORT   Milvus connection (default: localhost:19530)
    MILVUS_COLLECTION          Collection name   (default: scale_style_bge_small_v1_5)
    EXPECTED_EMBEDDING_DIM     Expected vector dimension (default: 384)
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

from src.config import POPULARITY_CANDIDATE_TOPN, POPULARITY_KEY
from src.redis_metadata import (
    build_item_metadata,
    canonical_article_id,
    item_key,
    item_meta_key,
)

# Default configuration (can be overridden by environment variables)
REDIS_HOST = os.getenv("REDIS_HOST", "localhost")
REDIS_PORT = int(os.getenv("REDIS_PORT", "6379"))
REDIS_TLS = os.getenv("REDIS_TLS", "false").lower() in ("1", "true", "yes")
MILVUS_HOST = os.getenv("MILVUS_HOST", "localhost")
MILVUS_PORT = os.getenv("MILVUS_PORT", "19530")
MILVUS_COLLECTION = os.getenv("MILVUS_COLLECTION", "scale_style_bge_small_v1_5")
# Expected embedding dimension from the parquet file.  Fail fast if actual != expected
# to catch model/collection mismatches before writing any data to Milvus.
EXPECTED_EMBEDDING_DIM = int(os.getenv("EXPECTED_EMBEDDING_DIM", "384"))

# Anchor all default output paths to the data-pipeline project root so that
# bootstrap_data.py produces consistent paths regardless of the working directory
# it is invoked from.
_PIPELINE_ROOT = Path(__file__).parent.parent.resolve()
_DEFAULT_METADATA_OUTPUT = str(_PIPELINE_ROOT / "data" / "processed" / "product_metadata.json")

# Default top_items paths (searched in order; first existing file wins).
DEFAULT_TOP_ITEMS_PATHS = [
    "data/processed/top_items.parquet",
    "data-pipeline/data/processed/top_items.parquet",
]

# Default parquet paths — BGE-small only.
# Legacy BGE-large artifacts (bge_detail, bge_v2) must be passed explicitly via
# --parquet to avoid silently loading a 1024-dim file into the 384-dim collection.
DEFAULT_PARQUET_PATHS = [
    "data/processed/article_embeddings_bge_small_v1_5_detail.parquet",
    "data-pipeline/data/processed/article_embeddings_bge_small_v1_5_detail.parquet",
]


def _resolve_embedding_column(df: pd.DataFrame) -> pd.DataFrame:
    """
    Ensure df contains a 'bge_embedding' column.

    Supports legacy parquet files where the column was named 'embedding'
    (produced by the original Colab notebook).  Renames with a clear warning.
    Raises ValueError if neither column is present.
    """
    if "bge_embedding" in df.columns:
        return df
    if "embedding" in df.columns:
        print(
            "Using legacy embedding column 'embedding'; renaming to 'bge_embedding'. "
            "Regenerate with generate_embeddings.py to get the canonical column name."
        )
        return df.rename(columns={"embedding": "bge_embedding"})
    raise ValueError(
        "No embedding column found in parquet. "
        "Expected 'bge_embedding' (or legacy 'embedding'). "
        f"Available columns: {df.columns.tolist()}\n"
        "Generate a fresh artifact with:  python data-pipeline/src/generate_embeddings.py"
    )


def find_parquet_file(custom_path=None):
    """Find parquet file in order of preference"""
    if custom_path:
        if Path(custom_path).exists():
            return custom_path
        else:
            print(f"Custom parquet not found: {custom_path}")
            sys.exit(1)

    for path in DEFAULT_PARQUET_PATHS:
        if Path(path).exists():
            print(f"Found parquet: {path}")
            return path

    print("No parquet file found. Tried:")
    for path in DEFAULT_PARQUET_PATHS:
        print(f"  - {path}")
    sys.exit(1)


def find_top_items_file(custom_path=None):
    """Return path to top_items.parquet, or None if not found and no custom_path given."""
    if custom_path:
        if Path(custom_path).exists():
            return custom_path
        print(f"Custom top-items path not found: {custom_path}")
        sys.exit(1)

    for path in DEFAULT_TOP_ITEMS_PATHS:
        if Path(path).exists():
            print(f"Found top_items: {path}")
            return path

    return None


def load_popularity(top_items_df):
    """Populate Redis global:popular ZSET from a top_items DataFrame.

    Uses purchase_count as the ZSET score so rank order is meaningful.
    """
    r = redis.Redis(
        host=REDIS_HOST,
        port=REDIS_PORT,
        decode_responses=True,
        ssl=REDIS_TLS,
        ssl_cert_reqs="required" if REDIS_TLS else None,
    )
    r.ping()

    topn = min(POPULARITY_CANDIDATE_TOPN, len(top_items_df))
    subset = top_items_df.head(topn)
    mapping = {
        str(row["article_id"]): int(row["purchase_count"])
        for _, row in subset.iterrows()
    }

    r.delete(POPULARITY_KEY)
    r.zadd(POPULARITY_KEY, mapping)

    zset_count = r.zcard(POPULARITY_KEY)
    print(f"  Created {POPULARITY_KEY} from transaction counts (count={zset_count})")
    return zset_count


def export_metadata(df, output_path=_DEFAULT_METADATA_OUTPUT):
    """Export metadata to JSON file"""
    print("\n[1/4] Exporting metadata to JSON...")

    metadata = {}
    for _, row in tqdm(df.iterrows(), total=len(df), desc="Exporting"):
        article_id = canonical_article_id(row["article_id"])
        metadata[article_id] = build_item_metadata(row)

    # Ensure output directory exists
    os.makedirs(os.path.dirname(output_path), exist_ok=True)

    with open(output_path, "w", encoding="utf-8") as f:
        json.dump(metadata, f, ensure_ascii=False, indent=2)

    print(f"Exported {len(metadata)} items to {output_path}")


def load_redis_data(df):
    """Load Redis metadata and popularity ZSET"""
    print("\n[2/4] Loading Redis data...")

    r = redis.Redis(
        host=REDIS_HOST,
        port=REDIS_PORT,
        decode_responses=True,
        ssl=REDIS_TLS,
        ssl_cert_reqs="required" if REDIS_TLS else None,
    )

    # Test connection
    try:
        r.ping()
        print(f"Connected to Redis at {REDIS_HOST}:{REDIS_PORT}")
    except redis.ConnectionError as e:
        print(f"Failed to connect to Redis: {e}")
        sys.exit(1)

    # Load metadata (item:* hashes)
    print("  Loading item metadata...")
    pipe = r.pipeline()

    for _, row in tqdm(df.iterrows(), total=len(df), desc="Redis metadata"):
        article_id = canonical_article_id(row["article_id"])
        meta = build_item_metadata(row)

        pipe.hset(item_key(article_id), mapping=meta)
        pipe.hset(item_meta_key(article_id), mapping=meta)

    pipe.execute()
    print(f"  Loaded {len(df)} item metadata")


def load_milvus_data(df, drop_existing: bool = False):
    """Initialize Milvus collection and load vectors"""
    print("\n[3/4] Loading Milvus data...")

    uri = f"http://{MILVUS_HOST}:{MILVUS_PORT}"

    try:
        mc = MilvusClient(uri=uri)
        print(f"Connected to Milvus at {uri}")
    except Exception as e:
        print(f"Failed to connect to Milvus: {e}")
        sys.exit(1)

    # Guard: refuse to overwrite an existing collection unless explicitly requested.
    if mc.has_collection(MILVUS_COLLECTION):
        if not drop_existing:
            print(
                f"Milvus collection '{MILVUS_COLLECTION}' already exists.\n"
                "   Re-run with --drop-existing to drop and recreate it."
            )
            sys.exit(1)
        print(f"  Dropping existing collection: {MILVUS_COLLECTION}")
        mc.drop_collection(MILVUS_COLLECTION)

    # Infer embedding dimension from data and validate against expectation.
    first_embedding = df.iloc[0]["bge_embedding"]
    dim = len(first_embedding)
    print(f"  Collection:           {MILVUS_COLLECTION}")
    print(f"  Detected dimension:   {dim}")
    print(f"  Expected dimension:   {EXPECTED_EMBEDDING_DIM}")
    if dim != EXPECTED_EMBEDDING_DIM:
        print(
            f"Dimension mismatch: parquet has {dim}-dim vectors but "
            f"EXPECTED_EMBEDDING_DIM={EXPECTED_EMBEDDING_DIM}. "
            "Set EXPECTED_EMBEDDING_DIM or provide the correct parquet file."
        )
        sys.exit(1)

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
    print("  Created index on bge_embedding")

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
    print("  Loaded collection into memory")

    # Verify
    count = collection.num_entities
    print(f"  Collection contains {count} entities")


def verify_data():
    """Verify all data stores are accessible"""
    print("\n[4/4] Verifying data...")

    # Verify Redis
    try:
        r = redis.Redis(
            host=REDIS_HOST,
            port=REDIS_PORT,
            decode_responses=True,
            ssl=REDIS_TLS,
            ssl_cert_reqs="required" if REDIS_TLS else None,
        )
        r.ping()

        # Check sample item
        sample_keys = r.keys("item:*")[:3]
        print(f"  Redis: {len(sample_keys)} sample items found")

        # Check popularity
        pop_type = r.type(POPULARITY_KEY)
        pop_count = r.zcard(POPULARITY_KEY)
        print(f"  Redis: {POPULARITY_KEY} (type={pop_type}, count={pop_count})")

    except Exception as e:
        print(f"  Redis verification failed: {e}")

    # Verify Milvus
    try:
        uri = f"http://{MILVUS_HOST}:{MILVUS_PORT}"
        mc = MilvusClient(uri=uri)

        if mc.has_collection(MILVUS_COLLECTION):
            # Get collection stats
            connections.connect(alias="default", host=MILVUS_HOST, port=MILVUS_PORT)
            collection = Collection(MILVUS_COLLECTION)
            count = collection.num_entities
            print(f"  Milvus: {MILVUS_COLLECTION} ({count} vectors)")
        else:
            print(f"  Milvus: {MILVUS_COLLECTION} not found")

    except Exception as e:
        print(f"  Milvus verification failed: {e}")


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
    parser.add_argument(
        "--drop-existing",
        action="store_true",
        help=(
            "Drop and recreate the Milvus collection if it already exists. "
            "Required when re-bootstrapping to avoid accidental data loss."
        ),
    )
    parser.add_argument(
        "--top-items",
        default=None,
        help="Path to top_items.parquet (transaction-based popularity). "
        "Auto-discovered from default locations if not specified.",
    )
    parser.add_argument(
        "--transactions",
        default=None,
        help="Path to transactions_train.csv. Used to compute popularity on the fly "
        "when top_items.parquet is absent.",
    )
    parser.add_argument(
        "--allow-empty-popularity",
        action="store_true",
        help="Allow bootstrap to proceed without populating global:popular. "
        "NOT recommended for production — popularity fallback will return empty results.",
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
    print(f"Loaded {len(df)} rows, {len(df.columns)} columns")

    # Normalise embedding column name (supports legacy 'embedding' → 'bge_embedding')
    df = _resolve_embedding_column(df)

    # Step 1: Export metadata
    export_metadata(df)

    # Step 2: Load Redis
    if not args.skip_redis:
        load_redis_data(df)

        # Resolve popularity source: top_items.parquet > transactions CSV > fail
        print("\n  Resolving popularity source...")
        top_items_path = find_top_items_file(args.top_items)
        if top_items_path:
            top_items_df = pd.read_parquet(top_items_path)
            print(f"  Using top_items.parquet ({len(top_items_df):,} rows)")
            load_popularity(top_items_df)
        elif args.transactions:
            from src.generate_top_items import compute_top_items
            print(f"  Computing popularity from {args.transactions}...")
            top_items_df = compute_top_items(args.transactions)
            load_popularity(top_items_df)
        elif args.allow_empty_popularity:
            print("  --allow-empty-popularity: skipping global:popular (fallback will be empty)")
        else:
            print(
                "No popularity source found.\n"
                "   Generate top_items.parquet first:\n"
                "     python data-pipeline/src/generate_top_items.py\n"
                "   Or pass --transactions PATH to compute on the fly.\n"
                "   Or pass --allow-empty-popularity to skip (not recommended)."
            )
            sys.exit(1)
    else:
        print("\n[2/4] Skipping Redis (--skip-redis)")

    # Step 3: Load Milvus
    if not args.skip_milvus:
        load_milvus_data(df, drop_existing=args.drop_existing)
    else:
        print("\n[3/4] Skipping Milvus (--skip-milvus)")

    # Step 4: Verify
    if not args.no_verify:
        verify_data()

    print("\n" + "=" * 60)
    print("Bootstrap complete!")
    print("=" * 60)
    print("\nNext steps:")
    print("  1. Start services: docker-compose up -d")
    print(
        "  2. Test API: curl http://localhost:8080/api/recommendation/search?query=dress&k=5"
    )
    print("  3. View traces: http://localhost:16686")


if __name__ == "__main__":
    main()
