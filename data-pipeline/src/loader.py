import os

import pandas as pd
import redis
from pymilvus import MilvusClient
from tqdm import tqdm

from config import (
    ARTICLE_EMBEDDINGS_PATH,
    MILVUS_COLLECTION,
    MILVUS_HOST,
    MILVUS_PORT,
    POPULARITY_KEY,
    POPULARITY_TOPN,
    REDIS_HOST,
    REDIS_PORT,
)


def redis_client() -> redis.Redis:
    return redis.Redis(host=REDIS_HOST, port=REDIS_PORT, decode_responses=True)


def main():
    assert os.path.exists(
        ARTICLE_EMBEDDINGS_PATH
    ), f"DATA_PATH not found: {ARTICLE_EMBEDDINGS_PATH}"
    df = pd.read_parquet(ARTICLE_EMBEDDINGS_PATH)
    total_rows = len(df)
    print(f"[loader] loaded rows={total_rows} cols={df.columns.tolist()}")

    r = redis_client()

    # ---------- 1) Redis metadata ----------
    # Minimum Week1 fields: colour_group_name
    # Recommended extras: product_type_name, department_name, detail_desc, image_url, price
    print("[loader] writing metadata to Redis...")
    pipe = r.pipeline()

    popularity_ids = []

    for _, row in tqdm(df.iterrows(), total=total_rows, desc="Redis metadata"):
        article_id = int(row["article_id"])
        key = f"item:{article_id}"

        meta = {
            "article_id": str(article_id),
            "colour_group_name": str(row.get("colour_group_name", "")),
            "product_type_name": str(row.get("product_type_name", "")),
            "department_name": str(row.get("department_name", "")),
            "detail_desc": str(row.get("detail_desc", ""))[:5000],
        }

        # Optional fields
        if "price" in df.columns:
            meta["price"] = str(row.get("price", ""))

        if "image_url" in df.columns:
            meta["image_url"] = str(row.get("image_url", ""))

        pipe.hset(key, mapping=meta)

        # build a simple popularity list (Week1: just first N)
        if len(popularity_ids) < POPULARITY_TOPN:
            popularity_ids.append(str(article_id))

    pipe.execute()

    # ---------- 2) Popularity ZSET (gateway expects ZSET, not list) ----------
    print(
        f"[loader] writing popularity ZSET key={POPULARITY_KEY} size={len(popularity_ids)}"
    )
    r.delete(POPULARITY_KEY)
    if popularity_ids:
        # Use ZADD with scores: higher score = more popular
        popularity_mapping = {
            pid: len(popularity_ids) - i for i, pid in enumerate(popularity_ids)
        }
        r.zadd(POPULARITY_KEY, popularity_mapping)

    # ---------- 3) Milvus load check ----------
    uri = f"http://{MILVUS_HOST}:{MILVUS_PORT}"
    mc = MilvusClient(uri=uri)
    mc.load_collection(MILVUS_COLLECTION)
    print(f"[loader] milvus ready uri={uri} collection={MILVUS_COLLECTION}")

    print("[loader] done.")


if __name__ == "__main__":
    main()
