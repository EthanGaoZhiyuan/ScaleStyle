import os
import redis

class RedisClient:
    _instance = None
    _client = None

    @classmethod
    def get_client(cls):
        if cls._client is None:
            host = os.getenv("REDIS_HOST", "localhost")
            port = int(os.getenv("REDIS_PORT", 6379))
            print(f"[RedisClient] Connecting to Redis at {host}:{port}...")
            cls._client = redis.Redis(host=host, port=port, decode_responses=True)
        return cls._client