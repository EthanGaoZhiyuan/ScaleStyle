"""
Behavior Boost - Apply user behavior-based reranking boost

Applies personalization boosts to ranking results based on:
- Recent clicks (exact item match)
- Category affinity
- Session history

Week 2: Real-time behavior loop
P0.4: Fixed fake async - now properly synchronous
P1.6: Made boost constants configurable via PersonalizationConfig
P2.9: Added debug mode support
"""
import logging
from typing import Dict, List, Optional, Any, Set

from .feature_reader import FeatureReader

logger = logging.getLogger(__name__)


class BehaviorBoost:
    """
    Applies behavior-based boost to ranking results
    P0.4: Synchronous API (removed fake async)
    P1.6: Config-driven boost parameters
    P2.9: Debug mode for ranking influence
    """
    
    def __init__(
        self, 
        feature_reader: FeatureReader,
        exact_click_boost: float = 1.5,
        category_affinity_boost: float = 1.2,
        max_recent_clicks: int = 20,
        debug_mode: bool = False
    ):
        """
        Args:
            feature_reader: FeatureReader instance
            exact_click_boost: Multiplier for exact item match (P1.6: configurable)
            category_affinity_boost: Multiplier for category match (P1.6: configurable)
            max_recent_clicks: Max recent clicks to consider
            debug_mode: If True, add _debug info to results (P2.9)
        """
        self.feature_reader = feature_reader
        self.exact_click_boost = exact_click_boost
        self.category_affinity_boost = category_affinity_boost
        self.max_recent_clicks = max_recent_clicks
        self.debug_mode = debug_mode
    
    def apply_boost(
        self, 
        user_id: Optional[str], 
        results: List[Dict[str, Any]]
    ) -> Dict[str, Any]:
        """
        Apply user behavior-based boosting to reranked results
        P0.4: Now synchronous (no fake async)
        
        Boosting strategies:
        1. Exact match (recently clicked item) → exact_click_boost
        2. Category affinity (same category as clicked items) → category_affinity_boost
        
        Args:
            user_id: User ID (None for anonymous)
            results: List of ranking results (dicts with article_id, rerank_score)
        
        Returns:
            Dict with boost statistics:
            {
                "boosted_items": int,
                "exact_boost_count": int,
                "category_boost_count": int,
                "avg_boost_factor": float
            }
        """
        if not user_id or not results:
            return {
                "boosted_items": 0,
                "exact_boost_count": 0,
                "category_boost_count": 0,
                "avg_boost_factor": 1.0
            }
        
        # Fetch user features (P0.4: Now synchronous)
        recent_clicks = self.feature_reader.get_user_recent_clicks(
            user_id, max_items=self.max_recent_clicks
        )
        
        if not recent_clicks:
            logger.debug(f"No recent clicks for user {user_id}, skip boost")
            return {"boosted_items": 0, "exact_boost_count": 0, "category_boost_count": 0, "avg_boost_factor": 1.0}
        
        # Get categories of clicked items
        clicked_categories = self.feature_reader.get_clicked_categories(
            user_id, recent_clicks
        )
        
        # Apply boost to each result
        boosted_items = 0
        exact_boost_count = 0
        category_boost_count = 0
        total_boost_factor = 0.0
        
        for result in results:
            article_id = result.get("article_id")
            original_score = result.get("rerank_score", 0.0)
            boost_factor = 1.0
            boost_reason = None
            
            # Strategy 1: Exact item match (recently clicked)
            if article_id in recent_clicks:
                boost_factor *= self.exact_click_boost
                boost_reason = "recent_click"
                exact_boost_count += 1
            
            # Strategy 2: Category affinity (same category as clicked items)
            elif clicked_categories:
                item_category = self.feature_reader.get_item_category(article_id)
                if item_category and item_category in clicked_categories:
                    boost_factor *= self.category_affinity_boost
                    boost_reason = "category_affinity"
                    category_boost_count += 1
            
            # Apply boost
            if boost_factor > 1.0:
                result["rerank_score"] *= boost_factor
                boosted_items += 1
                total_boost_factor += boost_factor
                
                # P2.9: Add debug info if enabled (NOT in normal response)
                if self.debug_mode:
                    if "_debug" not in result:
                        result["_debug"] = {}
                    result["_debug"]["boost"] = {
                        "original_score": original_score,
                        "boost_factor": boost_factor,
                        "boosted_score": result["rerank_score"],
                        "boost_reason": boost_reason
                    }
                    # Keep legacy fields for backward compat in debug mode
                    result["boost_reason"] = boost_reason
                    result["boost_factor"] = boost_factor
        
        # Re-sort by boosted scores
        results.sort(key=lambda x: x.get("rerank_score", -1e9), reverse=True)
        
        avg_boost_factor = (total_boost_factor / boosted_items) if boosted_items > 0 else 1.0
        
        logger.debug(
            f"🎯 Behavior boost applied: {boosted_items} items, "
            f"exact={exact_boost_count}, category={category_boost_count}, "
            f"avg_factor={avg_boost_factor:.2f}"
        )
        
        return {
            "boosted_items": boosted_items,
            "exact_boost_count": exact_boost_count,
            "category_boost_count": category_boost_count,
            "avg_boost_factor": round(avg_boost_factor, 2)
        }
    
    def update_config(
        self,
        exact_click_boost: Optional[float] = None,
        category_affinity_boost: Optional[float] = None,
        max_recent_clicks: Optional[int] = None
    ):
        """
        Update boost configuration at runtime
        P1.6: Allows dynamic tuning of boost parameters
        
        Args:
            exact_click_boost: New exact match boost multiplier
            category_affinity_boost: New category boost multiplier
            max_recent_clicks: New max recent clicks limit
        """
        if exact_click_boost is not None:
            self.exact_click_boost = exact_click_boost
            logger.info(f"✅ Updated exact_click_boost → {exact_click_boost}")
        
        if category_affinity_boost is not None:
            self.category_affinity_boost = category_affinity_boost
            logger.info(f"✅ Updated category_affinity_boost → {category_affinity_boost}")
        
        if max_recent_clicks is not None:
            self.max_recent_clicks = max_recent_clicks
            logger.info(f"✅ Updated max_recent_clicks → {max_recent_clicks}")
