"""Unit tests for hybrid search fusion logic (no live Milvus or Redis required)."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

from deployments.multimodal import (
    fuse_with_normalized_scores,
    _minmax_normalize,
    apply_behavior_boost_to_hybrid_results,
)
from personalization.behavior_boost import BehaviorBoost
from personalization.snapshot import PersonalizationSnapshot

# ---------------------------------------------------------------------------
# _minmax_normalize
# ---------------------------------------------------------------------------


def test_minmax_normalize_varying():
    scores = {"a": 0.2, "b": 0.5, "c": 1.0}
    result = _minmax_normalize(scores)
    assert result["a"] == 0.0, "min maps to 0"
    assert result["c"] == 1.0, "max maps to 1"
    assert 0.0 < result["b"] < 1.0


def test_minmax_normalize_all_equal():
    scores = {"a": 0.7, "b": 0.7, "c": 0.7}
    result = _minmax_normalize(scores)
    # All-equal → every value normalizes to 1.0
    assert all(v == 1.0 for v in result.values())


def test_minmax_normalize_empty():
    assert _minmax_normalize({}) == {}


# ---------------------------------------------------------------------------
# fuse_with_normalized_scores — article_id canonicalization
# ---------------------------------------------------------------------------


def test_fusion_canonicalizes_int_and_padded_string_to_same_key():
    """Int 108775015 and string '0108775015' must merge into one candidate."""
    image_candidates = [{"article_id": 108775015, "score": 0.99}]
    text_candidates = [{"article_id": "0108775015", "score": 0.85}]

    results = fuse_with_normalized_scores(
        text_candidates,
        image_candidates,
        limit=10,
        image_weight=0.5,
        text_weight=0.4,
        behavior_weight=0.1,
    )

    assert len(results) == 1, "same article must deduplicate"
    r = results[0]
    assert r["article_id"] == "0108775015"
    assert "image" in r["candidate_sources"]
    assert "text" in r["candidate_sources"]
    assert r["image_score"] is not None
    assert r["text_score"] is not None


def test_fusion_canonical_itemid_zfill():
    """article_id in fused results should be 10-digit zero-padded."""
    candidates = [{"article_id": 108775015, "score": 0.9}]
    results = fuse_with_normalized_scores([], candidates, limit=5)
    assert results[0]["article_id"] == "0108775015"
    assert len(results[0]["article_id"]) == 10


# ---------------------------------------------------------------------------
# fuse_with_normalized_scores — score normalization
# ---------------------------------------------------------------------------


def test_fusion_single_image_candidate_normalizes_to_one():
    """Single candidate on one side → normalized score = 1.0, all-equal path."""
    image_candidates = [{"article_id": "1234567890", "score": 0.75}]
    results = fuse_with_normalized_scores(
        [],
        image_candidates,
        limit=5,
        image_weight=0.5,
        text_weight=0.4,
        behavior_weight=0.1,
    )
    assert len(results) == 1
    # normalized_image_score = 1.0, norm_text = 0.0, behavior = 0.0
    # final_score ≈ 0.5 * 1.0 + 0.4 * 0.0 + 0.1 * 0.0 = 0.5 (after weight renorm: 0.5/1.0*1.0=0.5)
    assert results[0]["final_score"] > 0.0


def test_fusion_formula_image_and_text_candidate():
    """Candidate present in both lists should have higher score than image-only or text-only."""
    image_candidates = [
        {"article_id": "0000000001", "score": 1.0},
        {"article_id": "0000000002", "score": 0.5},
    ]
    text_candidates = [
        {"article_id": "0000000001", "score": 1.0},  # same as first image candidate
        {"article_id": "0000000003", "score": 0.5},  # text-only
    ]

    results = fuse_with_normalized_scores(
        text_candidates,
        image_candidates,
        limit=10,
        image_weight=0.5,
        text_weight=0.4,
        behavior_weight=0.1,
    )

    by_id = {r["article_id"]: r for r in results}

    # Both sources → should score higher than single-source candidates
    both = by_id["0000000001"]
    img_only = by_id["0000000002"]
    txt_only = by_id["0000000003"]

    assert both["final_score"] > img_only["final_score"]
    assert both["final_score"] > txt_only["final_score"]
    assert both["image_score"] == 1.0
    assert both["text_score"] == 1.0


# ---------------------------------------------------------------------------
# fuse_with_normalized_scores — fallback / degenerate inputs
# ---------------------------------------------------------------------------


def test_fusion_image_only_when_text_empty():
    """Empty text candidates → image-only fusion, degraded path coverage."""
    image_candidates = [
        {"article_id": "0000000010", "score": 0.9},
        {"article_id": "0000000011", "score": 0.5},
    ]
    results = fuse_with_normalized_scores([], image_candidates, limit=5)
    assert len(results) == 2
    for r in results:
        assert r["candidate_sources"] == ["image"]
        assert r["text_score"] is None
        assert r["behavior_score"] == 0.0


def test_fusion_text_only_when_image_empty():
    text_candidates = [
        {"article_id": "0000000020", "score": 0.8},
    ]
    results = fuse_with_normalized_scores(text_candidates, [], limit=5)
    assert len(results) == 1
    assert results[0]["candidate_sources"] == ["text"]
    assert results[0]["image_score"] is None


def test_fusion_both_empty_returns_empty():
    results = fuse_with_normalized_scores([], [], limit=5)
    assert results == []


def test_fusion_limit_respected():
    image_candidates = [
        {"article_id": str(i).zfill(10), "score": float(i)} for i in range(20)
    ]
    results = fuse_with_normalized_scores([], image_candidates, limit=5)
    assert len(results) == 5


def test_fusion_zero_weights_fallback_to_defaults():
    """All-zero weights should not crash — falls back to (0.5, 0.4, 0.1)."""
    candidates = [{"article_id": "0000000030", "score": 0.5}]
    results = fuse_with_normalized_scores(
        candidates, [], limit=5, image_weight=0.0, text_weight=0.0, behavior_weight=0.0
    )
    assert len(results) == 1
    assert results[0]["final_score"] >= 0.0


# ---------------------------------------------------------------------------
# fuse_with_normalized_scores — behavior_score starts at 0.0 before post-fusion boost
# ---------------------------------------------------------------------------


def test_fusion_behavior_score_is_zero():
    """fuse_with_normalized_scores sets behavior_score=0.0; post-fusion boost populates it."""
    candidates = [{"article_id": "0000000040", "score": 0.8}]
    results = fuse_with_normalized_scores(candidates, [], limit=5)
    assert results[0]["behavior_score"] == 0.0


# ---------------------------------------------------------------------------
# apply_behavior_boost_to_hybrid_results
# ---------------------------------------------------------------------------


def _make_snapshot(**kwargs):
    base = PersonalizationSnapshot(
        user_id="user-1",
        recent_clicks=(),
        category_affinity={},
        clicked_categories=set(),
        candidate_item_categories={},
        popularity_signals={},
    )
    for k, v in kwargs.items():
        setattr(base, k, v)
    return base


def test_hybrid_boost_populates_behavior_score_for_clicked_item():
    boost = BehaviorBoost(exact_click_boost=1.5)
    snapshot = _make_snapshot(recent_clicks=("0000000001",))
    results = [
        {
            "article_id": "0000000001",
            "final_score": 0.8,
            "score": 0.8,
            "image_score": 0.9,
            "text_score": 0.7,
            "behavior_score": 0.0,
        }
    ]
    apply_behavior_boost_to_hybrid_results(results, snapshot, boost)
    assert results[0]["behavior_score"] > 0.0
    assert results[0]["final_score"] > 0.8
    # image_score and text_score must not be touched
    assert results[0]["image_score"] == 0.9
    assert results[0]["text_score"] == 0.7


def test_hybrid_boost_no_behavior_features_keeps_zero():
    boost = BehaviorBoost()
    snapshot = _make_snapshot()  # no clicks, no affinity, no popularity
    results = [
        {
            "article_id": "0000000002",
            "final_score": 0.5,
            "score": 0.5,
            "image_score": 0.6,
            "text_score": 0.4,
            "behavior_score": 0.0,
        }
    ]
    apply_behavior_boost_to_hybrid_results(results, snapshot, boost)
    assert results[0]["behavior_score"] == 0.0
    assert results[0]["final_score"] == 0.5


def test_hybrid_boost_reorders_by_final_score_when_clicked():
    boost = BehaviorBoost(exact_click_boost=2.0)
    snapshot = _make_snapshot(recent_clicks=("0000000010",))
    results = [
        {
            "article_id": "0000000020",
            "final_score": 0.9,
            "score": 0.9,
            "image_score": None,
            "text_score": 0.9,
            "behavior_score": 0.0,
        },
        {
            "article_id": "0000000010",
            "final_score": 0.6,
            "score": 0.6,
            "image_score": 0.6,
            "text_score": None,
            "behavior_score": 0.0,
        },
    ]
    apply_behavior_boost_to_hybrid_results(results, snapshot, boost)
    # item_10 clicked → 0.6 * 2.0 = 1.2 > 0.9 → ranks first
    assert results[0]["article_id"] == "0000000010"
    assert results[0]["behavior_score"] > 0.0
    assert results[1]["article_id"] == "0000000020"
    assert results[1]["behavior_score"] == 0.0


def test_hybrid_boost_preserves_image_and_text_scores():
    boost = BehaviorBoost(exact_click_boost=1.5)
    snapshot = _make_snapshot(recent_clicks=("0000000030",))
    results = [
        {
            "article_id": "0000000030",
            "final_score": 0.7,
            "score": 0.7,
            "image_score": 0.8,
            "text_score": 0.6,
            "behavior_score": 0.0,
        }
    ]
    apply_behavior_boost_to_hybrid_results(results, snapshot, boost)
    assert results[0]["image_score"] == 0.8
    assert results[0]["text_score"] == 0.6


def test_hybrid_boost_degraded_snapshot_does_not_raise():
    """A degraded snapshot (no user features) produces zero boost without raising."""
    from degradation import DegradationReason

    boost = BehaviorBoost()
    snapshot = PersonalizationSnapshot.empty(
        "user-1",
        degraded=True,
        degraded_reasons=(DegradationReason.REDIS_TIMEOUT,),
    )
    results = [
        {
            "article_id": "0000000040",
            "final_score": 0.5,
            "score": 0.5,
            "image_score": None,
            "text_score": 0.5,
            "behavior_score": 0.0,
        }
    ]
    apply_behavior_boost_to_hybrid_results(results, snapshot, boost)
    assert results[0]["final_score"] == 0.5
    assert results[0]["behavior_score"] == 0.0


def test_hybrid_boost_empty_results_is_no_op():
    boost = BehaviorBoost()
    snapshot = _make_snapshot()
    results = []
    apply_behavior_boost_to_hybrid_results(results, snapshot, boost)
    assert results == []
