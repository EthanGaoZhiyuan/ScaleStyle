import sys
from pathlib import Path
from types import ModuleType
from unittest.mock import Mock

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

observability_stub = ModuleType("src.utils.observability")
observability_stub.setup_tracing = lambda *args, **kwargs: None
sys.modules.setdefault("src.utils.observability", observability_stub)

from deployments.ingress import _enrich_and_filter
from personalization.feature_reader import FeatureReader


def _pipeline_with_result(result):
    pipe = Mock()
    pipe.execute.return_value = result
    pipe.lrange.return_value = pipe
    pipe.hgetall.return_value = pipe
    pipe.hget.return_value = pipe
    pipe.ttl.return_value = pipe
    pipe.set.return_value = pipe
    pipe.zunionstore.return_value = pipe
    pipe.expire.return_value = pipe
    pipe.delete.return_value = pipe
    pipe.zmscore.return_value = pipe
    return pipe


def test_enrichment_reads_canonical_item_hash_for_raw_numeric_candidate():
    redis_client = Mock()
    pipe = Mock()
    pipe.hgetall.return_value = pipe
    pipe.execute.return_value = [{"prod_name": "Green Jacket", "price": "59.99"}]
    redis_client.pipeline.return_value = pipe

    results = __import__("asyncio").run(
        _enrich_and_filter(
            redis_client,
            [{"article_id": "123456", "score": 0.9}],
            {},
            1,
        )
    )

    pipe.hgetall.assert_called_once_with("item:0000123456")
    assert results[0]["article_id"] == "0000123456"


def test_snapshot_reads_canonical_meta_key_for_raw_numeric_candidates():
    redis_client = Mock()
    user_pipe = _pipeline_with_result([
        ["123456"],
        {"outerwear": "2.0"},
        {"outerwear": "1000.0"},
    ])
    ttl_pipe = _pipeline_with_result([-2, -2, -2])
    lock_pipe = _pipeline_with_result([True, True, True])
    materialize_pipe = _pipeline_with_result([None, True, None, True, None, True, None, None, None])
    item_pipe = _pipeline_with_result([
        "outerwear",
        [5.0],
        [8.0],
        [13.0],
    ])
    redis_client.pipeline.side_effect = [user_pipe, ttl_pipe, lock_pipe, materialize_pipe, item_pipe]

    reader = FeatureReader(redis_client)
    snapshot = reader.load_personalization_snapshot("user-1", ["123456"], max_recent_clicks=1)

    item_pipe.hget.assert_called_once_with("item:0000123456:meta", "category")
    assert snapshot.recent_clicks == ("0000123456",)
    assert snapshot.candidate_item_categories == {"0000123456": "outerwear"}
    assert snapshot.popularity_signals["0000123456"]["24h"] == 8.0