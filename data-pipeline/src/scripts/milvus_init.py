import math
import os
import sys

# Add parent directory to path to import from inference-service
sys.path.insert(
    0, os.path.join(os.path.dirname(__file__), "../../..", "inference-service")
)

import pandas as pd
from pymilvus import (
    Collection,
    CollectionSchema,
    DataType,
    FieldSchema,
    utility,
)
from src.ray_serve.utils.milvus_client import ensure_connection

DATA_PATH = os.getenv(
    "DATA_PATH", "data-pipeline/data/article_embeddings_bge_v2.parquet"
)
EMBEDDING_COL = os.getenv("EMBEDDING_COL", "embedding")

MILVUS_HOST = os.getenv("MILVUS_HOST", "localhost")
MILVUS_PORT = os.getenv("MILVUS_PORT", "19530")
COLLECTION = os.getenv("MILVUS_COLLECTION", "scale_style_bge_v2")

BATCH_SIZE = int(os.getenv("MILVUS_INSERT_BATCH", "2000"))


def main():
    assert os.path.exists(DATA_PATH), f"DATA_PATH not found: {DATA_PATH}"
    df = pd.read_parquet(DATA_PATH)

    assert "article_id" in df.columns, "parquet missing article_id"
    assert EMBEDDING_COL in df.columns, f"parquet missing {EMBEDDING_COL}"

    # infer dim
    first = df.iloc[0][EMBEDDING_COL]
    dim = len(first)

    # Connect to Milvus with automatic retry logic
    ensure_connection("default", max_retries=3, retry_delay=2.0)

    if utility.has_collection(COLLECTION):
        col = Collection(COLLECTION)
    else:
        fields = [
            FieldSchema(
                name="article_id", dtype=DataType.INT64, is_primary=True, auto_id=False
            ),
            FieldSchema(name="embedding", dtype=DataType.FLOAT_VECTOR, dim=dim),
            FieldSchema(
                name="colour_group_name", dtype=DataType.VARCHAR, max_length=64
            ),
            FieldSchema(name="price", dtype=DataType.FLOAT),
        ]
        schema = CollectionSchema(
            fields, description="ScaleStyle embeddings + scalar fields"
        )
        col = Collection(COLLECTION, schema=schema)

        index_params = {
            "metric_type": "COSINE",
            "index_type": "IVF_FLAT",
            "params": {"nlist": 2048},
        }
        col.create_index(field_name="embedding", index_params=index_params)

    # insert
    n = len(df)
    rounds = math.ceil(n / BATCH_SIZE)
    for i in range(rounds):
        chunk = df.iloc[i * BATCH_SIZE : (i + 1) * BATCH_SIZE]
        ids = chunk["article_id"].astype("int64").tolist()
        vecs = chunk[EMBEDDING_COL].tolist()
        colours = (
            chunk.get("colour_group_name", pd.Series([""] * len(chunk)))
            .fillna("")
            .astype(str)
            .tolist()
        )
        prices = (
            chunk.get("price", pd.Series([0.0] * len(chunk)))
            .fillna(0.0)
            .astype(float)
            .tolist()
        )

        col.insert([ids, vecs, colours, prices])
        print(f"[milvus_init] inserted batch {i+1}/{rounds} size={len(ids)}")

    col.flush()
    col.load()
    print(f"[milvus_init] done. collection={COLLECTION} rows={n} dim={dim}")


if __name__ == "__main__":
    main()
