"""
Configuration management for data pipeline.
"""

import os
from pathlib import Path

# Base directory (project root)
BASE_DIR = Path(__file__).parent.parent.resolve()

# Data directories
DATA_DIR = BASE_DIR / "data"
RAW_DATA_DIR = DATA_DIR / "raw"
PROCESSED_DATA_DIR = DATA_DIR / "processed"

# Input files
ARTICLES_CSV_PATH = os.getenv("ARTICLES_CSV_PATH", str(RAW_DATA_DIR / "articles.csv"))

# Output files
PRODUCT_METADATA_JSON_PATH = os.getenv(
    "PRODUCT_METADATA_JSON_PATH", str(PROCESSED_DATA_DIR / "product_metadata.json")
)

# Training and processed data paths
TRAIN_DATA_PARQUET_PATH = os.getenv(
    "TRAIN_DATA_PARQUET_PATH", str(PROCESSED_DATA_DIR / "train_data_parquet")
)

TOP_ITEMS_PARQUET_PATH = os.getenv(
    "TOP_ITEMS_PARQUET_PATH", str(PROCESSED_DATA_DIR / "top_items_parquet")
)

ARTICLE_EMBEDDINGS_PATH = os.getenv(
    "ARTICLE_EMBEDDINGS_PATH",
    str(BASE_DIR / "data" / "article_embeddings_bge_detail.parquet"),
)

# Embedding configuration
EMBEDDING_COL = os.getenv("EMBEDDING_COL", "embedding")

# Milvus configuration
MILVUS_HOST = os.getenv("MILVUS_HOST", "localhost")
MILVUS_PORT = os.getenv("MILVUS_PORT", "19530")
MILVUS_COLLECTION = os.getenv("MILVUS_COLLECTION", "scale_style_bge_v2")

# Redis configuration
REDIS_HOST = os.getenv("REDIS_HOST", "localhost")
REDIS_PORT = int(os.getenv("REDIS_PORT", "6379"))
POPULARITY_KEY = os.getenv("POPULARITY_KEY", "global:popular")
POPULARITY_TOPN = int(os.getenv("POPULARITY_TOPN", "200"))

# Price configuration
MIN_PRICE = float(os.getenv("MIN_PRICE", "19.99"))
MAX_PRICE = float(os.getenv("MAX_PRICE", "99.99"))

# Image URL template
IMAGE_URL_TEMPLATE = os.getenv(
    "IMAGE_URL_TEMPLATE",
    "https://lp2.hm.com/hmgoepprod?set=source[/{folder}/{article_id}.jpg],"
    "origin[dam],category[],type[LOOKBOOK],res[m],hmver[1]&call=url[file:/product/main]",
)
