import asyncio
import logging
import time
from ray import serve
from typing import Any, Dict, List

from src.config import RedisConfig
from src.personalization.popularity_windows import active_bucket_keys, materialized_window_key
from src.utils.redis_client import RedisClient
from src.utils.redis_metadata import canonical_article_id, item_key
from src.utils.metrics import counter, histogram

logger = logging.getLogger(__name__)


@serve.deployment(
    # Simple Redis query needs minimal CPU
    ray_actor_options={"num_cpus": 0.05}
)
class PopularityDeployment:
    """
    Ray Serve deployment for popularity-based recommendations.

    This deployment retrieves rolling-window popularity from Redis. The 24h
    window is the primary fallback because it adapts to recent demand while
    remaining more stable than a pure 1h spike detector.
    """

    _MATERIALIZED_LOCK_TTL_MS = 5000

    def __init__(self):
        """
        Initialize the popularity deployment and connect to Redis.

        Sets up Redis connection and popularity window configuration.
        """
        self.redis = RedisClient.get_client()
        self._window_specs = {
            "1h": {
                "bucket_seconds": RedisConfig.POPULARITY_1H_BUCKET_SECONDS,
                "bucket_count": RedisConfig.POPULARITY_1H_BUCKET_COUNT,
                "materialized_ttl_seconds": RedisConfig.POPULARITY_1H_MATERIALIZED_TTL_SECONDS,
            },
            "24h": {
                "bucket_seconds": RedisConfig.POPULARITY_24H_BUCKET_SECONDS,
                "bucket_count": RedisConfig.POPULARITY_24H_BUCKET_COUNT,
                "materialized_ttl_seconds": RedisConfig.POPULARITY_24H_MATERIALIZED_TTL_SECONDS,
            },
            "7d": {
                "bucket_seconds": RedisConfig.POPULARITY_7D_BUCKET_SECONDS,
                "bucket_count": RedisConfig.POPULARITY_7D_BUCKET_COUNT,
                "materialized_ttl_seconds": RedisConfig.POPULARITY_7D_MATERIALIZED_TTL_SECONDS,
            },
        }
        self.primary_window = RedisConfig.POPULARITY_PRIMARY_WINDOW
        self.secondary_window = RedisConfig.POPULARITY_SECONDARY_WINDOW
        self.metrics = {
            "requests_total": counter(
                "popularity_requests_total",
                "Popularity fallback request count by status",
                ["status"],
            ),
            "request_duration_seconds": histogram(
                "popularity_request_duration_seconds",
                "Popularity fallback request latency",
                [],
                buckets=(0.001, 0.005, 0.01, 0.02, 0.05, 0.1, 0.25, 0.5, 1.0),
            ),
            "redis_ops_total": counter(
                "popularity_redis_ops_total",
                "Popularity Redis operation count by status",
                ["operation", "status"],
            ),
            "redis_op_duration_seconds": histogram(
                "popularity_redis_op_duration_seconds",
                "Popularity Redis operation latency by operation",
                ["operation"],
                buckets=(0.001, 0.005, 0.01, 0.02, 0.05, 0.1, 0.25),
            ),
        }

    def _materialization_lock_key(self, materialized_key: str) -> str:
        return f"lock:{materialized_key}"

    def _ensure_materialized_window(self, window_name: str, now_ts: float | None = None) -> str:
        spec = self._window_specs[window_name]
        materialized_key = materialized_window_key(window_name, spec["bucket_seconds"], now_ts)
        ttl = self.redis.ttl(materialized_key)
        if ttl > 0:
            return materialized_key

        lock_key = self._materialization_lock_key(materialized_key)
        lock_acquired = self.redis.set(
            lock_key,
            "1",
            nx=True,
            px=self._MATERIALIZED_LOCK_TTL_MS,
        )
        if not lock_acquired:
            # Best-effort single-writer guard: if another pod already holds the
            # short lease for this exact materialized window key, skip duplicate
            # rebuild work and let the normal read path observe the result once
            # it becomes visible. The lease auto-expires, so there is no blocking
            # coordination or risk of a stuck global lock.
            logger.debug(
                "materialized_window_rebuild_skipped window=%s key=%s reason=lock_held_by_peer",
                window_name,
                materialized_key,
            )
            return materialized_key

        keys = active_bucket_keys(
            window_name,
            spec["bucket_seconds"],
            spec["bucket_count"],
            now_ts,
        )
        try:
            pipe = self.redis.pipeline(transaction=False)
            pipe.zunionstore(materialized_key, keys, aggregate="SUM")
            pipe.expire(materialized_key, spec["materialized_ttl_seconds"])
            pipe.execute()
        finally:
            try:
                self.redis.delete(lock_key)
            except Exception:
                logger.debug(
                    "materialized_window_lock_release_failed window=%s key=%s",
                    window_name,
                    materialized_key,
                    exc_info=True,
                )
        return materialized_key

    async def is_ready(self) -> bool:
        """Report whether the popularity dependency can currently serve requests."""
        try:
            await asyncio.to_thread(self.redis.ping)
            return True
        except Exception:
            return False

    async def topk(self, k: int) -> List[Dict[str, Any]]:
        """
        Retrieve the top-k most popular items (async).

        Fetches article IDs from the rolling 24h materialized popularity window.
        If that window is empty, falls back to 7d for graceful degradation.

        Uses asyncio.to_thread() to prevent Redis operations from blocking
        the Ray Serve event loop.

        Args:
            k: Number of popular items to retrieve.

        Returns:
            List[Dict[str, Any]]: List of popular items with article_id, metadata, and score.
        """
        t_request_start = time.perf_counter()
        try:
            materialize_start = time.perf_counter()
            primary_key = await asyncio.to_thread(self._ensure_materialized_window, self.primary_window)
            self.metrics["redis_ops_total"].labels(operation="materialize_primary", status="success").inc()
            self.metrics["redis_op_duration_seconds"].labels(operation="materialize_primary").observe(
                time.perf_counter() - materialize_start
            )

            t_op_start = time.perf_counter()
            ids_with_scores = await asyncio.to_thread(
                self.redis.zrevrange,
                primary_key,
                0,
                max(0, k - 1),
                withscores=True,
            )
            self.metrics["redis_ops_total"].labels(operation="zrevrange_primary", status="success").inc()
            self.metrics["redis_op_duration_seconds"].labels(operation="zrevrange_primary").observe(
                time.perf_counter() - t_op_start
            )
        except Exception as e:
            self.metrics["redis_ops_total"].labels(operation="materialize_primary", status="error").inc()
            self.metrics["requests_total"].labels(status="error").inc()
            self.metrics["request_duration_seconds"].observe(time.perf_counter() - t_request_start)
            logger.error("Failed to read primary popularity window=%s err=%s", self.primary_window, e)
            return []

        if not ids_with_scores and self.secondary_window:
            try:
                materialize_start = time.perf_counter()
                secondary_key = await asyncio.to_thread(self._ensure_materialized_window, self.secondary_window)
                self.metrics["redis_ops_total"].labels(operation="materialize_secondary", status="success").inc()
                self.metrics["redis_op_duration_seconds"].labels(operation="materialize_secondary").observe(
                    time.perf_counter() - materialize_start
                )
                t_op_start = time.perf_counter()
                ids_with_scores = await asyncio.to_thread(
                    self.redis.zrevrange,
                    secondary_key,
                    0,
                    max(0, k - 1),
                    withscores=True,
                )
                self.metrics["redis_ops_total"].labels(operation="zrevrange_secondary", status="success").inc()
                self.metrics["redis_op_duration_seconds"].labels(operation="zrevrange_secondary").observe(
                    time.perf_counter() - t_op_start
                )
            except Exception as e:
                self.metrics["redis_ops_total"].labels(operation="materialize_secondary", status="error").inc()
                self.metrics["requests_total"].labels(status="error").inc()
                self.metrics["request_duration_seconds"].observe(time.perf_counter() - t_request_start)
                logger.error("Failed to read secondary popularity window=%s err=%s", self.secondary_window, e)
                return []

        ids = [canonical_article_id(item_id) for item_id, _ in ids_with_scores]

        pipe = self.redis.pipeline(transaction=False)
        for aid in ids:
            pipe.hgetall(item_key(aid))
        # Execute pipeline async to avoid blocking
        t_op_start = time.perf_counter()
        try:
            rows = await asyncio.to_thread(pipe.execute, raise_on_error=False)
            metadata_op_elapsed = time.perf_counter() - t_op_start
        except Exception as e:
            self.metrics["redis_ops_total"].labels(operation="metadata", status="error").inc()
            self.metrics["redis_op_duration_seconds"].labels(operation="metadata").observe(
                time.perf_counter() - t_op_start
            )
            self.metrics["requests_total"].labels(status="error").inc()
            self.metrics["request_duration_seconds"].observe(time.perf_counter() - t_request_start)
            logger.error("Failed to read popularity metadata pipeline: %s", e)
            return []

        results = []
        metadata_errors = 0
        score_lookup = {item_id: float(score) for item_id, score in ids_with_scores}
        for aid, meta in zip(ids, rows, strict=True):
            if isinstance(meta, Exception):
                metadata_errors += 1
                meta = {}
            results.append(
                {
                    "article_id": int(aid) if str(aid).isdigit() else aid,
                    "score": score_lookup.get(aid, 0.0),
                    "meta": meta or {},
                }
            )
        metadata_status = "error" if metadata_errors > 0 else "success"
        request_status = "partial" if metadata_errors > 0 else "success"
        self.metrics["redis_ops_total"].labels(operation="metadata", status=metadata_status).inc()
        self.metrics["redis_op_duration_seconds"].labels(operation="metadata").observe(
            metadata_op_elapsed
        )
        self.metrics["requests_total"].labels(status=request_status).inc()
        self.metrics["request_duration_seconds"].observe(time.perf_counter() - t_request_start)
        return results
