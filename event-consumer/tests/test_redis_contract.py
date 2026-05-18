"""
Writer–Reader key contract: item-ID canonicalization.

Ensures FeatureUpdateHandler canonicalizes numeric item IDs to zero-padded
strings before handing them to the Lua upsert script.  Without this the
popularity ZSET members and recent_clicks LIST entries would contain
non-padded IDs that will never match the canonical IDs used by
feature_reader.load_personalization_snapshot() on the read path (ZMSCORE
requires exact member match).
"""
import os
import sys
from pathlib import Path
from unittest.mock import MagicMock, Mock, patch

import pytest

os.environ.setdefault("KAFKA_BOOTSTRAP_SERVERS", "test-kafka:9093")
os.environ.setdefault("CONSUMER_MODE", "primary")

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

from feature_update_handler import FeatureUpdateHandler
from models import ProcessingResult
from redis_metadata import canonical_article_id


# ---------------------------------------------------------------------------
# Fixtures (mirror test_feature_update_handler.py pattern)
# ---------------------------------------------------------------------------


@pytest.fixture
def lua_script():
    return Mock(return_value="OK")


@pytest.fixture
def redis_client():
    r = Mock()
    r.hget.return_value = "dress"
    return r


def _setup_metrics_mock(m):
    for attr in [
        "events_processed_total",
        "events_failed_total",
        "events_duplicate_total",
        "feature_failures_total",
        "redis_operations_total",
        "event_processing_latency_seconds",
        "redis_update_latency_seconds",
        "category_cache_ops_total",
        "category_cache_size",
    ]:
        mock_metric = MagicMock()
        mock_metric.labels.return_value = mock_metric
        mock_metric.inc.return_value = None
        mock_metric.observe.return_value = None
        mock_metric.set.return_value = None
        setattr(m, attr, mock_metric)
    m.record_redis_success = Mock()
    m.record_redis_error = Mock()


@pytest.fixture
def handler(lua_script, redis_client):
    with patch("feature_update_handler.metrics") as mock_metrics:
        _setup_metrics_mock(mock_metrics)
        h = FeatureUpdateHandler(
            lua_upsert_script=lua_script,
            redis_client=redis_client,
            category_cache_max_size=100,
            category_cache_ttl_seconds=3600,
        )
        yield h


def _lua_argv3(lua_script) -> str:
    """Return the item_id argument (ARGV[3] = args[2]) from the last Lua call."""
    return lua_script.call_args.kwargs["args"][2]


# ---------------------------------------------------------------------------
# Canonicalization contract
# ---------------------------------------------------------------------------


class TestItemIdCanonicalization:
    """
    H-2 contract: FeatureUpdateHandler must write canonical item IDs into Redis
    so that popularity ZSET members and recent_clicks entries match the IDs
    used by feature_reader.ZMSCORE lookups on the read path.
    """

    def test_numeric_id_is_zero_padded_to_10_digits(self, handler, lua_script):
        handler.update_features({
            "event_id": "evt-1",
            "user_id": "test-user",
            "item_id": "108775015",
            "timestamp": "2026-01-01T00:00:00Z",
        })
        assert _lua_argv3(lua_script) == "0108775015"

    def test_already_padded_id_is_unchanged(self, handler, lua_script):
        handler.update_features({
            "event_id": "evt-2",
            "user_id": "test-user",
            "item_id": "0108775015",
            "timestamp": "2026-01-01T00:00:00Z",
        })
        assert _lua_argv3(lua_script) == "0108775015"

    def test_short_numeric_id_is_padded(self, handler, lua_script):
        handler.update_features({
            "event_id": "evt-3",
            "user_id": "test-user",
            "item_id": "5432",
            "timestamp": "2026-01-01T00:00:00Z",
        })
        assert _lua_argv3(lua_script) == canonical_article_id("5432")

    def test_non_numeric_id_is_unchanged(self, handler, lua_script):
        """Non-numeric IDs (e.g., slugs, UUIDs) pass through unchanged."""
        handler.update_features({
            "event_id": "evt-4",
            "user_id": "test-user",
            "item_id": "shirt-abc-123",
            "timestamp": "2026-01-01T00:00:00Z",
        })
        assert _lua_argv3(lua_script) == "shirt-abc-123"

    def test_empty_item_id_returns_permanent_failure(self, handler):
        result = handler.update_features({
            "event_id": "evt-5",
            "user_id": "test-user",
            "item_id": "",
            "timestamp": "2026-01-01T00:00:00Z",
        })
        assert result == ProcessingResult.PERMANENT_FAILURE

    def test_none_item_id_returns_permanent_failure(self, handler):
        result = handler.update_features({
            "event_id": "evt-6",
            "user_id": "test-user",
            "item_id": None,
            "timestamp": "2026-01-01T00:00:00Z",
        })
        assert result == ProcessingResult.PERMANENT_FAILURE


class TestWriterReaderKeyContract:
    """
    End-to-end key contract: verify that what the writer stores can be found
    by the reader's lookup strategy.

    Writer (Lua, via handler): LPUSH item_id into user:{user_id}:recent_clicks
    Reader (feature_reader):   LRANGE → each item canonicalized via canonical_article_id

    Writer (Lua):              ZINCRBY item_id into popularity:bucket:*
    Reader (feature_reader):   ZMSCORE(materialized_key, canonical_candidate_ids)

    For the read to succeed the ZSET members must already be canonical.
    """

    def test_writer_canonical_id_survives_reader_canonicalization(
        self, handler, lua_script
    ):
        """
        The handler writes canonical_article_id("108775015") = "0108775015".
        The reader reads back the LIST entry and calls canonical_article_id again.
        canonical_article_id("0108775015") must equal "0108775015" (idempotent).
        """
        raw_id = "108775015"
        handler.update_features({
            "event_id": "evt-7",
            "user_id": "test-user",
            "item_id": raw_id,
            "timestamp": "2026-01-01T00:00:00Z",
        })
        written_id = _lua_argv3(lua_script)
        # Reader applies canonical_article_id once more on LRANGE result:
        assert canonical_article_id(written_id) == written_id

    def test_written_id_matches_reader_candidate_id(self, handler, lua_script):
        """
        The ZSET member written by the Lua script (ARGV[3]) must equal the
        canonical_candidate_id that the reader uses for ZMSCORE.
        """
        raw_id = "108775015"
        handler.update_features({
            "event_id": "evt-8",
            "user_id": "test-user",
            "item_id": raw_id,
            "timestamp": "2026-01-01T00:00:00Z",
        })
        written_id = _lua_argv3(lua_script)
        reader_candidate_id = canonical_article_id(raw_id)
        assert written_id == reader_candidate_id, (
            f"ZSET member written={written_id!r} but reader ZMSCORE uses "
            f"{reader_candidate_id!r}; ZMSCORE will always return None"
        )
