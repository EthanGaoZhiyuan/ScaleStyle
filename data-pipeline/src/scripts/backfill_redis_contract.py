"""
Backfill script to ensure Redis item metadata meets contract requirements.

This script scans all Redis items and adds missing required fields to ensure
API contract compliance. Required fields include title, image_url, department_name,
detail_desc, price, and colour_group_name.
"""

import os
import redis

# Redis connection configuration from environment variables
REDIS_HOST = os.getenv("REDIS_HOST", "localhost")
REDIS_PORT = int(os.getenv("REDIS_PORT", "6379"))

# Fields that must be present in every item (even if empty)
REQUIRED_FIELDS = ["title", "image_url", "department_name", "detail_desc", "price", "colour_group_name"]

def image_path_from_article_id(article_id: str) -> str:
    """
    Generate image path from article ID.
    
    Pads the article ID to 10 digits and constructs a path using the first
    3 digits as a folder name. This matches the expected image storage structure.
    
    Args:
        article_id: Article identifier (will be zero-padded to 10 digits).
    
    Returns:
        str: Image path in format /static/images/{folder}/{article_id}.jpg
    
    Example:
        image_path_from_article_id("12345") -> "/static/images/000/0000012345.jpg"
    """
    # Strip whitespace and zero-pad to 10 digits
    aid10 = str(article_id).strip()
    aid10 = aid10.zfill(10)
    # Use first 3 digits as folder name for organization
    folder = aid10[:3]
    return f"/static/images/{folder}/{aid10}.jpg"

# Establish Redis connection with string decoding enabled
r = redis.Redis(host=REDIS_HOST, port=REDIS_PORT, decode_responses=True)

# Initialize cursor for scanning and counter for tracking updates
cursor = 0
updated = 0

# Main scanning loop: iterate through all items in Redis using SCAN
while True:
    # Scan for item keys in batches (count=2000 for efficiency)
    cursor, keys = r.scan(cursor=cursor, match="item:*", count=2000)
    # Skip if no keys found in this batch
    if not keys:
        if cursor == 0:
            break
        continue

    # Batch fetch all item metadata using pipeline for efficiency
    pipe = r.pipeline()
    for k in keys:
        pipe.hgetall(k)
    rows = pipe.execute()

    # Prepare batch updates for items with missing fields
    pipe2 = r.pipeline()
    for k, raw in zip(keys, rows, strict=True, strict=True):
        # Extract article ID from metadata or key
        aid = raw.get("article_id") or k.split("item:")[-1]

        # Collect fields that need to be added or updated
        mapping = {}

        # Ensure 'title' field exists (fallback to prod_name or product_type_name)
        if not raw.get("title"):
            title = raw.get("prod_name") or raw.get("product_type_name") or ""
            mapping["title"] = title

        # Generate image_url if missing
        if not raw.get("image_url"):
            mapping["image_url"] = image_path_from_article_id(aid)

        # Ensure all required fields exist (even if empty)
        if "department_name" not in raw:
            mapping["department_name"] = ""
        if "detail_desc" not in raw:
            mapping["detail_desc"] = ""
        if "price" not in raw:
            mapping["price"] = ""
        if "colour_group_name" not in raw:
            mapping["colour_group_name"] = ""

        # Add update to pipeline if any fields need to be set
        if mapping:
            pipe2.hset(k, mapping=mapping)
            updated += 1

    # Execute all updates in batch
    pipe2.execute()

    # Exit loop when scan cursor returns to 0 (full scan complete)
    if cursor == 0:
        break

# Print completion summary
print(f"done. updated_keys={updated}")
