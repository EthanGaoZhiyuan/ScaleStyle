import redis
from src.config import RedisConfig


class RedisClient:
    _instance = None
    _client = None

    @classmethod
    def get_client(cls):
        if cls._client is None:
            print(
                f"[RedisClient] Connecting to Redis at {RedisConfig.HOST}:{RedisConfig.PORT}..."
            )
            cls._client = redis.Redis(
                host=RedisConfig.HOST, port=RedisConfig.PORT, decode_responses=True
            )
        return cls._client
