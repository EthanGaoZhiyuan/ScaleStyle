import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

from src.deployments.popularity import PopularityDeployment


class _DummyPipeline:
    def __init__(self, execute_result):
        self.execute_result = execute_result
        self.keys = []

    def zunionstore(self, key, bucket_keys, aggregate="SUM"):
        self.keys.append(("zunionstore", key, bucket_keys, aggregate))
        return self

    def expire(self, key, ttl):
        self.keys.append(("expire", key, ttl))
        return self

    def hgetall(self, key):
        self.keys.append(("hgetall", key))
        return self

    def execute(self, **kwargs):
        return self.execute_result


class _DummyRedis:
    def __init__(self):
        self.window_scores = {}
        self._pipeline_results = []
        self.ttl_values = {}
        self.set_results = []
        self.deleted_keys = []

    def ping(self):
        return True

    def ttl(self, key):
        return self.ttl_values.get(key, -2)

    def set(self, key, value, nx=False, px=None):
        if not nx:
            raise AssertionError("materialization lease must use nx=True")
        if px is None:
            raise AssertionError("materialization lease must set a bounded ttl")
        if self.set_results:
            return self.set_results.pop(0)
        return True

    def delete(self, key):
        self.deleted_keys.append(key)
        return 1

    def pipeline(self, transaction=False):
        if transaction:
            raise AssertionError("popularity path should use non-transactional pipeline")
        if not self._pipeline_results:
            raise AssertionError("No pipeline result configured")
        return _DummyPipeline(self._pipeline_results.pop(0))

    def zrevrange(self, key, start, end, withscores=False):
        values = self.window_scores.get(key, [])
        return values if withscores else [item_id for item_id, _ in values]


def test_popularity_topk_prefers_primary_window_and_falls_back_to_secondary(monkeypatch):
    redis_client = _DummyRedis()

    primary_key = "popularity:materialized:24h:test"
    secondary_key = "popularity:materialized:7d:test"
    redis_client.window_scores[primary_key] = []
    redis_client.window_scores[secondary_key] = [("stale_item", 3.0)]
    redis_client._pipeline_results = [
        [{"article_id": "stale_item", "detail_desc": "older but still relevant"}],
    ]

    monkeypatch.setattr("src.deployments.popularity.RedisClient.get_client", lambda: redis_client)

    deployment = PopularityDeployment()
    monkeypatch.setattr(deployment, "_ensure_materialized_window", lambda window_name, now_ts=None: primary_key if window_name == "24h" else secondary_key)
    results = asyncio.run(deployment.topk(1))

    assert results[0]["article_id"] == "stale_item"
    assert results[0]["score"] == 3.0


def test_materialized_window_rebuild_uses_short_redis_lease(monkeypatch):
    redis_client = _DummyRedis()
    redis_client.set_results = [True]
    redis_client._pipeline_results = [None]

    monkeypatch.setattr("src.deployments.popularity.RedisClient.get_client", lambda: redis_client)

    deployment = PopularityDeployment()
    materialized_key = deployment._ensure_materialized_window("24h", now_ts=3600.0)

    assert materialized_key.endswith(":3600")
    assert redis_client.deleted_keys == [f"lock:{materialized_key}"]


def test_materialized_window_skips_duplicate_rebuild_when_peer_holds_lease(monkeypatch):
    redis_client = _DummyRedis()
    redis_client.set_results = [False]

    monkeypatch.setattr("src.deployments.popularity.RedisClient.get_client", lambda: redis_client)

    deployment = PopularityDeployment()
    materialized_key = deployment._ensure_materialized_window("24h", now_ts=3600.0)

    assert materialized_key.endswith(":3600")
    assert redis_client.deleted_keys == []
    assert redis_client._pipeline_results == []
