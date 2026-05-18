"""Helpers for Redis-backed windowed popularity signals."""

from __future__ import annotations

import time

from src.config import RedisConfig


def latest_bucket_start(bucket_seconds: int, now_ts: float | None = None) -> int:
    now_ts = time.time() if now_ts is None else now_ts
    return int(now_ts // bucket_seconds) * bucket_seconds


def active_bucket_keys(
    window_name: str,
    bucket_seconds: int,
    bucket_count: int,
    now_ts: float | None = None,
) -> list[str]:
    current_bucket = latest_bucket_start(bucket_seconds, now_ts)
    return [
        f"{RedisConfig.POPULARITY_BUCKET_PREFIX}:{window_name}:{current_bucket - (idx * bucket_seconds)}"
        for idx in range(bucket_count)
    ]


def materialized_window_key(
    window_name: str, bucket_seconds: int, now_ts: float | None = None
) -> str:
    """Return the Redis key for a materialized popularity window at the current bucket boundary.

    Formula (must stay in sync with gateway-service RecommendationService.java::popularityMaterializedKey):
      key = "{prefix}:{window_name}:{bucketStart}"
      bucketStart = floor(now_unix_seconds / bucket_seconds) * bucket_seconds

    Rounding is floor division on Unix epoch seconds (UTC). No timezone offset applied.
    The resulting bucketStart is always a multiple of bucket_seconds.

    Default bucket sizes (overridable via env / RedisConfig):
      "1h"  -> bucket_seconds = 300   (5-min buckets, 12 per window)
      "24h" -> bucket_seconds = 3600  (1-hour buckets, 24 per window)
      "7d"  -> bucket_seconds = 86400 (1-day buckets, 7 per window)

    Cross-language reference:
      gateway-service/.../RecommendationService.java :: popularityMaterializedKey()
    Drift test:
      tests/test_popularity_windowed.py -- same fixed timestamp, same expected string in Python and Java.
    """
    return f"{RedisConfig.POPULARITY_MATERIALIZED_PREFIX}:{window_name}:{latest_bucket_start(bucket_seconds, now_ts)}"
