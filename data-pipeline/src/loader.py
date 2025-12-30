import os
import json
import time
import pandas as pd
import redis
from pymilvus import (
    connections,
    FieldSchema, CollectionSchema, DataType,
    Collection, utility
)
from tqdm import tqdm

# --- Configuration ---
MILVUS_HOST = "localhost"
MILVUS_PORT = "19530"
REDIS_HOST = "localhost"
REDIS_PORT = 6379
VECTOR_DIM = 3584
COLLECTION_NAME = "scale_style_articles"

# --- Path Setup ---
current_dir = os.path.dirname(os.path.abspath(__file__))
DATA_PATH = os.path.abspath(os.path.join(current_dir, "../data/processed/article_embeddings_qwen2.parquet"))

def connect_services():
    """Connect to Milvus and Redis services."""
    connections.connect("default", host=MILVUS_HOST, port=MILVUS_PORT)
    
    r = redis.Redis(host=REDIS_HOST, port=REDIS_PORT, decode_responses=True)
    return r

def create_milvus_collection():
    """Create Milvus collection schema."""
    # 1. Check if exists and drop (Ensure fresh start on every run)
    if utility.has_collection(COLLECTION_NAME):
        utility.drop_collection(COLLECTION_NAME)

    # 2. Define Schema
    print("Creating Milvus Schema...")
    fields = [
        # Primary Key ID (Int64)
        FieldSchema(name="article_id", dtype=DataType.INT64, is_primary=True, auto_id=False),
        # Vector Field (FloatVector, dim=3584)
        FieldSchema(name="embedding", dtype=DataType.FLOAT_VECTOR, dim=VECTOR_DIM)
    ]
    schema = CollectionSchema(fields, description="H&M Article Embeddings (Qwen2-7B)")

    # 3. Create Collection
    collection = Collection(name=COLLECTION_NAME, schema=schema)
    print(f"Collection {COLLECTION_NAME} created successfully.")
    return collection

def insert_data(redis_client):
    """Read Parquet file and insert data in batches."""
    # ---------------------- Path Check ----------------------
    print(f"Reading data from {DATA_PATH}...")
    if not os.path.exists(DATA_PATH):
        raise FileNotFoundError(f"File not found: {DATA_PATH}. Please download it from Drive first!")
        
    df = pd.read_parquet(DATA_PATH)
    total_rows = len(df)
    print(f"Loaded {total_rows} rows.")

    # ---------------------- 1. Write to Redis ----------------------
    print("Writing metadata to Redis (Pipeline)...")
    pipe = redis_client.pipeline()
    
    # Print column names for verification
    print(f"   Columns: {df.columns.tolist()}")
    
    for _, row in tqdm(df.iterrows(), total=total_rows, desc="Redis Write"):
        key = f"item:{row['article_id']}"
        # Only storing ID here. In a real project, merge more item metadata (e.g., name, price).
        value = json.dumps({"article_id": row['article_id']})
        pipe.set(key, value)
        
        if _ % 5000 == 0:
            pipe.execute()
    pipe.execute() 

    # ---------------------- 2. Write to Milvus (Batch Insert) ----------------------
    print("Inserting vectors into Milvus (Batch Mode)...")
    collection = Collection(COLLECTION_NAME)
    
    # CRITICAL: Set Batch Size
    # Qwen2 vectors are large (3584 dim), approx 14KB per row.
    # Milvus gRPC limit is 64MB. 
    # Calculation: 64MB / 14KB ≈ 4500 rows.
    # To be safe, we set it to 2000 rows per batch.
    BATCH_SIZE = 2000
    
    for i in tqdm(range(0, total_rows, BATCH_SIZE), desc="Milvus Insert"):
        # Slice data
        batch_df = df.iloc[i : i + BATCH_SIZE]
        
        # Organize data: [ [ids...], [vectors...] ]
        data_to_insert = [
            batch_df['article_id'].tolist(),
            batch_df['embedding'].tolist()
        ]
        
        # Insert current batch
        collection.insert(data_to_insert)
    
    # Flush to disk after insertion is complete
    collection.flush()
    print(f"Milvus insertion complete. Row count: {collection.num_entities}")

    # ---------------------- 3. Create Index ----------------------
    # Since Qwen2 dimension is large, suggest a larger nlist, e.g., 2048
    index_params = {
        "metric_type": "COSINE", 
        "index_type": "IVF_FLAT",
        "params": {"nlist": 2048}
    }
    collection.create_index(field_name="embedding", index_params=index_params)
    
    # ---------------------- 4. Load Collection ----------------------
    collection.load()

if __name__ == "__main__":
    try:
        r = connect_services()
        create_milvus_collection()
        insert_data(r)
    except Exception as e:
        print(f"\n Error: {e}")