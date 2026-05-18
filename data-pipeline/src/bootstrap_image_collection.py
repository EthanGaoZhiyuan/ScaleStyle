#!/usr/bin/env python3
"""
Loads the CLIP image embedding artifact into a dedicated Milvus collection.

Bootstrap pipeline:
  article_image_embeddings_clip_vit_base_patch32.parquet
    → Milvus collection: scale_style_clip_image_v1
      (INT64 primary key, 512-dim FLOAT_VECTOR, IVF_FLAT IP index)

This script is image-only.  It never touches:
  - scale_style_bge_small_v1_5  (text collection)
  - Redis (metadata or popularity)

Usage:
    # Run from repo root
    python data-pipeline/src/bootstrap_image_collection.py --drop-existing

    # Explicit parquet path
    python data-pipeline/src/bootstrap_image_collection.py \\
        --parquet data-pipeline/data/processed/article_image_embeddings_clip_vit_base_patch32.parquet \\
        --collection scale_style_clip_image_v1 \\
        --drop-existing

    # Dry-run: validate parquet only, do not touch Milvus
    python data-pipeline/src/bootstrap_image_collection.py --dry-run

Environment Variables:
    MILVUS_HOST   Milvus host (default: localhost)
    MILVUS_PORT   Milvus port (default: 19530)
"""

import argparse
import os
import sys
from pathlib import Path

import pandas as pd
from pymilvus import (
    Collection,
    CollectionSchema,
    DataType,
    FieldSchema,
    MilvusClient,
    connections,
)

# ──────────────────────────── constants ────────────────────────────

MILVUS_HOST = os.getenv("MILVUS_HOST", "localhost")
MILVUS_PORT = os.getenv("MILVUS_PORT", "19530")

# Guard: this script must never touch the text collection.
_TEXT_COLLECTION = "scale_style_bge_small_v1_5"

_PIPELINE_ROOT = Path(__file__).parent.parent.resolve()
DEFAULT_PARQUET = str(
    _PIPELINE_ROOT
    / "data"
    / "processed"
    / "article_image_embeddings_clip_vit_base_patch32.parquet"
)
DEFAULT_COLLECTION = "scale_style_clip_image_v1"
DEFAULT_VECTOR_FIELD = "image_embedding"
DEFAULT_EXPECTED_DIM = 512
DEFAULT_METRIC_TYPE = "IP"
DEFAULT_BATCH_SIZE = 1000

# ──────────────────────────── parquet validation ────────────────────────────


def validate_parquet(
    parquet_path: str,
    expected_dim: int = DEFAULT_EXPECTED_DIM,
    vector_field: str = DEFAULT_VECTOR_FIELD,
) -> pd.DataFrame:
    """
    Load and validate the image embedding parquet.

    Returns the DataFrame if all checks pass.
    Raises ValueError with a descriptive message on any failure.
    This function is pure — it does not touch Milvus.
    """
    p = Path(parquet_path)
    if not p.exists():
        raise FileNotFoundError(f"Parquet not found: {parquet_path}")

    try:
        df = pd.read_parquet(str(p))
    except Exception as exc:
        raise ValueError(f"Cannot read parquet: {exc}") from exc

    if len(df) == 0:
        raise ValueError("Parquet is empty (0 rows)")

    required = {"article_id", "image_path", vector_field}
    missing = required - set(df.columns)
    if missing:
        raise ValueError(
            f"Missing required columns: {sorted(missing)}. "
            f"Available: {sorted(df.columns.tolist())}"
        )

    null_count = int(df[vector_field].isna().sum())
    if null_count > 0:
        raise ValueError(f"{null_count} null values in '{vector_field}' column")

    sample = df[vector_field].iloc[0]
    try:
        actual_dim = len(sample)
    except TypeError as exc:
        raise ValueError(
            f"'{vector_field}' at row 0 is not iterable "
            f"(got {type(sample).__name__}; expected list or array)"
        ) from exc

    if actual_dim != expected_dim:
        raise ValueError(
            f"Dimension mismatch: parquet has {actual_dim}-dim vectors, "
            f"expected {expected_dim}."
        )

    # Check all rows have consistent dim (sample first 100 to keep fast for large files)
    sample_size = min(100, len(df))
    dims = df[vector_field].iloc[:sample_size].apply(len).unique()
    if len(dims) > 1:
        raise ValueError(
            f"Inconsistent embedding dimensions in first {sample_size} rows: {sorted(dims.tolist())}"
        )

    print(f"Parquet valid: {len(df):,} rows, {actual_dim}-dim '{vector_field}'")
    return df


# ──────────────────────────── Milvus helpers ────────────────────────────


def _milvus_client() -> MilvusClient:
    uri = f"http://{MILVUS_HOST}:{MILVUS_PORT}"
    try:
        mc = MilvusClient(uri=uri)
        return mc
    except Exception as exc:
        print(f"Cannot connect to Milvus at {uri}: {exc}")
        sys.exit(1)


def create_image_collection(
    collection_name: str,
    dim: int,
    vector_field: str,
    metric_type: str,
    drop_existing: bool,
    n_rows: int,
) -> Collection:
    """
    Create (or reopen) the image Milvus collection.

    Primary key choice: article_id as INT64.
    This matches bootstrap_data.py's text collection convention and avoids
    the VARCHAR primary key used by the legacy inference-service script, which
    has a different schema and different column names.

    nlist is computed as min(128, n_rows // 4) so that IVF_FLAT index
    training always has more data points than clusters, including on small
    smoke-test artifacts.
    """
    mc = _milvus_client()

    if mc.has_collection(collection_name):
        if not drop_existing:
            print(
                f"Collection '{collection_name}' already exists.\n"
                "   Re-run with --drop-existing to drop and recreate it."
            )
            sys.exit(1)
        print(f"  Dropping existing collection: {collection_name}")
        mc.drop_collection(collection_name)

    # Use low-level connections API for schema creation — same pattern as bootstrap_data.py
    connections.connect(alias="default", host=MILVUS_HOST, port=MILVUS_PORT)

    fields = [
        FieldSchema(
            name="article_id", dtype=DataType.INT64, is_primary=True, auto_id=False
        ),
        FieldSchema(name="article_id_str", dtype=DataType.VARCHAR, max_length=32),
        FieldSchema(name="image_path", dtype=DataType.VARCHAR, max_length=512),
        FieldSchema(name=vector_field, dtype=DataType.FLOAT_VECTOR, dim=dim),
    ]
    schema = CollectionSchema(fields, description="ScaleStyle CLIP image embeddings")

    print(f"  Creating collection: {collection_name}")
    collection = Collection(collection_name, schema)

    # nlist must be <= n_rows for IVF_FLAT index training
    nlist = max(1, min(128, n_rows // 4))
    index_params = {
        "index_type": "IVF_FLAT",
        "metric_type": metric_type,
        "params": {"nlist": nlist},
    }
    collection.create_index(vector_field, index_params)
    print(f"  IVF_FLAT index created (metric={metric_type}, nlist={nlist})")

    collection.load()
    print("  Collection loaded into memory")
    return collection


def insert_records(
    collection: Collection,
    df: pd.DataFrame,
    vector_field: str,
    batch_size: int,
) -> int:
    """Insert all rows from df into collection. Returns total inserted count."""
    total = len(df)
    inserted = 0

    # Ensure article_id_str is present; compute if missing
    if "article_id_str" not in df.columns:
        df = df.copy()
        df["article_id_str"] = df["article_id"].apply(lambda v: str(int(v)).zfill(10))

    for start in range(0, total, batch_size):
        batch = df.iloc[start : start + batch_size]
        data = [
            {
                "article_id": int(row["article_id"]),
                "article_id_str": str(row["article_id_str"]),
                "image_path": str(row["image_path"]),
                vector_field: row[vector_field],
            }
            for _, row in batch.iterrows()
        ]
        collection.insert(data)
        inserted += len(batch)

    collection.flush()
    return inserted


def verify_and_search(
    collection: Collection,
    df: pd.DataFrame,
    vector_field: str,
    metric_type: str,
    k: int = 5,
) -> None:
    """Verify entity count and run a sample search using the first embedding."""
    count = collection.num_entities
    expected = len(df)
    if count != expected:
        print(f"Entity count mismatch: Milvus has {count}, expected {expected}")
    else:
        print(f"Entity count: {count} (matches parquet)")

    # Sample search using the first vector in the parquet
    query_vec = df[vector_field].iloc[0]
    search_params = {"metric_type": metric_type, "params": {"nprobe": 10}}

    results = collection.search(
        data=[query_vec],
        anns_field=vector_field,
        param=search_params,
        limit=k,
        output_fields=["article_id", "article_id_str", "image_path"],
    )

    print(f"\n  Sample search (query = first embedding in parquet), top {k}:")
    hits = results[0]
    if not hits:
        print("  Search returned no results")
    else:
        for i, hit in enumerate(hits):
            aid = hit.entity.get("article_id")
            aid_str = hit.entity.get("article_id_str")
            img = hit.entity.get("image_path", "")
            print(
                f"    {i+1}. article_id={aid} ({aid_str})  score={hit.score:.6f}  path={img}"
            )


# ──────────────────────────── CLI ────────────────────────────


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Bootstrap the CLIP image Milvus collection.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument(
        "--parquet",
        default=DEFAULT_PARQUET,
        help="Path to image embedding parquet",
    )
    parser.add_argument(
        "--collection",
        default=DEFAULT_COLLECTION,
        help=f"Milvus collection name (default: {DEFAULT_COLLECTION})",
    )
    parser.add_argument(
        "--vector-field",
        default=DEFAULT_VECTOR_FIELD,
        help=f"Vector field name in parquet (default: {DEFAULT_VECTOR_FIELD})",
    )
    parser.add_argument(
        "--expected-dim",
        type=int,
        default=DEFAULT_EXPECTED_DIM,
        help=f"Expected embedding dimension (default: {DEFAULT_EXPECTED_DIM})",
    )
    parser.add_argument(
        "--metric-type",
        default=DEFAULT_METRIC_TYPE,
        choices=["IP", "L2", "COSINE"],
        help=f"Milvus index metric type (default: {DEFAULT_METRIC_TYPE})",
    )
    parser.add_argument(
        "--drop-existing",
        action="store_true",
        help="Drop and recreate the collection if it already exists.",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Validate parquet only; do not touch Milvus.",
    )
    parser.add_argument(
        "--batch-size",
        type=int,
        default=DEFAULT_BATCH_SIZE,
        help=f"Insert batch size (default: {DEFAULT_BATCH_SIZE})",
    )
    return parser


def main() -> None:
    args = _build_parser().parse_args()

    # Hard guard — never touch the text collection
    if args.collection == _TEXT_COLLECTION:
        print(f"Refusing to modify the text collection '{_TEXT_COLLECTION}'.")
        sys.exit(1)

    print("=" * 60)
    print("ScaleStyle Image Collection Bootstrap")
    print("=" * 60)
    print(f"Parquet:     {args.parquet}")
    print(f"Collection:  {args.collection}")
    print(f"Vector field:{args.vector_field}")
    print(f"Dim:         {args.expected_dim}")
    print(f"Metric:      {args.metric_type}")
    print(f"Milvus:      {MILVUS_HOST}:{MILVUS_PORT}")
    print("=" * 60)

    # ── Step 1: Validate parquet ──
    print("\n[1/4] Validating parquet...")
    df = validate_parquet(args.parquet, args.expected_dim, args.vector_field)

    if args.dry_run:
        print("\n--dry-run: parquet is valid. Milvus not touched.")
        return

    # ── Step 2: Create collection ──
    print("\n[2/4] Creating Milvus collection...")
    collection = create_image_collection(
        collection_name=args.collection,
        dim=args.expected_dim,
        vector_field=args.vector_field,
        metric_type=args.metric_type,
        drop_existing=args.drop_existing,
        n_rows=len(df),
    )

    # ── Step 3: Insert ──
    print(f"\n[3/4] Inserting {len(df):,} records (batch_size={args.batch_size})...")
    inserted = insert_records(collection, df, args.vector_field, args.batch_size)
    print(f"  Inserted {inserted:,} records")

    # ── Step 4: Verify + search ──
    print("\n[4/4] Verifying...")
    verify_and_search(collection, df, args.vector_field, args.metric_type)

    print("\n" + "=" * 60)
    print("Image collection bootstrap complete")
    print("=" * 60)
    print(f"  Collection: {args.collection}")
    print(f"  Entities:   {collection.num_entities}")
    print(f"  Dim:        {args.expected_dim}")
    print(f"  Index:      IVF_FLAT / {args.metric_type}")


if __name__ == "__main__":
    main()
