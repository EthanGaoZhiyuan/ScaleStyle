"""
Feature Reader - Snapshot-driven personalization for the request hot path.

The ONLY supported hot-path entry point is load_personalization_snapshot().
Load once, consume snapshot only. No per-item or per-feature Redis reads
in the reranker or serving code.

For legacy fine-grained readers (debug/admin only), use LegacyFeatureReader
from feature_reader_legacy.
"""

import logging
import math
import threading
import time
from typing import Dict, List, Optional, Set

import redis

from src.config import RedisConfig
from src.degradation import DegradationReason
from src.personalization.metrics import (
    snapshot_degraded_total,
    snapshot_load_latency_seconds,
    snapshot_materialization_latency_seconds,
    snapshot_materialization_total,
    snapshot_load_total,
    snapshot_redis_round_trips,
)
from src.personalization.popularity_windows import (
    active_bucket_keys,
    materialized_window_key,
)
from src.personalization.snapshot import PersonalizationSnapshot
from src.utils.redis_metadata import canonical_article_id, item_meta_key

logger = logging.getLogger(__name__)


class FeatureReader:
    """Reads online features from Redis for personalization"""

    def __init__(self, redis_client):
        """
        Args:
            redis_client: Synchronous Redis client instance
        """
        self.redis = redis_client
        self._popularity_windows = {
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
        self._materialized_window_cache: Dict[str, tuple[str, float]] = {}
        self._materialized_window_lock = threading.Lock()
        # Events to signal when a window rebuild completes (avoids sleep-in-lock)
        self._rebuild_events: Dict[str, threading.Event] = {}

    def _resolve_materialized_popularity_windows(
        self, now_ts: float
    ) -> tuple[Dict[str, str], int]:
        materialized_keys = {
            window_name: materialized_window_key(
                window_name, spec["bucket_seconds"], now_ts
            )
            for window_name, spec in self._popularity_windows.items()
        }

        # Phase 1 (under lock): Identify which windows need a Redis TTL check.
        # No I/O — only reads the process-local cache.
        ttl_pipe = self.redis.pipeline(transaction=False)
        windows_to_check: list[str] = []
        with self._materialized_window_lock:
            if self._all_windows_cached(materialized_keys, now_ts):
                snapshot_materialization_total.labels(outcome="local_cache_hit").inc()
                return {
                    window_name: cache_key
                    for window_name, (
                        cache_key,
                        _,
                    ) in self._materialized_window_cache.items()
                }, 0

            for window_name, key in materialized_keys.items():
                cached = self._materialized_window_cache.get(window_name)
                if cached and cached[0] == key and now_ts < cached[1]:
                    continue
                windows_to_check.append(window_name)
                ttl_pipe.ttl(key)

        # Phase 2: Execute TTL pipeline WITHOUT holding the lock.
        # Parallel serving threads can run their own Redis I/O concurrently.
        round_trips = 0
        ttl_results: list[int] = []
        if windows_to_check:
            ttl_results = ttl_pipe.execute()
            round_trips += 1

        # Phase 3 (under lock): Process TTL results; determine what needs a rebuild.
        # TOCTOU re-check: a concurrent thread may have populated the cache while we
        # held no lock during Phase 2 — skip any window that is now already valid.
        windows_to_rebuild: list[str] = []
        lock_pipe = None
        lock_keys: dict[str, str] = {}
        t0 = time.perf_counter()
        with self._materialized_window_lock:
            for window_name, ttl in zip(windows_to_check, ttl_results):
                key = materialized_keys[window_name]
                # Re-check cache (TOCTOU guard): another thread may have populated
                # this entry while we held no lock in Phase 2.
                cached = self._materialized_window_cache.get(window_name)
                if cached and cached[0] == key and now_ts < cached[1]:
                    continue
                if ttl and ttl > 0:
                    self._materialized_window_cache[window_name] = (
                        key,
                        now_ts + float(ttl),
                    )
                    snapshot_materialization_total.labels(outcome="redis_ttl_hit").inc()
                else:
                    windows_to_rebuild.append(window_name)

            if windows_to_rebuild:
                # Use distributed lock to prevent thundering herd when multiple replicas
                # detect expired TTL simultaneously and all issue ZUNIONSTORE
                lock_pipe = self.redis.pipeline(transaction=False)
                for window_name in windows_to_rebuild:
                    key = materialized_keys[window_name]
                    lock_key = f"lock:materialized:{window_name}:{key}"
                    lock_keys[window_name] = lock_key
                    lock_pipe.set(lock_key, "1", nx=True, px=5000)  # 5s lock TTL

        # Phase 4: Execute distributed-lock pipeline WITHOUT holding the process-local lock.
        lock_results: list = []
        if lock_pipe is not None:
            lock_results = lock_pipe.execute()
            round_trips += 1

        # Phase 5 (under lock): Categorise winners vs locked-out; register rebuild Events.
        windows_won_lock: list[str] = []
        windows_locked_out: list[str] = []
        wait_events: dict[str, threading.Event] = {}
        if windows_to_rebuild:
            with self._materialized_window_lock:
                for window_name, lock_acquired in zip(windows_to_rebuild, lock_results):
                    key = materialized_keys[window_name]
                    if lock_acquired:
                        windows_won_lock.append(window_name)
                        # Create Event for other threads to wait on
                        # Use key (not window_name) since key changes per time bucket
                        if key not in self._rebuild_events:
                            self._rebuild_events[key] = threading.Event()
                    else:
                        windows_locked_out.append(window_name)
                        snapshot_materialization_total.labels(
                            outcome="lock_skipped"
                        ).inc()
                        logger.debug(
                            "zunionstore_skipped window=%s key=%s reason=lock_held_by_peer",
                            window_name,
                            key,
                        )
                        # Check if there's already an Event to wait on
                        if key in self._rebuild_events:
                            wait_events[window_name] = self._rebuild_events[key]

        # Phase 6: Perform ZUNIONSTORE rebuild (outside the lock)
        if windows_to_rebuild:
            if windows_won_lock:
                rebuild_pipe = self.redis.pipeline(transaction=False)
                for window_name in windows_won_lock:
                    spec = self._popularity_windows[window_name]
                    key = materialized_keys[window_name]
                    bucket_keys = active_bucket_keys(
                        window_name,
                        spec["bucket_seconds"],
                        spec["bucket_count"],
                        now_ts,
                    )
                    rebuild_pipe.zunionstore(key, bucket_keys, aggregate="SUM")
                    rebuild_pipe.expire(key, spec["materialized_ttl_seconds"])
                    rebuild_pipe.delete(lock_keys[window_name])  # Release lock

                try:
                    rebuild_pipe.execute()
                    round_trips += 1

                    # Update cache and signal waiting threads
                    with self._materialized_window_lock:
                        for window_name in windows_won_lock:
                            spec = self._popularity_windows[window_name]
                            key = materialized_keys[window_name]
                            self._materialized_window_cache[window_name] = (
                                key,
                                now_ts + float(spec["materialized_ttl_seconds"]),
                            )
                            snapshot_materialization_total.labels(
                                outcome="rebuilt"
                            ).inc()
                            # Signal waiting threads
                            if key in self._rebuild_events:
                                self._rebuild_events[key].set()
                                # Clean up old events to prevent memory leak
                                del self._rebuild_events[key]

                    snapshot_materialization_latency_seconds.observe(
                        time.perf_counter() - t0
                    )
                except Exception as e:
                    logger.warning("zunionstore_batch_failed error=%s", e)
                    # Release all locks on failure
                    try:
                        unlock_pipe = self.redis.pipeline(transaction=False)
                        for window_name in windows_won_lock:
                            unlock_pipe.delete(lock_keys[window_name])
                        unlock_pipe.execute()
                        round_trips += 1
                    except Exception:
                        pass  # Locks will expire naturally

                    # Signal failure to waiting threads
                    with self._materialized_window_lock:
                        for window_name in windows_won_lock:
                            key = materialized_keys[window_name]
                            if key in self._rebuild_events:
                                self._rebuild_events[key].set()
                                del self._rebuild_events[key]
                        snapshot_materialization_total.labels(
                            outcome="rebuild_failed"
                        ).inc()

            # Phase 4: For windows locked out, wait on Event or poll Redis
            if windows_locked_out:
                # Wait on Events (50ms timeout) instead of sleeping in lock
                for window_name, event in wait_events.items():
                    event.wait(
                        timeout=0.05
                    )  # 50ms max wait, non-blocking for other threads

                # Check if peer rebuild completed
                recheck_pipe = self.redis.pipeline(transaction=False)
                for window_name in windows_locked_out:
                    recheck_pipe.ttl(materialized_keys[window_name])
                recheck_ttls = recheck_pipe.execute()
                round_trips += 1

                # Update cache based on results
                with self._materialized_window_lock:
                    for window_name, ttl in zip(windows_locked_out, recheck_ttls):
                        key = materialized_keys[window_name]
                        if ttl and ttl > 0:
                            # Peer rebuild succeeded - cache the result
                            self._materialized_window_cache[window_name] = (
                                key,
                                now_ts + float(ttl),
                            )
                            snapshot_materialization_total.labels(
                                outcome="peer_rebuilt"
                            ).inc()
                        else:
                            # Peer rebuild not visible yet - use shorter local cache TTL for retry
                            self._materialized_window_cache[window_name] = (
                                key,
                                now_ts + 5.0,
                            )
                            snapshot_materialization_total.labels(
                                outcome="rebuild_pending"
                            ).inc()
                            logger.warning(
                                "materialized_window_unavailable window=%s key=%s fallback=short_ttl_retry",
                                window_name,
                                key,
                            )

        # Return resolved keys (re-acquire lock briefly for final cache read)
        with self._materialized_window_lock:
            resolved = {
                window_name: self._materialized_window_cache[window_name][0]
                for window_name in self._popularity_windows
            }
        return resolved, round_trips

    def _all_windows_cached(self, expected_keys: Dict[str, str], now_ts: float) -> bool:
        for window_name, expected_key in expected_keys.items():
            cached = self._materialized_window_cache.get(window_name)
            if cached is None:
                return False
            cache_key, expires_at = cached
            if cache_key != expected_key or now_ts >= expires_at:
                return False
        return True

    def load_personalization_snapshot(
        self,
        user_id: Optional[str],
        candidate_item_ids: List[str],
        *,
        max_recent_clicks: int = 20,
    ) -> PersonalizationSnapshot:
        """Load all personalization inputs for one request using a bounded read plan.

        Read plan:
        1. One pipeline for user-level state (`recent_clicks`, `category_affinity`, timestamps)
        2. One Redis-side materialization step for rolling popularity windows
        3. One pipeline for candidate/recent-click item categories and popularity zmscores
        """
        started_at = time.perf_counter()
        round_trips = 0
        degraded_reasons: list[DegradationReason] = []
        category_affinity: Dict[str, float] = {}
        recent_clicks: tuple[str, ...] = ()
        clicked_categories: Set[str] = set()
        candidate_item_categories: Dict[str, str] = {}
        canonical_candidate_ids = [
            canonical_article_id(item_id) for item_id in candidate_item_ids
        ]
        popularity_signals: Dict[str, Dict[str, float]] = {
            item_id: {"1h": 0.0, "24h": 0.0, "7d": 0.0}
            for item_id in canonical_candidate_ids
        }

        # Stage 1: user-level features in one pipeline
        if user_id:
            try:
                affinity_key = f"user:{user_id}:category_affinity"
                affinity_ts_key = (
                    f"{affinity_key}{RedisConfig.CATEGORY_AFFINITY_TIMESTAMP_SUFFIX}"
                )
                user_pipe = self.redis.pipeline(transaction=False)
                user_pipe.lrange(
                    f"user:{user_id}:recent_clicks", 0, max_recent_clicks - 1
                )
                user_pipe.hgetall(affinity_key)
                user_pipe.hgetall(affinity_ts_key)
                raw_recent_clicks, affinity_raw, affinity_ts_raw = user_pipe.execute()
                round_trips += 1

                recent_clicks = tuple(
                    canonical_article_id(item_id)
                    for item_id in (raw_recent_clicks or [])
                )

                now = time.time()
                for cat, score in affinity_raw.items():
                    raw_score = float(score)
                    raw_ts = affinity_ts_raw.get(cat)
                    try:
                        updated_at = float(raw_ts) if raw_ts is not None else now
                    except (TypeError, ValueError):
                        updated_at = now
                    elapsed = max(0.0, now - updated_at)
                    decayed_score = raw_score * math.exp(
                        -RedisConfig.CATEGORY_AFFINITY_DECAY_LAMBDA * elapsed
                    )
                    if decayed_score > 1e-9:
                        category_affinity[cat] = decayed_score
            except redis.TimeoutError:
                degraded_reasons.append(DegradationReason.REDIS_TIMEOUT)
            except redis.ConnectionError:
                degraded_reasons.append(DegradationReason.REDIS_UNAVAILABLE)
            except Exception:
                degraded_reasons.append(DegradationReason.PERSONALIZATION_UNAVAILABLE)

        # Stage 2: resolve rolling popularity window keys. This path is read-mostly:
        # reuse process-local cache when valid, check Redis TTL on miss/expiry, and
        # rebuild only the windows that are actually absent.
        materialized_keys: Dict[str, str] = {}
        try:
            materialized_keys, materialize_round_trips = (
                self._resolve_materialized_popularity_windows(time.time())
            )
            round_trips += materialize_round_trips
        except redis.TimeoutError:
            degraded_reasons.append(DegradationReason.REDIS_TIMEOUT)
        except redis.ConnectionError:
            degraded_reasons.append(DegradationReason.REDIS_UNAVAILABLE)
        except Exception:
            degraded_reasons.append(DegradationReason.PERSONALIZATION_UNAVAILABLE)

        # Stage 3: all item-level data in one pipeline
        item_ids_to_fetch = []
        seen: Set[str] = set()
        for item_id in canonical_candidate_ids + list(recent_clicks):
            if item_id and item_id not in seen:
                seen.add(item_id)
                item_ids_to_fetch.append(item_id)

        if item_ids_to_fetch:
            try:
                item_pipe = self.redis.pipeline(transaction=False)
                for item_id in item_ids_to_fetch:
                    item_pipe.hget(item_meta_key(item_id), "category")
                for window_name in ("1h", "24h", "7d"):
                    if materialized_keys:
                        item_pipe.zmscore(
                            materialized_keys[window_name], canonical_candidate_ids
                        )
                response = item_pipe.execute()
                round_trips += 1

                raw_categories = response[: len(item_ids_to_fetch)]
                offset = len(item_ids_to_fetch)
                for item_id, category in zip(item_ids_to_fetch, raw_categories):
                    if category and category != "unknown":
                        candidate_item_categories[item_id] = category

                clicked_categories = {
                    candidate_item_categories[item_id]
                    for item_id in recent_clicks
                    if item_id in candidate_item_categories
                }

                if materialized_keys:
                    for window_name, scores in zip(
                        ("1h", "24h", "7d"), response[offset:]
                    ):
                        for item_id, raw_score in zip(canonical_candidate_ids, scores):
                            popularity_signals[item_id][window_name] = float(
                                raw_score or 0.0
                            )
            except redis.TimeoutError:
                degraded_reasons.append(DegradationReason.REDIS_TIMEOUT)
            except redis.ConnectionError:
                degraded_reasons.append(DegradationReason.REDIS_UNAVAILABLE)
            except Exception:
                degraded_reasons.append(DegradationReason.PERSONALIZATION_UNAVAILABLE)

        degraded = bool(degraded_reasons)
        if degraded:
            for reason in degraded_reasons:
                snapshot_degraded_total.labels(reason=reason.value).inc()
            logger.warning(
                "personalization_snapshot_degraded user_id=%s redis_round_trips=%d degrade_reasons=%s",
                user_id,
                round_trips,
                ",".join(reason.value for reason in degraded_reasons),
            )

        status = "success"
        if degraded and (
            recent_clicks
            or category_affinity
            or candidate_item_categories
            or any(popularity_signals.values())
        ):
            status = "partial"
        elif degraded:
            status = "degraded"
        snapshot_load_total.labels(status=status).inc()
        snapshot_load_latency_seconds.observe(time.perf_counter() - started_at)
        snapshot_redis_round_trips.observe(round_trips)

        return PersonalizationSnapshot(
            user_id=user_id,
            recent_clicks=recent_clicks,
            category_affinity=category_affinity,
            clicked_categories=clicked_categories,
            candidate_item_categories={
                item_id: category
                for item_id, category in candidate_item_categories.items()
                if item_id in canonical_candidate_ids
            },
            popularity_signals=popularity_signals,
            redis_round_trips=round_trips,
            degraded=degraded,
            degraded_reasons=tuple(degraded_reasons),
        )
