#!/usr/bin/env python3
"""
Redis Cache Warmup Script

Manually preload product metadata into Redis for the Gateway service.
This is optional - the Gateway will auto-warm on first startup.

Usage:
    python warmup_redis.py [--redis-host localhost] [--redis-port 6379]
"""

import json
import sys
import argparse
import time
from pathlib import Path

try:
    import redis
except ImportError:
    print("ERROR: redis-py not installed. Run: pip install redis")
    sys.exit(1)


def warmup_redis(
    metadata_path: str, redis_host: str = "localhost", redis_port: int = 6379
):
    """Load product metadata from JSON into Redis."""

    print(f"[1/4] Connecting to Redis at {redis_host}:{redis_port}...")
    try:
        r = redis.Redis(
            host=redis_host,
            port=redis_port,
            decode_responses=False,  # We'll handle JSON serialization
            socket_timeout=5,
        )
        r.ping()
        print(f"✓ Connected to Redis (version: {r.info()['redis_version']})")
    except redis.ConnectionError as e:
        print(f"✗ Failed to connect to Redis: {e}")
        sys.exit(1)

    print(f"\n[2/4] Loading metadata from {metadata_path}...")
    metadata_file = Path(metadata_path)
    if not metadata_file.exists():
        print(f"✗ Metadata file not found: {metadata_path}")
        sys.exit(1)

    try:
        with open(metadata_file, "r", encoding="utf-8") as f:
            products = json.load(f)
        print(f"✓ Loaded {len(products)} products from JSON")
    except json.JSONDecodeError as e:
        print(f"✗ Invalid JSON file: {e}")
        sys.exit(1)

    if not products:
        print("✗ No products found in metadata file")
        sys.exit(1)

    print("\n[3/4] Writing to Redis with pipeline...")
    start_time = time.time()

    # Use pipeline for batch insert (100x faster than individual SET)
    pipe = r.pipeline()
    count = 0

    for product_id, product_data in products.items():
        redis_key = f"product:{product_id}"
        # Serialize as JSON string (matching Spring's Jackson serialization format)
        product_json = json.dumps(product_data, ensure_ascii=False)
        pipe.set(redis_key, product_json)

        count += 1
        # Execute in batches of 1000
        if count % 1000 == 0:
            pipe.execute()
            pipe = r.pipeline()
            print(f"  Progress: {count}/{len(products)} products...", end="\r")

    # Execute remaining
    if count % 1000 != 0:
        pipe.execute()

    # Set warmup flag
    r.set("product:cache:warmed", "true", ex=7 * 24 * 60 * 60)  # 7 days TTL

    elapsed = time.time() - start_time
    print(
        f"\n✓ Wrote {count} products to Redis in {elapsed:.2f}s ({count/elapsed:.0f} ops/sec)"
    )

    print("\n[4/4] Verifying random samples...")
    sample_keys = list(products.keys())[:5]
    for product_id in sample_keys:
        redis_key = f"product:{product_id}"
        exists = r.exists(redis_key)
        print(f"  {redis_key}: {'✓' if exists else '✗'}")

    print("\n" + "=" * 60)
    print("Redis cache warmup completed successfully!")
    print(f"Total products: {count}")
    print(f"Redis memory used: {r.info()['used_memory_human']}")
    print("=" * 60)


def main():
    parser = argparse.ArgumentParser(description="Warm up Redis with product metadata")
    parser.add_argument(
        "--metadata-path",
        default="./data-pipeline/data/processed/product_metadata.json",
        help="Path to product metadata JSON file",
    )
    parser.add_argument(
        "--redis-host", default="localhost", help="Redis host (default: localhost)"
    )
    parser.add_argument(
        "--redis-port", type=int, default=6379, help="Redis port (default: 6379)"
    )

    args = parser.parse_args()

    print("=" * 60)
    print("Redis Cache Warmup for ScaleStyle Gateway")
    print("=" * 60)

    warmup_redis(args.metadata_path, args.redis_host, args.redis_port)


if __name__ == "__main__":
    main()
