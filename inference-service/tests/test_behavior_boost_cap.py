"""
P1.3: Test behavior boost cap and cleaner structure

Validates:
1. Boost cap prevents excessive score domination
2. Debug mode properly controls debug info emission
3. Boost calculations work correctly
"""

import sys
from pathlib import Path
import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))
sys.path.insert(0, str(Path(__file__).parent.parent.parent / "inference-service" / "src"))

from personalization.behavior_boost import BehaviorBoost
from personalization.snapshot import PersonalizationSnapshot


def make_snapshot(**kwargs):
    base = PersonalizationSnapshot(
        user_id="user_123",
        recent_clicks=(),
        category_affinity={},
        clicked_categories=set(),
        candidate_item_categories={},
        popularity_signals={},
    )
    for key, value in kwargs.items():
        setattr(base, key, value)
    return base


class TestBehaviorBoostCap:
    """P1.3: Test boost cap functionality"""
    
    def test_boost_cap_limits_extreme_boosts(self):
        """P1.3: Test that boost cap prevents excessive score domination"""
        # Set a low cap for testing
        booster = BehaviorBoost(
            exact_click_boost=5.0,  # Very high boost
            max_boost_cap=2.0,  # But cap at 2.0
            debug_mode=False
        )
        
        snapshot = make_snapshot(recent_clicks=("item_123",))
        
        results = [
            {"article_id": "item_123", "rerank_score": 10.0}
        ]
        
        # Apply boost
        stats = booster.apply_boost(snapshot, results)
        
        # Verify boost was capped
        assert results[0]["rerank_score"] == 20.0  # 10.0 * 2.0 (capped, not 5.0)
        assert stats["exact_boost_count"] == 1
    
    def test_boost_below_cap_not_affected(self):
        """P1.3: Test that boosts below cap are not modified"""
        booster = BehaviorBoost(
            exact_click_boost=1.5,
            max_boost_cap=3.0,
            debug_mode=False
        )
        
        snapshot = make_snapshot(recent_clicks=("item_456",))
        
        results = [
            {"article_id": "item_456", "rerank_score": 10.0}
        ]
        
        # Apply boost
        booster.apply_boost(snapshot, results)
        
        # Verify boost was NOT capped (1.5 < 3.0)
        assert results[0]["rerank_score"] == 15.0  # 10.0 * 1.5
    
    def test_debug_mode_disabled_no_debug_info(self):
        """P1.3: Test that debug_mode=False does not add debug info"""
        booster = BehaviorBoost(
            exact_click_boost=1.5,
            debug_mode=False  # Debug mode OFF
        )
        
        snapshot = make_snapshot(recent_clicks=("item_789",))
        
        results = [
            {"article_id": "item_789", "rerank_score": 10.0}
        ]
        
        # Apply boost
        booster.apply_boost(snapshot, results)
        
        # Verify NO debug info added
        assert "_debug" not in results[0]
        assert "boost_reason" not in results[0]
        assert "boost_factor" not in results[0]
    
    def test_debug_mode_enabled_adds_debug_info(self):
        """P1.3: Test that debug_mode=True adds debug info"""
        booster = BehaviorBoost(
            exact_click_boost=1.5,
            debug_mode=True  # Debug mode ON
        )
        
        snapshot = make_snapshot(recent_clicks=("item_999",))
        
        results = [
            {"article_id": "item_999", "rerank_score": 10.0}
        ]
        
        # Apply boost
        booster.apply_boost(snapshot, results)
        
        # Verify debug info added
        assert "_debug" in results[0]
        assert "boost" in results[0]["_debug"]
        assert results[0]["_debug"]["boost"]["original_score"] == 10.0
        assert results[0]["_debug"]["boost"]["boost_factor"] == 1.5
        assert results[0]["_debug"]["boost"]["boost_reason"] == "recent_click"
    
    def test_category_boost_with_cap(self):
        """P1.3: Test category boost respects cap"""
        booster = BehaviorBoost(
            category_affinity_boost=4.0,  # High boost
            max_boost_cap=2.5,  # Cap at 2.5
            debug_mode=False
        )
        
        snapshot = make_snapshot(
            recent_clicks=("other_item",),
            clicked_categories={"dress"},
            candidate_item_categories={"item_dress_123": "dress"},
        )
        
        results = [
            {"article_id": "item_dress_123", "rerank_score": 10.0}
        ]
        
        # Apply boost
        stats = booster.apply_boost(snapshot, results)
        
        # Verify boost was capped
        assert results[0]["rerank_score"] == 25.0  # 10.0 * 2.5 (capped)
        assert stats["category_boost_count"] == 1
    
    def test_update_config_updates_boost_cap(self):
        """P1.3: Test that update_config can change max_boost_cap"""
        booster = BehaviorBoost(
            max_boost_cap=3.0
        )
        
        assert booster.max_boost_cap == 3.0
        
        # Update config
        booster.update_config(max_boost_cap=5.0)
        
        assert booster.max_boost_cap == 5.0

    def test_recent_hot_item_outranks_stale_item_via_windowed_popularity(self):
        booster = BehaviorBoost(
            debug_mode=False
        )

        snapshot = make_snapshot(
            recent_clicks=(),
            popularity_signals={
                "item_hot": {"1h": 12.0, "24h": 20.0, "7d": 25.0},
                "item_stale": {"1h": 0.0, "24h": 1.0, "7d": 40.0},
            },
        )

        results = [
            {"article_id": "item_stale", "rerank_score": 10.0},
            {"article_id": "item_hot", "rerank_score": 10.0},
        ]

        stats = booster.apply_boost(snapshot, results)

        assert results[0]["article_id"] == "item_hot"
        assert stats["popularity_boost_count"] == 2

    def test_missing_popularity_windows_fall_back_gracefully(self):
        booster = BehaviorBoost(
            debug_mode=False
        )

        snapshot = make_snapshot(recent_clicks=(), popularity_signals={})

        results = [
            {"article_id": "item_1", "rerank_score": 10.0},
            {"article_id": "item_2", "rerank_score": 9.0},
        ]

        stats = booster.apply_boost(snapshot, results)

        assert results[0]["article_id"] == "item_1"
        assert stats["popularity_boost_count"] == 0

    def test_behavior_boost_uses_only_snapshot_no_reader(self):
        """BehaviorBoost.apply_boost takes only snapshot and results; no reader."""
        booster = BehaviorBoost(debug_mode=False)
        snapshot = make_snapshot(
            recent_clicks=("item_123",),
            candidate_item_categories={"item_123": "dress"},
            popularity_signals={"item_123": {"1h": 0.0, "24h": 0.0, "7d": 0.0}},  # no popularity boost
        )

        results = [{"article_id": "item_123", "rerank_score": 10.0}]
        stats = booster.apply_boost(snapshot, results)

        assert stats["exact_boost_count"] == 1
        assert results[0]["rerank_score"] == 15.0  # 10.0 * 1.5 exact_click_boost
