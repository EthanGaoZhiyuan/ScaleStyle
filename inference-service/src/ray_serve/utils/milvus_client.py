import os
from pymilvus import connections
import time

class MilvusClient:
    @staticmethod
    def ensure_connection(alias="default"):
        host = os.getenv("MILVUS_HOST", "localhost")
        port = os.getenv("MILVUS_PORT", "19530")
        
        # ckeck connection
        if connections.has_connection(alias):
            return
        
        for i in range(3):
            try:
                connections.connect(alias, host=host, port=port)
                return
            except Exception as e:
                print(f"[MilvusClient] Connection attempt {i+1} failed: {e}")
                time.sleep(2)
                
        raise ConnectionError(f"[MilvusClient] Could not connect to Milvus at {host}:{port} after 3 attempts.")
