import os
import redis
from ray import serve
from typing import Any, Dict, List


def _redis_client() -> redis.Redis:
    """
    Create and return a Redis client connection.

    Reads connection parameters from environment variables with fallback defaults.

    Returns:
        redis.Redis: Configured Redis client with string decoding enabled.
    """
    host = os.getenv("REDIS_HOST", "localhost")
    port = int(os.getenv("REDIS_PORT", "6379"))
    return redis.Redis(host=host, port=port, decode_responses=True)


@serve.deployment
class PopularityDeployment:
    """
    Ray Serve deployment for popularity-based recommendations.

    This deployment retrieves globally popular items from Redis, which can be
    used as a fallback strategy or to augment personalized recommendations.
    Popular items are stored in a Redis list, with metadata stored in hash maps.
    """

    def __init__(self):
        """
        Initialize the popularity deployment and connect to Redis.

        Sets up Redis connection and configures the key for accessing
        the global popularity list.
        """
        # Establish Redis connection for accessing popularity data
        self.redis = _redis_client()
        # Configure the Redis key for the global popularity list
        self.key = os.getenv("POPULARITY_KEY", "global:popular")

    def topk(self, k: int) -> List[Dict[str, Any]]:
        """
        Retrieve the top-k most popular items.

        Fetches article IDs from a Redis list (ordered by popularity) and
        enriches each with metadata stored in Redis hash maps.

        Args:
            k: Number of popular items to retrieve.

        Returns:
            List[Dict[str, Any]]: List of popular items with article_id, metadata, and score.
                Score is set to None for popularity-based results.
        """
        # Retrieve top-k article IDs from Redis list (0-indexed, so k-1)
        ids = self.redis.lrange(self.key, 0, max(0, k - 1))
        out: List[Dict[str, Any]] = []

        pipe = self.redis.pipeline()
        for aid in ids:
            pipe.hgetall(f"item:{aid}")
        rows = pipe.execute(raise_on_error=False)

        results = []
        for idx, (aid, meta) in enumerate(zip(ids, rows)):
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
