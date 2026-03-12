"""
Legacy fine-grained Redis readers. NOT for request hot path.

Use only for: debug scripts, migrations, tests that explicitly exercise
the old per-feature API. Serving code must use PersonalizationSnapshotLoader
and load_personalization_snapshot only.
"""

import logging
import math
import time
from typing import Dict, List, Optional

import redis

from src.config import RedisConfig
from src.utils.redis_metadata import canonical_article_id, item_meta_key
from .feature_reader import FeatureReader
from .metrics import feature_read_latency_seconds, feature_read_total

logger = logging.getLogger(__name__)


def _record_feature_read(feature: str, status: str) -> None:
    feature_read_total.labels(feature=feature, status=status).inc()


def _record_feature_read_latency(feature: str, started_at: float) -> None:
    feature_read_latency_seconds.labels(feature=feature).observe(
        time.perf_counter() - started_at
    )


class LegacyFeatureReader(FeatureReader):
    """
    Fine-grained Redis readers for debug/admin use only.

    NOT for request hot path. Each method triggers separate Redis round-trips.
    Serving code must use load_personalization_snapshot() instead.
    """

    def get_user_recent_clicks(
        self,
        user_id: Optional[str],
        max_items: int = 20,
    ) -> List[str]:
        """Get user's recent clicked items. NOT for hot path."""
        _FEATURE = "recent_clicks"
        if not user_id:
            return []

        t0 = time.perf_counter()
        try:
            items = self.redis.lrange(
                f"user:{user_id}:recent_clicks", 0, max_items - 1
            ) or []
            if items and isinstance(items[0], bytes):
                items = [item.decode("utf-8") for item in items]
            _record_feature_read(_FEATURE, "success")
            return items
        except (redis.TimeoutError, redis.ConnectionError, Exception) as e:
            logger.warning(
                "feature_read_error feature=%s user_id=%s err=%s",
                _FEATURE, user_id, e,
            )
            _record_feature_read(_FEATURE, "error")
            return []
        finally:
            _record_feature_read_latency(_FEATURE, t0)

    def get_user_category_affinity(
        self,
        user_id: Optional[str],
    ) -> Dict[str, float]:
        """Get user's category affinity with lazy decay. NOT for hot path."""
        _FEATURE = "category_affinity"
        if not user_id:
            return {}

        t0 = time.perf_counter()
        try:
            affinity_key = f"user:{user_id}:category_affinity"
            affinity_ts_key = (
                f"{affinity_key}{RedisConfig.CATEGORY_AFFINITY_TIMESTAMP_SUFFIX}"
            )
            pipe = self.redis.pipeline(transaction=False)
            pipe.hgetall(affinity_key)
            pipe.hgetall(affinity_ts_key)
            affinity_raw, affinity_ts_raw = pipe.execute()
            affinity: Dict[str, float] = {}
            now = time.time()
            for cat, score in affinity_raw.items():
                if isinstance(cat, bytes):
                    cat = cat.decode("utf-8")
                raw_score = float(score)
                raw_ts = affinity_ts_raw.get(cat)
                if raw_ts is None and isinstance(cat, str):
                    raw_ts = affinity_ts_raw.get(cat.encode("utf-8"))
                if isinstance(raw_ts, bytes):
                    raw_ts = raw_ts.decode("utf-8")
                try:
                    updated_at = float(raw_ts) if raw_ts is not None else now
                except (TypeError, ValueError):
                    updated_at = now
                elapsed = max(0.0, now - updated_at)
                decayed_score = raw_score * math.exp(
                    -RedisConfig.CATEGORY_AFFINITY_DECAY_LAMBDA * elapsed
                )
                if decayed_score > 1e-9:
                    affinity[cat] = decayed_score
            _record_feature_read(_FEATURE, "success")
            return affinity
        except (redis.TimeoutError, redis.ConnectionError, Exception) as e:
            logger.warning(
                "feature_read_error feature=%s user_id=%s err=%s",
                _FEATURE, user_id, e,
            )
            _record_feature_read(_FEATURE, "error")
            return {}
        finally:
            _record_feature_read_latency(_FEATURE, t0)

    def get_item_categories_batch(self, item_ids: List[str]) -> Dict[str, str]:
        """Batch fetch categories. NOT for hot path."""
        _FEATURE = "item_categories_batch"
        if not item_ids:
            return {}

        t0 = time.perf_counter()
        try:
            pipe = self.redis.pipeline(transaction=False)
            for item_id in item_ids:
                pipe.hget(item_meta_key(item_id), "category")
            raw_categories = pipe.execute()

            result: Dict[str, str] = {}
            for item_id, category in zip(item_ids, raw_categories):
                if isinstance(category, bytes):
                    category = category.decode("utf-8")
                if category and category != "unknown":
                    result[canonical_article_id(item_id)] = category
            _record_feature_read(_FEATURE, "success")
            return result
        except (redis.TimeoutError, redis.ConnectionError, Exception) as e:
            logger.warning(
                "feature_read_error feature=%s batch_size=%d err=%s",
                _FEATURE, len(item_ids), e,
            )
            _record_feature_read(_FEATURE, "error")
            return {}
        finally:
            _record_feature_read_latency(_FEATURE, t0)

    def get_session_clicks(
        self,
        session_id: Optional[str],
        max_items: int = 10,
    ) -> List[str]:
        """Get session clicks. NOT for hot path."""
        _FEATURE = "session_clicks"
        if not session_id:
            return []

        t0 = time.perf_counter()
        try:
            items = self.redis.lrange(
                f"session:{session_id}:clicks", 0, max_items - 1
            ) or []
            if items and isinstance(items[0], bytes):
                items = [item.decode("utf-8") for item in items]
            _record_feature_read(_FEATURE, "success")
            return items
        except (redis.TimeoutError, redis.ConnectionError, Exception) as e:
            logger.warning(
                "feature_read_error feature=%s session_id=%s err=%s",
                _FEATURE, session_id, e,
            )
            _record_feature_read(_FEATURE, "error")
            return []
        finally:
            _record_feature_read_latency(_FEATURE, t0)

    def get_item_popularity_signals(
        self,
        item_ids: List[str],
    ) -> Dict[str, Dict[str, float]]:
        """Read windowed popularity signals. NOT for hot path."""
        _FEATURE = "popularity_signals"
        if not item_ids:
            return {}

        t0 = time.perf_counter()
        try:
            now_ts = time.time()
            materialized_keys, _ = self._resolve_materialized_popularity_windows(
                now_ts
            )
            pipe = self.redis.pipeline(transaction=False)
            for window_name in ("1h", "24h", "7d"):
                pipe.zmscore(materialized_keys[window_name], item_ids)
            scores_by_window = pipe.execute()

            result: Dict[str, Dict[str, float]] = {
                item_id: {"1h": 0.0, "24h": 0.0, "7d": 0.0} for item_id in item_ids
            }
            for window_name, window_scores in zip(
                ("1h", "24h", "7d"), scores_by_window
            ):
                for item_id, raw_score in zip(item_ids, window_scores):
                    result[item_id][window_name] = float(raw_score or 0.0)
            _record_feature_read(_FEATURE, "success")
            return result
        except (redis.TimeoutError, redis.ConnectionError, Exception) as e:
            logger.warning(
                "feature_read_error feature=%s item_count=%d err=%s",
                _FEATURE, len(item_ids), e,
            )
            _record_feature_read(_FEATURE, "error")
            return {}
        finally:
            _record_feature_read_latency(_FEATURE, t0)
