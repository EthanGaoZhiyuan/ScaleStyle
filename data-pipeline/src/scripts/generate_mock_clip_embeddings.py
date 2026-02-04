#!/usr/bin/env python3
"""
Generate mock CLIP embeddings for testing image search.

Since we don't have actual product images, this creates synthetic
CLIP embeddings for a subset of items from the catalog.

Usage:
    python generate_mock_clip_embeddings.py [--count 1000] [--host milvus-standalone]
"""

import argparse
import sys
from pathlib import Path

import numpy as np
import pandas as pd
from pymilvus import Collection, connections
from tqdm import tqdm


def load_product_ids(data_dir: Path, limit: int = 1000):
    """Load product IDs from articles.csv"""
    articles_path = data_dir / "articles.csv"

    if not articles_path.exists():
        print(f"❌ Error: {articles_path} not found")
        sys.exit(1)

    print(f"📂 Loading product IDs from {articles_path}")
    df = pd.read_csv(articles_path, usecols=["article_id"])

    # Get unique article IDs
    article_ids = df["article_id"].unique()[:limit]

    print(f"✅ Loaded {len(article_ids)} article IDs")
    return article_ids


def generate_mock_embeddings(article_ids, dim=512, seed=42):
    """
    Generate mock CLIP embeddings (normalized random vectors).

    In production, these would be real CLIP embeddings from product images.
    For testing, we generate random unit vectors.
    """
    print(f"🎲 Generating {len(article_ids)} mock CLIP embeddings (dim={dim})")

    np.random.seed(seed)

    embeddings = []
    for _aid in article_ids:
        # Generate random vector
        vec = np.random.randn(dim).astype(np.float32)
        # Normalize to unit length (required for IP metric)
        vec = vec / np.linalg.norm(vec)
        embeddings.append(vec)

    return np.array(embeddings)


def insert_to_milvus(
    article_ids,
    embeddings,
    collection_name: str = "scale_style_clip_v1",
    batch_size: int = 100,
):
    """Insert mock embeddings into Milvus collection"""

    # Get collection
    collection = Collection(name=collection_name)

    print(f"💾 Inserting {len(article_ids)} embeddings into {collection_name}")

    # Convert article_ids to strings
    item_ids = [str(aid) for aid in article_ids]

    # Insert in batches
    total_batches = (len(item_ids) + batch_size - 1) // batch_size

    for i in tqdm(range(0, len(item_ids), batch_size), total=total_batches):
        batch_ids = item_ids[i : i + batch_size]
        batch_vecs = embeddings[i : i + batch_size].tolist()

        entities = [batch_ids, batch_vecs]

        collection.insert(entities)

    # Flush to ensure data is persisted
    collection.flush()

    print(f"✅ Inserted {collection.num_entities} entities total")


def verify_search(collection_name: str = "scale_style_clip_v1"):
    """Test search functionality with a random query"""
    collection = Collection(name=collection_name)
    collection.load()

    print("\n🔍 Testing search functionality...")

    # Generate random query vector
    query_vec = np.random.randn(512).astype(np.float32)
    query_vec = query_vec / np.linalg.norm(query_vec)

    # Search
    search_params = {
        "metric_type": "IP",
        "params": {"ef": 64},  # HNSW search parameter
    }

    results = collection.search(
        data=[query_vec.tolist()],
        anns_field="vector",
        param=search_params,
        limit=5,
        output_fields=["item_id"],
    )

    print(f"✅ Search returned {len(results[0])} results:")
    for i, hit in enumerate(results[0], 1):
        print(f"   {i}. item_id={hit.entity.get('item_id')}, score={hit.score:.4f}")


def main():
    parser = argparse.ArgumentParser(
        description="Generate mock CLIP embeddings for testing"
    )
    parser.add_argument(
        "--count",
        type=int,
        default=1000,
        help="Number of items to generate embeddings for (default: 1000)",
    )
    parser.add_argument(
        "--host",
        default="milvus-standalone",
        help="Milvus server hostname (default: milvus-standalone)",
    )
    parser.add_argument(
        "--port",
        type=int,
        default=19530,
        help="Milvus server port (default: 19530)",
    )
    parser.add_argument(
        "--collection",
        default="scale_style_clip_v1",
        help="Collection name (default: scale_style_clip_v1)",
    )
    parser.add_argument(
        "--data-dir",
        type=Path,
        default=Path(__file__).parent.parent.parent / "data" / "raw",
        help="Path to data directory containing articles.csv",
    )
    parser.add_argument(
        "--batch-size",
        type=int,
        default=100,
        help="Batch size for insertion (default: 100)",
    )

    args = parser.parse_args()

    try:
        # Connect to Milvus
        print(f"🔗 Connecting to Milvus at {args.host}:{args.port}")
        connections.connect(alias="default", host=args.host, port=str(args.port))

        # Load product IDs
        article_ids = load_product_ids(args.data_dir, limit=args.count)

        # Generate mock embeddings
        embeddings = generate_mock_embeddings(article_ids, dim=512)

        # Insert into Milvus
        insert_to_milvus(
            article_ids,
            embeddings,
            collection_name=args.collection,
            batch_size=args.batch_size,
        )

        # Verify search works
        verify_search(collection_name=args.collection)

        print("\n✅ Success! Mock CLIP embeddings are ready for testing.")
        print(f"   - Collection: {args.collection}")
        print(f"   - Total items: {len(article_ids)}")
        print("   - Dimension: 512")
        print("\n💡 Test the API:")
        print("   kubectl port-forward -n scalestyle svc/inference 8000:8000")
        print("   curl -X POST http://localhost:8000/search/image \\")
        print("     -H 'Content-Type: application/json' \\")
        print('     -d \'{"image_base64": "mock", "k": 5}\'')

    except Exception as e:
        print(f"\n❌ Error: {e}", file=sys.stderr)
        import traceback

        traceback.print_exc()
        sys.exit(1)
    finally:
        connections.disconnect(alias="default")


if __name__ == "__main__":
    main()
