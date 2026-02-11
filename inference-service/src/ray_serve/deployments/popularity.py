import asyncio
import logging
import redis
from ray import serve
from typing import Any, Dict, List

from src.config import RedisConfig

logger = logging.getLogger(__name__)


def _redis_client() -> redis.Redis:
    """
    Create and return a Redis client connection.

    Reads connection parameters from centralized config.

    Returns:
        redis.Redis: Configured Redis client with string decoding enabled.
    """
    return redis.Redis(
        host=RedisConfig.HOST, port=RedisConfig.PORT, decode_responses=True
    )


@serve.deployment(
    # Simple Redis query needs minimal CPU
    ray_actor_options={"num_cpus": 0.1}
)
class PopularityDeployment:
    """
    Ray Serve deployment for popularity-based recommendations.

    This deployment retrieves globally popular items from Redis, which can be
    used as a fallback strategy or to augment personalized recommendations.
    Popular items are stored in a Redis sorted set (ZSET), with metadata stored in hash maps.
    """

    def __init__(self):
        """
        Initialize the popularity deployment and connect to Redis.

        Sets up Redis connection and configures the key for accessing
        the global popularity sorted set.
        """
        # Establish Redis connection for accessing popularity data
        self.redis = _redis_client()
        # Configure the Redis key for the global popularity sorted set
        self.key = RedisConfig.POPULARITY_KEY

    async def topk(self, k: int) -> List[Dict[str, Any]]:
        """
        Retrieve the top-k most popular items (async).

        Fetches article IDs from a Redis sorted set (ordered by popularity score) and
        enriches each with metadata stored in Redis hash maps.

        Uses asyncio.to_thread() to prevent Redis operations from blocking
        the Ray Serve event loop.

        Args:
            k: Number of popular items to retrieve.

        Returns:
            List[Dict[str, Any]]: List of popular items with article_id, metadata, and score.
                Score is derived from the ZSET ranking.
        """
        # Fetch top-k article IDs from Redis sorted set (highest scores first)
        # ZREVRANGE returns items in descending order by score
        try:
            ids = await asyncio.to_thread(
                self.redis.zrevrange, self.key, 0, max(0, k - 1)
            )
        except Exception as e:
            logger.error(f"Failed to read popular items from {self.key}: {e}")
            return []

        pipe = self.redis.pipeline()
        for aid in ids:
            pipe.hgetall(f"item:{aid}")
        # Execute pipeline async to avoid blocking
        rows = await asyncio.to_thread(pipe.execute, raise_on_error=False)

        results = []
        for idx, (aid, meta) in enumerate(zip(ids, rows, strict=True)):
            if isinstance(meta, Exception):
                meta = {}
            results.append(
                {
                    "article_id": int(aid) if str(aid).isdigit() else aid,
                    "score": float(len(ids) - idx),
                    "meta": meta or {},
                }
            )
        return results
