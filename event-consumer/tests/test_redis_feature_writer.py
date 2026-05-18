"""Tests for redis_feature_writer.py — atomic Lua upsert writer."""

import os
import sys
from pathlib import Path
from unittest.mock import Mock, patch, MagicMock, call

import pytest

os.environ.setdefault("KAFKA_BOOTSTRAP_SERVERS", "test-kafka:9093")
os.environ.setdefault("CONSUMER_MODE", "primary")

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

import redis as redis_lib
from redis_feature_writer import RedisFeatureWriter, LUA_UPSERT_FEATURES


@pytest.fixture
def redis_mock():
    """Minimal Redis mock with register_script support."""
    client = Mock(spec=redis_lib.Redis)
    lua_script = Mock()
    client.register_script.return_value = lua_script
    return client, lua_script


@pytest.fixture
def writer(redis_mock):
    client, lua_script = redis_mock
    return RedisFeatureWriter(client), lua_script


# ── Construction ──────────────────────────────────────────────────────────────


class TestRedisFeatureWriterConstruction:
    def test_registers_lua_script_on_init(self, redis_mock):
        client, _ = redis_mock
        RedisFeatureWriter(client)
        client.register_script.assert_called_once_with(LUA_UPSERT_FEATURES)

    def test_lua_script_string_unchanged(self):
        """The Lua script literal must not be accidentally modified during refactor."""
        # Spot-check critical tokens that would break atomicity if changed.
        assert "redis.call('EXISTS', dedupe_key)" in LUA_UPSERT_FEATURES
        assert "redis.call('SETEX', dedupe_key, dedupe_ttl, '1')" in LUA_UPSERT_FEATURES
        assert "return 'DUPLICATE'" in LUA_UPSERT_FEATURES
        assert "return 'OK'" in LUA_UPSERT_FEATURES
        assert "ZREMRANGEBYRANK" in LUA_UPSERT_FEATURES
        assert "popularity_bucket_key" in LUA_UPSERT_FEATURES


# ── Successful upsert ─────────────────────────────────────────────────────────


class TestRedisFeatureWriterExecute:
    def test_execute_calls_lua_with_dedupe_key(self, writer):
        w, lua_script = writer
        lua_script.return_value = "OK"

        result = w.execute(
            event_id="evt-001",
            user_id="user-1",
            item_id="item-1",
            event_ts_seconds=1741345845.123,
            canonical_timestamp="2025-03-07T10:30:45.123000Z",
            category="dress",
            session_id="sess-1",
        )

        assert result == "OK"
        lua_script.assert_called_once()
        call_kwargs = lua_script.call_args.kwargs
        assert call_kwargs["keys"] == ["dedupe:event:evt-001"]

    def test_execute_passes_21_args(self, writer):
        w, lua_script = writer
        lua_script.return_value = "OK"

        w.execute(
            event_id="evt-001",
            user_id="user-1",
            item_id="item-1",
            event_ts_seconds=1741345845.0,
            canonical_timestamp="2025-03-07T10:30:45Z",
            category="dress",
            session_id="sess-1",
        )

        args = lua_script.call_args.kwargs["args"]
        assert len(args) == 21

    def test_execute_passes_user_id_as_argv2(self, writer):
        w, lua_script = writer
        lua_script.return_value = "OK"

        w.execute(
            event_id="evt-x",
            user_id="user-abc",
            item_id="item-xyz",
            event_ts_seconds=0.0,
            canonical_timestamp="1970-01-01T00:00:00Z",
            category="shoes",
            session_id="",
        )

        args = lua_script.call_args.kwargs["args"]
        assert args[1] == "user-abc"   # ARGV[2]
        assert args[2] == "item-xyz"   # ARGV[3]
        assert args[5] == "shoes"      # ARGV[6] category

    def test_execute_passes_category_unknown(self, writer):
        w, lua_script = writer
        lua_script.return_value = "OK"

        w.execute(
            event_id="evt-x",
            user_id="user-1",
            item_id="item-1",
            event_ts_seconds=0.0,
            canonical_timestamp="1970-01-01T00:00:00Z",
            category="unknown",
            session_id="",
        )

        args = lua_script.call_args.kwargs["args"]
        assert args[5] == "unknown"

    def test_execute_last_arg_is_popularity_bucket_prefix(self, writer):
        w, lua_script = writer
        lua_script.return_value = "OK"

        w.execute(
            event_id="e",
            user_id="u",
            item_id="i",
            event_ts_seconds=1.0,
            canonical_timestamp="2025-01-01T00:00:00Z",
            category="sportswear",
            session_id="s",
        )

        args = lua_script.call_args.kwargs["args"]
        import config
        assert args[20] == config.POPULARITY_BUCKET_PREFIX

    # ── Duplicate detection ───────────────────────────────────────────────────

    def test_duplicate_hash_returns_DUPLICATE(self, writer):
        w, lua_script = writer
        lua_script.return_value = "DUPLICATE"

        result = w.execute(
            event_id="dup-001",
            user_id="user-1",
            item_id="item-1",
            event_ts_seconds=100.0,
            canonical_timestamp="2025-01-01T00:00:00Z",
            category="dress",
            session_id="",
        )

        assert result == "DUPLICATE"

    def test_duplicate_hash_bytes_also_returned(self, writer):
        """Redis may return bytes when decode_responses=False."""
        w, lua_script = writer
        lua_script.return_value = b"DUPLICATE"

        result = w.execute(
            event_id="dup-002",
            user_id="user-1",
            item_id="item-1",
            event_ts_seconds=100.0,
            canonical_timestamp="2025-01-01T00:00:00Z",
            category="dress",
            session_id="",
        )

        assert result == b"DUPLICATE"

    # ── Error propagation ─────────────────────────────────────────────────────

    def test_transient_redis_connection_error_propagates(self, writer):
        """ConnectionError must not be swallowed — caller classifies it as transient."""
        w, lua_script = writer
        lua_script.side_effect = redis_lib.ConnectionError("Connection refused")

        with pytest.raises(redis_lib.ConnectionError):
            w.execute(
                event_id="e",
                user_id="u",
                item_id="i",
                event_ts_seconds=0.0,
                canonical_timestamp="1970-01-01T00:00:00Z",
                category="unknown",
                session_id="",
            )

    def test_transient_redis_timeout_error_propagates(self, writer):
        w, lua_script = writer
        lua_script.side_effect = redis_lib.TimeoutError("timed out")

        with pytest.raises(redis_lib.TimeoutError):
            w.execute(
                event_id="e",
                user_id="u",
                item_id="i",
                event_ts_seconds=0.0,
                canonical_timestamp="1970-01-01T00:00:00Z",
                category="unknown",
                session_id="",
            )

    def test_generic_exception_propagates(self, writer):
        w, lua_script = writer
        lua_script.side_effect = RuntimeError("unexpected")

        with pytest.raises(RuntimeError):
            w.execute(
                event_id="e",
                user_id="u",
                item_id="i",
                event_ts_seconds=0.0,
                canonical_timestamp="1970-01-01T00:00:00Z",
                category="unknown",
                session_id="",
            )
