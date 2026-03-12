#!/usr/bin/env python3
"""
Milvus Image Collection Setup

Creates Milvus collection for FashionCLIP image embeddings and ingests parquet data.

Usage:
    # Create collection only
    python milvus_image_collection.py --mode create
    
    # Ingest embeddings from parquet
    python milvus_image_collection.py --mode ingest \\
        --parquet_path ../data-pipeline/data/processed/image_embeddings.parquet \\
        --batch_size 100
    
    # Full pipeline (create + ingest)
    python milvus_image_collection.py --mode full \\
        --parquet_path ../data-pipeline/data/processed/image_embeddings.parquet
"""

import argparse
import logging
import os
import time
from pathlib import Path

import pandas as pd
from pymilvus import (
    Collection,
    CollectionSchema,
    connections,
    DataType,
    FieldSchema,
    utility,
)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)
logger = logging.getLogger(__name__)


class MilvusImageCollection:
    """Manager for Milvus image embedding collection"""

    def __init__(
        self,
        collection_name: str = "scalestyle_image_v1",
        host: str = "localhost",
        port: str = "19530",
        dim: int = 512,
    ):
        """
        Args:
            collection_name: Milvus collection name
            host: Milvus server host
            port: Milvus server port
            dim: Embedding dimension (512 for FashionCLIP)
        """
        self.collection_name = collection_name
        self.dim = dim

        # Connect to Milvus
        logger.info(f"Connecting to Milvus at {host}:{port}")
        connections.connect(
            alias="default",
            host=host,
            port=port,
        )
        logger.info("✅ Connected to Milvus")

        self.collection = None

    def create_collection(self, drop_existing: bool = False):
        """
        Create Milvus collection for image embeddings

        Schema:
        - article_id (VARCHAR, primary key): H&M article ID
        - vector (FLOAT_VECTOR, dim=512): FashionCLIP embedding
        - image_path (VARCHAR): Relative path to image
        - width (INT32): Image width
        - height (INT32): Image height

        Index: HNSW for fast ANN search
        """
        # Check if collection exists
        if utility.has_collection(self.collection_name):
            if drop_existing:
                logger.warning(f"Dropping existing collection: {self.collection_name}")
                utility.drop_collection(self.collection_name)
            else:
                logger.info(f"Collection {self.collection_name} already exists")
                self.collection = Collection(self.collection_name)
                return

        # Define schema
        fields = [
            FieldSchema(
                name="article_id",
                dtype=DataType.VARCHAR,
                max_length=32,
                is_primary=True,
                auto_id=False,
                description="H&M article ID",
            ),
            FieldSchema(
                name="vector",
                dtype=DataType.FLOAT_VECTOR,
                dim=self.dim,
                description="FashionCLIP image embedding (512-d)",
            ),
            FieldSchema(
                name="image_path",
                dtype=DataType.VARCHAR,
                max_length=256,
                description="Relative path to image file",
            ),
            FieldSchema(
                name="width",
                dtype=DataType.INT32,
                description="Image width in pixels",
            ),
            FieldSchema(
                name="height",
                dtype=DataType.INT32,
                description="Image height in pixels",
            ),
        ]

        schema = CollectionSchema(
            fields=fields,
            description="ScaleStyle FashionCLIP image embeddings",
        )

        # Create collection
        logger.info(f"Creating collection: {self.collection_name}")
        self.collection = Collection(
            name=self.collection_name,
            schema=schema,
        )
        logger.info("✅ Collection created")

        # Create HNSW index for fast search
        logger.info("Creating HNSW index on vector field...")
        index_params = {
            "index_type": "HNSW",
            "metric_type": "IP",  # Inner Product (cosine similarity for normalized vectors)
            "params": {
                "M": 16,  # Max connections per layer
                "efConstruction": 200,  # Build-time search depth
            },
        }

        self.collection.create_index(
            field_name="vector",
            index_params=index_params,
        )
        logger.info("✅ HNSW index created")

        # Load collection into memory
        self.collection.load()
        logger.info("✅ Collection loaded into memory")

    def ingest_parquet(
        self,
        parquet_path: Path,
        batch_size: int = 100,
        limit: int = None,
    ):
        """
        Ingest embeddings from parquet file

        Args:
            parquet_path: Path to parquet file from image_etl.py
            batch_size: Number of records per batch insert
            limit: Max number of records to ingest (for testing)
        """
        if not parquet_path.exists():
            raise FileNotFoundError(f"Parquet file not found: {parquet_path}")

        # Ensure collection exists and is loaded
        if self.collection is None:
            self.collection = Collection(self.collection_name)

        if not utility.has_collection(self.collection_name):
            raise RuntimeError(
                f"Collection {self.collection_name} does not exist. Run with --mode create first."
            )

        # Load collection if not already loaded
        logger.info("Loading collection...")
        self.collection.load()

        # Read parquet
        logger.info(f"Reading parquet from {parquet_path}")
        df = pd.read_parquet(parquet_path)

        if limit:
            df = df.head(limit)

        total_rows = len(df)
        logger.info(f"Found {total_rows} records to ingest")

        # Validate schema
        required_cols = ["image_id", "embedding", "image_path", "width", "height"]
        missing = [c for c in required_cols if c not in df.columns]
        if missing:
            raise ValueError(f"Missing columns in parquet: {missing}")

        # Validate embedding dimension
        sample_emb = df.iloc[0]["embedding"]
        if len(sample_emb) != self.dim:
            raise ValueError(
                f"Expected {self.dim}-d embeddings, got {len(sample_emb)}-d"
            )

        # Ingest in batches
        num_batches = (total_rows + batch_size - 1) // batch_size
        logger.info(f"Ingesting in {num_batches} batches of {batch_size}")

        start_time = time.time()
        total_inserted = 0

        for i in range(0, total_rows, batch_size):
            batch_df = df.iloc[i : i + batch_size]

            # Prepare data for Milvus
            data = [
                batch_df["image_id"].tolist(),  # article_id
                batch_df["embedding"].tolist(),  # vector
                batch_df["image_path"].tolist(),  # image_path
                batch_df["width"].tolist(),  # width
                batch_df["height"].tolist(),  # height
            ]

            # Insert batch
            self.collection.insert(data)
            total_inserted += len(batch_df)

            if (i // batch_size + 1) % 10 == 0:
                logger.info(
                    f"Progress: {total_inserted}/{total_rows} ({total_inserted/total_rows*100:.1f}%)"
                )

        elapsed = time.time() - start_time
        throughput = total_inserted / elapsed if elapsed > 0 else 0

        logger.info(f"\n{'='*60}")
        logger.info("✅ Ingestion complete!")
        logger.info(f"   Total inserted: {total_inserted}")
        logger.info(f"   Time elapsed: {elapsed:.2f}s")
        logger.info(f"   Throughput: {throughput:.1f} records/s")
        logger.info(f"{'='*60}\n")

        # Flush to ensure persistence
        logger.info("Flushing collection...")
        self.collection.flush()
        logger.info("✅ Collection flushed")

        # Verify count
        count = self.collection.num_entities
        logger.info(f"✅ Collection now has {count} entities")

    def test_search(self, k: int = 5):
        """
        Test search with a random vector

        Args:
            k: Number of results to return
        """
        import numpy as np

        if self.collection is None:
            self.collection = Collection(self.collection_name)
            self.collection.load()

        # Generate random normalized vector
        random_vector = np.random.rand(self.dim).astype(np.float32)
        random_vector = random_vector / np.linalg.norm(random_vector)

        logger.info(f"\nTesting search with k={k}")

        search_params = {
            "metric_type": "IP",
            "params": {"ef": 100},  # HNSW search parameter
        }

        start_time = time.time()
        results = self.collection.search(
            data=[random_vector.tolist()],
            anns_field="vector",
            param=search_params,
            limit=k,
            output_fields=["article_id", "image_path", "width", "height"],
        )
        search_time = (time.time() - start_time) * 1000  # ms

        logger.info(f"Search completed in {search_time:.2f}ms")
        logger.info(f"\nTop {k} results:")
        for i, hit in enumerate(results[0]):
            logger.info(
                f"  {i+1}. article_id={hit.entity.get('article_id')}, score={hit.score:.4f}"
            )

        return results


def main():
    parser = argparse.ArgumentParser(
        description="Setup Milvus image collection and ingest embeddings"
    )
    parser.add_argument(
        "--mode",
        type=str,
        required=True,
        choices=["create", "ingest", "full", "test"],
        help="Operation mode: create (collection only), ingest (data only), full (both), test (search test)",
    )
    parser.add_argument(
        "--collection_name",
        type=str,
        default="scalestyle_image_v1",
        help="Milvus collection name",
    )
    parser.add_argument(
        "--host",
        type=str,
        default=os.getenv("MILVUS_HOST", "localhost"),
        help="Milvus host (default: localhost or MILVUS_HOST env)",
    )
    parser.add_argument(
        "--port",
        type=str,
        default=os.getenv("MILVUS_PORT", "19530"),
        help="Milvus port (default: 19530 or MILVUS_PORT env)",
    )
    parser.add_argument(
        "--parquet_path",
        type=str,
        help="Path to parquet file with embeddings (required for ingest/full modes)",
    )
    parser.add_argument(
        "--batch_size",
        type=int,
        default=100,
        help="Batch size for ingestion (default: 100)",
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=None,
        help="Limit number of records to ingest (for testing)",
    )
    parser.add_argument(
        "--drop_existing",
        action="store_true",
        help="Drop existing collection before creating (WARNING: deletes all data)",
    )

    args = parser.parse_args()

    # Validate inputs
    if args.mode in ["ingest", "full"] and not args.parquet_path:
        parser.error("--parquet_path is required for ingest/full modes")

    # Initialize manager
    manager = MilvusImageCollection(
        collection_name=args.collection_name,
        host=args.host,
        port=args.port,
    )

    # Execute mode
    if args.mode in ["create", "full"]:
        manager.create_collection(drop_existing=args.drop_existing)

    if args.mode in ["ingest", "full"]:
        parquet_path = Path(args.parquet_path)
        manager.ingest_parquet(
            parquet_path=parquet_path,
            batch_size=args.batch_size,
            limit=args.limit,
        )

    if args.mode == "test":
        manager.test_search(k=5)

    logger.info("\n✅ All operations completed successfully!")


if __name__ == "__main__":
    main()
