"""
Redis feature writer for the event-consumer pipeline.

Executes the atomic Lua upsert script that writes time-decayed online features
to Redis.  The script performs a single atomic Redis transaction that:
  1. Checks the deduplicate key (SETEX-based, TTL from config.DEDUPE_WINDOW_SECONDS)
  2. Updates user recent-clicks list (LPUSH + LTRIM)
  3. Updates user last-activity timestamp
  4. Increments category affinity with exponential decay (HSET)
  5. Increments item click signals with exponential decay (SET)
  6. Increments global popularity decay hash + ZSET surrogate
  7. Increments three windowed popularity buckets (1 h, 24 h, 7 d)
  8. Appends item to session click list

This writer assumes single-node Redis / non-cluster mode because the Lua script
touches multiple key slots across user:*, item:*, global:*, popularity:*, and
session:* namespaces.  Redis Cluster support requires a different key design or
non-atomic multi-key strategy.
"""

from __future__ import annotations

import logging

import redis

import config

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Lua script (verbatim copy from original consumer.py — do NOT modify)
# ---------------------------------------------------------------------------

LUA_UPSERT_FEATURES = """
    -- Args:
    --   dedupe_ttl, user_id, item_id, event_ts, last_activity_ts, category, session_id,
    --   recent_clicks_max, affinity_lambda, item_click_lambda, recent_item_lambda,
    --   popularity_lambda, session_ttl, feature_state_ttl,
    --   popularity_1h_bucket_seconds, popularity_1h_bucket_ttl,
    --   popularity_24h_bucket_seconds, popularity_24h_bucket_ttl,
    --   popularity_7d_bucket_seconds, popularity_7d_bucket_ttl,
    --   popularity_bucket_prefix
    local dedupe_key = KEYS[1]
    local dedupe_ttl = tonumber(ARGV[1])
    local user_id = ARGV[2]
    local item_id = ARGV[3]
    local event_ts = tonumber(ARGV[4])
    local last_activity_ts = ARGV[5]
    local category = ARGV[6]
    local session_id = ARGV[7]
    local recent_clicks_max = tonumber(ARGV[8])
    local affinity_lambda = tonumber(ARGV[9])
    local item_click_lambda = tonumber(ARGV[10])
    local recent_item_lambda = tonumber(ARGV[11])
    local popularity_lambda = tonumber(ARGV[12])
    local session_ttl = tonumber(ARGV[13])
    local feature_state_ttl = tonumber(ARGV[14])
    local popularity_1h_bucket_seconds = tonumber(ARGV[15])
    local popularity_1h_bucket_ttl = tonumber(ARGV[16])
    local popularity_24h_bucket_seconds = tonumber(ARGV[17])
    local popularity_24h_bucket_ttl = tonumber(ARGV[18])
    local popularity_7d_bucket_seconds = tonumber(ARGV[19])
    local popularity_7d_bucket_ttl = tonumber(ARGV[20])
    local popularity_bucket_prefix = ARGV[21]

    local function format_number(value)
        return string.format('%.17g', value)
    end

    local function decayed_score(score_key, timestamp_key, now_ts, decay_lambda)
        local raw_score = redis.call('GET', score_key)
        if not raw_score then
            return 0.0
        end
        local score = tonumber(raw_score) or 0.0
        if score <= 0.0 then
            return 0.0
        end
        local raw_last_ts = redis.call('GET', timestamp_key)
        local last_ts = tonumber(raw_last_ts)
        if not last_ts or last_ts > now_ts then
            return score
        end
        local elapsed = now_ts - last_ts
        if elapsed <= 0.0 then
            return score
        end
        return score * math.exp(-decay_lambda * elapsed)
    end

    local function decayed_hash_score(score_hash_key, timestamp_hash_key, field, now_ts, decay_lambda)
        local raw_score = redis.call('HGET', score_hash_key, field)
        if not raw_score then
            return 0.0
        end
        local score = tonumber(raw_score) or 0.0
        if score <= 0.0 then
            return 0.0
        end
        local raw_last_ts = redis.call('HGET', timestamp_hash_key, field)
        local last_ts = tonumber(raw_last_ts)
        if not last_ts or last_ts > now_ts then
            return score
        end
        local elapsed = now_ts - last_ts
        if elapsed <= 0.0 then
            return score
        end
        return score * math.exp(-decay_lambda * elapsed)
    end

    local function popularity_bucket_key(window_name, now_ts, bucket_seconds)
        local bucket_start = math.floor(now_ts / bucket_seconds) * bucket_seconds
        return popularity_bucket_prefix .. ':' .. window_name .. ':' .. tostring(bucket_start)
    end

    -- Check if event already processed (idempotency)
    if redis.call('EXISTS', dedupe_key) == 1 then
        return 'DUPLICATE'
    end

    -- Set dedupe marker
    redis.call('SETEX', dedupe_key, dedupe_ttl, '1')

    -- Update all features atomically
    -- 1. Recent clicks
    local recent_clicks_key = 'user:' .. user_id .. ':recent_clicks'
    redis.call('LPUSH', recent_clicks_key, item_id)
    redis.call('LTRIM', recent_clicks_key, 0, recent_clicks_max - 1)

    -- 2. Last activity
    redis.call('SET', 'user:' .. user_id .. ':last_activity', last_activity_ts)

    -- 3. Category affinity with true exponential time decay per category
    if category ~= '' and category ~= 'unknown' then
        local affinity_key = 'user:' .. user_id .. ':category_affinity'
        local affinity_ts_key = affinity_key .. ':last_ts'
        local affinity_score = decayed_hash_score(affinity_key, affinity_ts_key, category, event_ts, affinity_lambda) + 1.0
        redis.call('HSET', affinity_key, category, format_number(affinity_score))
        redis.call('HSET', affinity_ts_key, category, format_number(event_ts))
        redis.call('EXPIRE', affinity_key, feature_state_ttl)
        redis.call('EXPIRE', affinity_ts_key, feature_state_ttl)
    end

    -- 4. Item click signals with real decay
    local item_click_key = 'item:' .. item_id .. ':clicks'
    local item_click_ts_key = item_click_key .. ':last_ts'
    local item_click_score = decayed_score(item_click_key, item_click_ts_key, event_ts, item_click_lambda) + 1.0
    redis.call('SET', item_click_key, format_number(item_click_score))
    redis.call('SET', item_click_ts_key, format_number(event_ts))
    redis.call('EXPIRE', item_click_key, feature_state_ttl)
    redis.call('EXPIRE', item_click_ts_key, feature_state_ttl)

    local recent_item_key = 'item:' .. item_id .. ':recent_clicks'
    local recent_item_ts_key = recent_item_key .. ':last_ts'
    local recent_item_score = decayed_score(recent_item_key, recent_item_ts_key, event_ts, recent_item_lambda) + 1.0
    redis.call('SET', recent_item_key, format_number(recent_item_score))
    redis.call('SET', recent_item_ts_key, format_number(event_ts))
    redis.call('EXPIRE', recent_item_key, feature_state_ttl)
    redis.call('EXPIRE', recent_item_ts_key, feature_state_ttl)

    -- 5. Global popularity with true exponential decay.
    -- We store the actual score in a hash and a ranking surrogate in the ZSET:
    --   zset_score = log(actual_score_at_last_update) + lambda * last_update_ts
    -- This preserves correct ordering at read time without a full ZSET rescore sweep.
    local popularity_score_key = 'global:popular:score'
    local popularity_ts_key = 'global:popular:last_ts'
    local popularity_score = decayed_hash_score(popularity_score_key, popularity_ts_key, item_id, event_ts, popularity_lambda) + 1.0
    redis.call('HSET', popularity_score_key, item_id, format_number(popularity_score))
    redis.call('HSET', popularity_ts_key, item_id, format_number(event_ts))
    redis.call('ZADD', 'global:popular', math.log(popularity_score) + (popularity_lambda * event_ts), item_id)

    -- Prevent unbounded growth: cap ZSET at top 50K items and set TTL on all structures
    redis.call('ZREMRANGEBYRANK', 'global:popular', 0, -(50001))
    local popularity_ttl = 30 * 86400  -- 30 days
    redis.call('EXPIRE', 'global:popular', popularity_ttl)
    redis.call('EXPIRE', popularity_score_key, popularity_ttl)
    redis.call('EXPIRE', popularity_ts_key, popularity_ttl)

    -- 5b. Windowed popularity buckets used as the primary online ranking signal.
    local popularity_1h_key = popularity_bucket_key('1h', event_ts, popularity_1h_bucket_seconds)
    redis.call('ZINCRBY', popularity_1h_key, 1.0, item_id)
    redis.call('EXPIRE', popularity_1h_key, popularity_1h_bucket_ttl)

    local popularity_24h_key = popularity_bucket_key('24h', event_ts, popularity_24h_bucket_seconds)
    redis.call('ZINCRBY', popularity_24h_key, 1.0, item_id)
    redis.call('EXPIRE', popularity_24h_key, popularity_24h_bucket_ttl)

    local popularity_7d_key = popularity_bucket_key('7d', event_ts, popularity_7d_bucket_seconds)
    redis.call('ZINCRBY', popularity_7d_key, 1.0, item_id)
    redis.call('EXPIRE', popularity_7d_key, popularity_7d_bucket_ttl)

    -- After local session_id = ARGV[7], add:
    if session_id ~= '' then
        local session_key = 'session:' .. session_id .. ':clicks'
        redis.call('LPUSH', session_key, item_id)
        redis.call('LTRIM', session_key, 0, 99)
        redis.call('EXPIRE', session_key, session_ttl)
    end

    return 'OK'
    """


class RedisFeatureWriter:
    """Executes the atomic Lua upsert for a single click event.

    This writer assumes single-node Redis / non-cluster mode because the Lua
    script touches multiple key slots across user:*, item:*, global:*,
    popularity:*, and session:* namespaces.  Redis Cluster support requires a
    different key design or non-atomic multi-key strategy.

    Parameters
    ----------
    redis_client:
        A connected ``redis.Redis`` instance.  The caller is responsible for
        creating the connection pool and verifying connectivity.
    """

    def __init__(self, redis_client: redis.Redis) -> None:
        self._redis = redis_client
        self._lua_script = redis_client.register_script(LUA_UPSERT_FEATURES)
        logger.info(
            "[SUCCESS] Loaded atomic duplicate-suppression + decayed-feature Lua script"
        )

    def execute(
        self,
        *,
        event_id: str,
        user_id: str,
        item_id: str,
        event_ts_seconds: float,
        canonical_timestamp: str,
        category: str,
        session_id: str,
    ):
        """Execute the Lua upsert and return the raw Redis response string.

        Returns ``"OK"`` on successful upsert or ``"DUPLICATE"`` when the
        dedupe key already exists.  Propagates any Redis exception to the
        caller for classification.

        Parameters
        ----------
        event_id:
            Unique event identifier used as the deduplication key suffix.
        user_id:
            User identifier written into user:* feature keys.
        item_id:
            Item identifier written into item:* feature keys.
        event_ts_seconds:
            Event timestamp as Unix epoch seconds (float), used for decay math.
        canonical_timestamp:
            ISO-8601 string written as the ``last_activity`` value.
        category:
            Item category string (``"unknown"`` when unavailable).
        session_id:
            Session identifier for session:* click history (empty string to skip).

        Returns
        -------
        str
            ``"OK"`` or ``"DUPLICATE"``.

        Raises
        ------
        redis.ConnectionError, redis.TimeoutError, redis.ResponseError, Exception
            Re-raised from the Redis client for the caller to classify as
            transient or permanent.
        """
        dedupe_key = f"dedupe:event:{event_id}"
        return self._lua_script(
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
