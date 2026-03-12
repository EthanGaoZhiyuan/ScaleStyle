"""Helpers for Redis-backed windowed popularity signals."""

from __future__ import annotations

import time

from src.config import RedisConfig


def latest_bucket_start(bucket_seconds: int, now_ts: float | None = None) -> int:
    now_ts = time.time() if now_ts is None else now_ts
    return int(now_ts // bucket_seconds) * bucket_seconds


def active_bucket_keys(window_name: str, bucket_seconds: int, bucket_count: int, now_ts: float | None = None) -> list[str]:
    current_bucket = latest_bucket_start(bucket_seconds, now_ts)
    return [
        f"{RedisConfig.POPULARITY_BUCKET_PREFIX}:{window_name}:{current_bucket - (idx * bucket_seconds)}"
        for idx in range(bucket_count)
    ]


def materialized_window_key(window_name: str, bucket_seconds: int, now_ts: float | None = None) -> str:
    return f"{RedisConfig.POPULARITY_MATERIALIZED_PREFIX}:{window_name}:{latest_bucket_start(bucket_seconds, now_ts)}"
