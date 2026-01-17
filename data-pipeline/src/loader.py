import os

import pandas as pd
import redis
from pymilvus import MilvusClient
from tqdm import tqdm

# --- Configuration ---
DATA_PATH = os.getenv("DATA_PATH", "data-pipeline/data/articles.parquet")

MILVUS_HOST = os.getenv("MILVUS_HOST", "localhost")
MILVUS_PORT = os.getenv("MILVUS_PORT", "19530")
MILVUS_COLLECTION = os.getenv("MILVUS_COLLECTION", "scale_style_bge_v2")

REDIS_HOST = os.getenv("REDIS_HOST", "localhost")
REDIS_PORT = int(os.getenv("REDIS_PORT", "6379"))

# This loader assumes embeddings are already computed and stored in parquet column "embedding"
# embedding dim must match the collection dim
EMBEDDING_COL = os.getenv("EMBEDDING_COL", "embedding")

POPULARITY_KEY = os.getenv("POPULARITY_KEY", "global:popular")
POPULARITY_TOPN = int(os.getenv("POPULARITY_TOPN", "200"))


def redis_client() -> redis.Redis:
    return redis.Redis(host=REDIS_HOST, port=REDIS_PORT, decode_responses=True)


def main():
    assert os.path.exists(DATA_PATH), f"DATA_PATH not found: {DATA_PATH}"
    df = pd.read_parquet(DATA_PATH)
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

    # ---------- 2) Popularity list ----------
    print(
        f"[loader] writing popularity list key={POPULARITY_KEY} size={len(popularity_ids)}"
    )
    r.delete(POPULARITY_KEY)
    if popularity_ids:
        r.rpush(POPULARITY_KEY, *popularity_ids)

    # ---------- 3) Milvus load check ----------
    uri = f"http://{MILVUS_HOST}:{MILVUS_PORT}"
    mc = MilvusClient(uri=uri)
    mc.load_collection(MILVUS_COLLECTION)
    print(f"[loader] milvus ready uri={uri} collection={MILVUS_COLLECTION}")

    print("[loader] done.")


if __name__ == "__main__":
    main()
