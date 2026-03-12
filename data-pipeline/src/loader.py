import os

import pandas as pd
import redis
from pymilvus import MilvusClient

from bootstrap_data import load_redis_data
from config import (
    ARTICLE_EMBEDDINGS_PATH,
    MILVUS_COLLECTION,
    MILVUS_HOST,
    MILVUS_PORT,
    REDIS_HOST,
    REDIS_PORT,
    REDIS_TLS,
)


def redis_client() -> redis.Redis:
    return redis.Redis(
        host=REDIS_HOST,
        port=REDIS_PORT,
        decode_responses=True,
        ssl=REDIS_TLS,
        ssl_cert_reqs="required" if REDIS_TLS else None,
    )


def main():
    assert os.path.exists(
        ARTICLE_EMBEDDINGS_PATH
    ), f"DATA_PATH not found: {ARTICLE_EMBEDDINGS_PATH}"
    df = pd.read_parquet(ARTICLE_EMBEDDINGS_PATH)
    total_rows = len(df)
    print(f"[loader] loaded rows={total_rows} cols={df.columns.tolist()}")

    load_redis_data(df)

    # ---------- 3) Milvus load check ----------
    uri = f"http://{MILVUS_HOST}:{MILVUS_PORT}"
    mc = MilvusClient(uri=uri)
    mc.load_collection(MILVUS_COLLECTION)
    print(f"[loader] milvus ready uri={uri} collection={MILVUS_COLLECTION}")

    print("[loader] done.")


if __name__ == "__main__":
    main()
