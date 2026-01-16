import os
import redis
from ray import serve
from typing import Any, Dict, List

def _redis_client() -> redis.Redis:
    host = os.getenv("REDIS_HOST", "localhost")
    port = int(os.getenv("REDIS_PORT", "6379"))
    return redis.Redis(host=host, port=port, decode_responses=True)

@serve.deployment
class PopularityDeployment:
    def __init__(self):
        self.redis = _redis_client()
        self.key = os.getenv("POPULARITY_KEY", "global:popular")

    def topk(self, k: int) -> List[Dict[str, Any]]:
        ids = self.redis.lrange(self.key, 0, max(0, k - 1))
        out: List[Dict[str, Any]] = []
        for aid in ids:
            meta = self.redis.hgetall(f"item:{aid}") or {}
            out.append({"article_id": int(aid) if str(aid).isdigit() else aid, "meta": meta, "score": None})
        return out
