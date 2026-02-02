#!/usr/bin/env python3
"""
Quick Redis data loader (FAST version - only essential data)
Loads minimal data needed for search to work
"""

import json
import sys

import redis


def main():
    # Connect to Redis
    print("Connecting to Redis...")
    r = redis.Redis(host="localhost", port=6379, db=0)

    try:
        r.ping()
        print("✅ Redis connection successful")
    except Exception as e:
        print(f"❌ Redis connection failed: {e}")
        sys.exit(1)

    # Load metadata
    print("\nLoading product metadata...")
    try:
        with open("data/processed/product_metadata.json") as f:
            metadata = json.load(f)
    except FileNotFoundError:
        print("❌ product_metadata.json not found")
        sys.exit(1)

    print(f"Found {len(metadata)} products in metadata")

    # Generate popular items list FIRST (most important for fallback)
    # Gateway expects ZSET, not list! Use ZADD with scores
    print("\n✅ PRIORITY: Generating popular items ZSET...")
    popular_ids = list(metadata.keys())[:100]
    r.delete("global:popular")
    # ZADD expects: key, {member1: score1, member2: score2, ...}
    # Higher score = more popular (gateway uses reverseRange)
    popular_mapping = {pid: 100 - i for i, pid in enumerate(popular_ids)}
    r.zadd("global:popular", popular_mapping)
    print(f"Loaded {len(popular_ids)} popular items to ZSET (scores: 100-1)")
    print(f"✅ Loaded {len(popular_ids)} popular items to global:popular")

    # Load only popular items' metadata to Redis (fast!)
    print("\n✅ Loading metadata for popular items only...")
    pipe = r.pipeline()
    count = 0

    for article_id in popular_ids:
        if article_id in metadata:
            meta = metadata[article_id]
            key = f"item:{article_id}"
            pipe.hset(
                key,
                mapping={
                    "title": meta.get("name", meta.get("prod_name", "Unknown Product")),
                    "dept": meta.get(
                        "category", meta.get("product_group_name", "General")
                    ),
                    "color": meta.get("colour_group_name", "N/A"),
                    "price": str(meta.get("price", 0.0)),
                    "desc": meta.get(
                        "description", meta.get("detail_desc", "No description")
                    ),
                    "image_url": meta.get(
                        "imgUrl", f"https://via.placeholder.com/300?text={article_id}"
                    ),
                },
            )
            count += 1

    pipe.execute()
    print(f"✅ Loaded {count} popular items' metadata to Redis")

    # Optionally load more items in background (for vector search hits)
    print("\n✅ Loading additional items (batched)...")
    all_ids = list(metadata.keys())
    batch_size = 1000
    total_loaded = count

    # Load first 10k items (enough for most queries)
    for i in range(len(popular_ids), min(10000, len(all_ids)), batch_size):
        pipe = r.pipeline()
        batch = all_ids[i : i + batch_size]

        for article_id in batch:
            if article_id in metadata:
                meta = metadata[article_id]
                key = f"item:{article_id}"
                pipe.hset(
                    key,
                    mapping={
                        "title": meta.get(
                            "name", meta.get("prod_name", "Unknown Product")
                        ),
                        "dept": meta.get(
                            "category", meta.get("product_group_name", "General")
                        ),
                        "color": meta.get("colour_group_name", "N/A"),
                        "price": str(meta.get("price", 0.0)),
                        "desc": meta.get(
                            "description", meta.get("detail_desc", "No description")
                        ),
                        "image_url": meta.get(
                            "imgUrl",
                            f"https://via.placeholder.com/300?text={article_id}",
                        ),
                    },
                )
                total_loaded += 1

        pipe.execute()
        print(f"  Loaded {total_loaded} items...")

    print(f"✅ Loaded {total_loaded} items total to Redis")

    # Verify
    print("\n=== Verification ===")
    db_size = r.dbsize()
    popular_count = r.zcard("global:popular")  # Use ZCARD for ZSET
    print(f"Total Redis keys: {db_size}")
    print(f"Popular items count: {popular_count}")

    if popular_count > 0:
        sample_ids = [
            x.decode() if isinstance(x, bytes) else x
            for x in r.zrevrange("global:popular", 0, 2)
        ]  # Use ZREVRANGE for ZSET
        print(f"Sample popular IDs: {sample_ids}")

        # Check if metadata exists for sample
        for sample_id in sample_ids[:1]:
            key = f"item:{sample_id}"
            if r.exists(key):
                title = r.hget(key, "title")
                if title:
                    title = title.decode() if isinstance(title, bytes) else title
                    print(f"  {key}: title = '{title}'")

    print("\n✅ Redis data loading complete!")
    print("\nNow test search:")
    print("  curl 'http://localhost:8080/api/recommendation/search?query=dress&k=5'")


if __name__ == "__main__":
    main()
