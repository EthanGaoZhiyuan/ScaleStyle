#!/usr/bin/env python3
"""
Initialize Milvus CLIP collection for image-based search.

Creates a new collection with 512-dim vectors for CLIP embeddings.
This is separate from the text embedding collection (scale_style_v2).

Usage:
    python milvus_clip_init.py [--host milvus-standalone] [--port 19530]
"""

import argparse
import sys

from pymilvus import (
    Collection,
    CollectionSchema,
    DataType,
    FieldSchema,
    connections,
    utility,
)


def create_clip_collection(
    collection_name: str = "scale_style_clip_v1",
    host: str = "milvus-standalone",
    port: int = 19530,
    drop_existing: bool = False,
):
    """
    Create Milvus collection for CLIP image embeddings.

    Args:
        collection_name: Name of the collection to create
        host: Milvus server hostname
        port: Milvus server port
        drop_existing: If True, drop existing collection before creating
    """
    print(f"🔗 Connecting to Milvus at {host}:{port}")
    connections.connect(alias="default", host=host, port=str(port))

    # Check if collection already exists
    if utility.has_collection(collection_name):
        if drop_existing:
            print(f"⚠️  Dropping existing collection: {collection_name}")
            utility.drop_collection(collection_name)
        else:
            print(f"✅ Collection '{collection_name}' already exists")
            # Load existing collection
            collection = Collection(name=collection_name)
            collection.load()
            print(f"📊 Collection loaded with {collection.num_entities} entities")
            return collection

    # Define schema
    print("📝 Creating collection schema...")
    fields = [
        FieldSchema(
            name="item_id",
            dtype=DataType.VARCHAR,
            max_length=50,
            is_primary=True,
            description="Article ID (primary key)",
        ),
        FieldSchema(
            name="vector",
            dtype=DataType.FLOAT_VECTOR,
            dim=512,  # CLIP ViT-B/32 embedding dimension
            description="CLIP image embedding vector",
        ),
    ]

    schema = CollectionSchema(
        fields=fields,
        description="CLIP image embeddings for visual product search",
    )

    # Create collection
    print(f"🔨 Creating collection: {collection_name}")
    collection = Collection(
        name=collection_name,
        schema=schema,
        using="default",
    )

    # Create HNSW index for fast similarity search
    print("🔍 Creating HNSW index...")
    index_params = {
        "metric_type": "IP",  # Inner Product (cosine similarity for normalized vectors)
        "index_type": "HNSW",
        "params": {
            "M": 16,  # Number of bi-directional links
            "efConstruction": 200,  # Search depth during index construction
        },
    }

    collection.create_index(
        field_name="vector",
        index_params=index_params,
    )

    # Load collection into memory
    print("💾 Loading collection into memory...")
    collection.load()

    print("✅ CLIP collection created successfully!")
    print(f"   - Collection: {collection_name}")
    print("   - Dimension: 512")
    print("   - Metric: Inner Product (IP)")
    print("   - Index: HNSW (M=16, efConstruction=200)")
    print(f"   - Entities: {collection.num_entities}")

    return collection


def verify_collection(collection_name: str = "scale_style_clip_v1"):
    """Verify collection is ready for use"""
    collection = Collection(name=collection_name)

    print("\n📊 Collection Status:")
    print(f"   - Name: {collection.name}")
    print(f"   - Entities: {collection.num_entities}")
    print(f"   - Primary field: {collection.primary_field.name}")
    print(f"   - Vector field: vector (dim={512})")
    print(f"   - Loaded: {utility.load_state(collection_name)}")

    # Get index info
    indexes = collection.indexes
    if indexes:
        idx = indexes[0]
        print(f"   - Index: {idx.params.get('index_type', 'N/A')}")
        print(f"   - Metric: {idx.params.get('metric_type', 'N/A')}")

    return collection


def main():
    parser = argparse.ArgumentParser(
        description="Initialize Milvus CLIP collection for image search"
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
        "--drop",
        action="store_true",
        help="Drop existing collection before creating",
    )
    parser.add_argument(
        "--verify-only",
        action="store_true",
        help="Only verify existing collection (don't create)",
    )

    args = parser.parse_args()

    try:
        if args.verify_only:
            print(f"🔍 Verifying collection: {args.collection}")
            connections.connect(alias="default", host=args.host, port=str(args.port))
            verify_collection(args.collection)
        else:
            create_clip_collection(
                collection_name=args.collection,
                host=args.host,
                port=args.port,
                drop_existing=args.drop,
            )
            verify_collection(args.collection)

        print("\n✅ Success! Collection is ready for CLIP image search.")

    except Exception as e:
        print(f"\n❌ Error: {e}", file=sys.stderr)
        sys.exit(1)
    finally:
        connections.disconnect(alias="default")


if __name__ == "__main__":
    main()
