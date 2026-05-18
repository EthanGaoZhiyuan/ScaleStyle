"""
FeatureUpdateHandler — validates events, resolves item categories, and calls
the atomic Lua upsert script to materialise decayed online features.

Extracted from EventConsumer to allow independent testing and to keep
consumer.py focused on orchestration.

All Redis key layout, Lua script arguments, and metric names are preserved
exactly from the original consumer.py implementation.
"""

from __future__ import annotations

import logging
import time
from collections import OrderedDict
from datetime import datetime, timezone
from typing import Any, Dict, Optional, Tuple

import config
import metrics
from models import ProcessingResult
from redis_metadata import canonical_article_id, item_meta_key

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# In-process LRU category cache
# ---------------------------------------------------------------------------


class _CategoryCache:
    """In-process LRU cache with per-entry TTL for item→category lookups.

    Eliminates one Redis round trip per event.  Item categories are stable
    after ingestion, so a 1-hour TTL is safe and keeps the cache warm across
    normal traffic patterns.

    ``"unknown"`` entries are cached with the same TTL to prevent repeated
    lookups for cold or unindexed items.

    Thread-safety: the consumer loop is single-threaded, so no locking needed.

    :param max_size: Maximum number of entries.  LRU entry is evicted when full.
    :param ttl_seconds: Seconds before a cached entry is considered stale.
    """

    def __init__(self, max_size: int, ttl_seconds: float) -> None:
        self._max_size = max_size
        self._ttl = ttl_seconds
        self._store: "OrderedDict[str, Tuple[str, float]]" = OrderedDict()

    def get(self, item_id: str) -> Tuple[Optional[str], str]:
        """Look up *item_id*.

        Returns ``(category, status)`` where status is ``"hit"``, ``"expired"``,
        or ``"miss"``.
        """
        entry = self._store.get(item_id)
        if entry is None:
            return None, "miss"
        category, expires_at = entry
        if time.monotonic() > expires_at:
            del self._store[item_id]
            return None, "expired"
        self._store.move_to_end(item_id)
        return category, "hit"

    def put(self, item_id: str, category: str) -> None:
        """Insert or refresh *item_id* → *category*.  Evicts LRU when full."""
        if item_id in self._store:
            self._store.move_to_end(item_id)
        elif len(self._store) >= self._max_size:
            self._store.popitem(last=False)
        self._store[item_id] = (category, time.monotonic() + self._ttl)

    def __len__(self) -> int:
        return len(self._store)


# ---------------------------------------------------------------------------
# Timestamp parsing helper
# ---------------------------------------------------------------------------


def _parse_event_timestamp_seconds(raw_timestamp: Any) -> Tuple[float, str]:
    """Parse event time into epoch seconds and a canonical ISO-8601 string.

    Real decay uses event timestamps, not wall-clock update time, so malformed
    timestamps are treated as permanent input errors.
    """
    if isinstance(raw_timestamp, (int, float)):
        ts_seconds = float(raw_timestamp)
        return ts_seconds, datetime.fromtimestamp(
            ts_seconds, tz=timezone.utc
        ).isoformat().replace("+00:00", "Z")

    if not isinstance(raw_timestamp, str) or not raw_timestamp.strip():
        raise ValueError("timestamp is required for decayed feature updates")

    normalized = raw_timestamp.strip()
    if normalized.endswith("Z"):
        normalized = normalized[:-1] + "+00:00"
    parsed = datetime.fromisoformat(normalized)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    else:
        parsed = parsed.astimezone(timezone.utc)
    return parsed.timestamp(), parsed.isoformat().replace("+00:00", "Z")


# ---------------------------------------------------------------------------
# Main handler
# ---------------------------------------------------------------------------


class FeatureUpdateHandler:
    """Validates, enriches, and materialises a single click event in Redis.

    Constructor parameters
    ----------------------
    lua_upsert_script : callable
        The registered Lua script callable returned by
        ``redis_client.register_script(LUA_UPSERT_FEATURES)``.  Stored as
        ``self.lua_upsert_script`` so that ``EventConsumer`` can expose a
        property that forwards test assignments directly here.
    redis_client :
        Used for the category metadata lookup (``HGET item:{id}:meta category``).
    category_cache_max_size : int
    category_cache_ttl_seconds : int | float
        Controls the in-process LRU category cache.
    """

    def __init__(
        self,
        *,
        lua_upsert_script,
        redis_client,
        category_cache_max_size: int = config.CATEGORY_CACHE_MAX_SIZE,
        category_cache_ttl_seconds: float = float(config.CATEGORY_CACHE_TTL_SECONDS),
    ) -> None:
        # Public — EventConsumer exposes a property that wraps this attribute
        # so that tests can still do ``consumer.lua_upsert_script = mock``.
        self.lua_upsert_script = lua_upsert_script
        self._redis_client = redis_client
        self._category_cache = _CategoryCache(
            max_size=category_cache_max_size,
            ttl_seconds=category_cache_ttl_seconds,
        )
        logger.info(
            "[SUCCESS] Category LRU cache initialised: max_size=%d, ttl=%ds",
            category_cache_max_size,
            category_cache_ttl_seconds,
        )

    # ------------------------------------------------------------------
    # Public entry point
    # ------------------------------------------------------------------

    def update_features(self, event: Dict[str, Any]) -> ProcessingResult:
        """Update Redis online features using the atomic Lua upsert script.

        Returns:
            ProcessingResult: APPLIED, DUPLICATE, TRANSIENT_FAILURE, or
            PERMANENT_FAILURE.
        """
        t0 = time.time()

        event_id = event.get("event_id")
        user_id = event.get("user_id")
        # Canonicalize before writing: zero-pad numeric IDs so all Redis keys
        # (recent_clicks LIST, popularity ZSETs) use the same format that
        # feature_reader uses for ZMSCORE lookups on the read path.
        item_id = canonical_article_id(event.get("item_id"))
        session_id = event.get("session_id", "")
        raw_timestamp = event.get("timestamp")
        source = event.get("source", "unknown")

        # Validate required fields — permanent failures.
        if not event_id:
            logger.warning("Invalid event (missing event_id): %s", event)
            return ProcessingResult.PERMANENT_FAILURE

        if not user_id or not item_id:
            logger.warning("Invalid event (missing user_id or item_id): %s", event)
            return ProcessingResult.PERMANENT_FAILURE

        try:
            event_ts_seconds, canonical_timestamp = _parse_event_timestamp_seconds(
                raw_timestamp
            )
        except ValueError as exc:
            logger.warning(
                "Invalid event timestamp event_id=%s timestamp=%r error=%s",
                event_id,
                raw_timestamp,
                exc,
            )
            return ProcessingResult.PERMANENT_FAILURE

        category = self._infer_category(item_id)
        if category == "unknown":
            metrics.feature_failures_total.labels(feature="category_affinity").inc()

        try:
            dedupe_key = f"dedupe:event:{event_id}"

            redis_t0 = time.time()
            result = self.lua_upsert_script(
                keys=[dedupe_key],
                args=[
                    config.DEDUPE_WINDOW_SECONDS,
                    user_id,
                    item_id,
                    format(event_ts_seconds, ".6f"),
                    canonical_timestamp,
                    category,
                    session_id,
                    config.RECENT_CLICKS_MAX,
                    format(config.CATEGORY_AFFINITY_DECAY_LAMBDA, ".18g"),
                    format(config.ITEM_CLICK_DECAY_LAMBDA, ".18g"),
                    format(config.RECENT_ITEM_CLICK_DECAY_LAMBDA, ".18g"),
                    format(config.GLOBAL_POPULARITY_DECAY_LAMBDA, ".18g"),
                    config.SESSION_EXPIRE_SECONDS,
                    config.ONLINE_FEATURE_STATE_TTL_SECONDS,
                    config.POPULARITY_1H_BUCKET_SECONDS,
                    config.POPULARITY_1H_BUCKET_TTL_SECONDS,
                    config.POPULARITY_24H_BUCKET_SECONDS,
                    config.POPULARITY_24H_BUCKET_TTL_SECONDS,
                    config.POPULARITY_7D_BUCKET_SECONDS,
                    config.POPULARITY_7D_BUCKET_TTL_SECONDS,
                    config.POPULARITY_BUCKET_PREFIX,
                ],
            )
            redis_latency_s = time.time() - redis_t0
            metrics.redis_update_latency_seconds.observe(redis_latency_s)

            if result == b"DUPLICATE" or result == "DUPLICATE":
                logger.debug(
                    "Duplicate event (already processed): event_id=%s", event_id
                )
                metrics.events_duplicate_total.inc()
                metrics.events_processed_total.labels(result="duplicate").inc()
                metrics.record_redis_success()
                return ProcessingResult.DUPLICATE

            latency_ms = (time.time() - t0) * 1000
            logger.debug(
                "Updated decayed online features: user_id=%s, item_id=%s, "
                "category=%s, source=%s, event_ts=%s, latency_ms=%.1f",
                user_id,
                item_id,
                category,
                source,
                canonical_timestamp,
                latency_ms,
            )
            metrics.event_processing_latency_seconds.observe(latency_ms / 1000.0)
            metrics.events_processed_total.labels(result="applied").inc()
            metrics.redis_operations_total.labels(
                operation="lua_decay_upsert", status="success"
            ).inc()
            metrics.record_redis_success()
            return ProcessingResult.APPLIED

        except Exception as e:
            if "redis_t0" in locals():
                redis_latency_s = time.time() - redis_t0
                metrics.redis_update_latency_seconds.observe(redis_latency_s)

            latency_ms = (time.time() - t0) * 1000
            logger.error(
                "Failed to update features: event_id=%s, user_id=%s, item_id=%s, "
                "error=%s",
                event_id,
                user_id,
                item_id,
                e,
                exc_info=True,
            )
            metrics.record_redis_error()
            metrics.redis_operations_total.labels(
                operation="lua_decay_upsert", status="error"
            ).inc()
            metrics.event_processing_latency_seconds.observe(latency_ms / 1000.0)

            return self._classify_error(e)

    # ------------------------------------------------------------------
    # Category inference
    # ------------------------------------------------------------------

    def _infer_category(self, item_id: str) -> str:
        """Return the category for *item_id*, served from the LRU cache where possible.

        Flow:
          1. Cache hit  → return immediately (no Redis I/O).
          2. Cache miss / expired → HGET from Redis; populate cache on success.
          3. Redis error → return ``"unknown"`` without caching.
        """
        cached_category, cache_status = self._category_cache.get(item_id)
        metrics.category_cache_ops_total.labels(status=cache_status).inc()
        metrics.category_cache_size.set(len(self._category_cache))

        if cache_status == "hit":
            return cached_category  # type: ignore[return-value]

        try:
            normalized_item_id = canonical_article_id(item_id)
            meta_key = item_meta_key(normalized_item_id)
            raw = self._redis_client.hget(meta_key, "category")
            metrics.record_redis_success()

            if raw:
                category = raw
                if category and category != "unknown":
                    metrics.redis_operations_total.labels(
                        operation="category_lookup", status="success"
                    ).inc()
                    self._category_cache.put(item_id, category)
                    return category

        except Exception as e:
            metrics.record_redis_error()
            logger.debug("Category lookup failed for item %s: %s", item_id, e)
            metrics.redis_operations_total.labels(
                operation="category_lookup", status="error"
            ).inc()
            return "unknown"

        self._category_cache.put(item_id, "unknown")
        metrics.redis_operations_total.labels(
            operation="category_lookup", status="miss"
        ).inc()
        return "unknown"

    # ------------------------------------------------------------------
    # Error classification
    # ------------------------------------------------------------------

    @staticmethod
    def _classify_error(exc: Exception) -> ProcessingResult:
        """Classify exception as transient or permanent."""
        import json
        import redis as redis_lib

        if isinstance(exc, (ValueError, KeyError, TypeError, json.JSONDecodeError)):
            return ProcessingResult.PERMANENT_FAILURE
        if isinstance(exc, (redis_lib.ConnectionError, redis_lib.TimeoutError)):
            return ProcessingResult.TRANSIENT_FAILURE
        return ProcessingResult.TRANSIENT_FAILURE

    # ------------------------------------------------------------------
    # Accessors for backward compat
    # ------------------------------------------------------------------

    @property
    def category_cache(self) -> _CategoryCache:
        """Expose the category cache for external introspection / metrics."""
        return self._category_cache
