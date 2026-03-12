"""
Behavior Boost - Apply user behavior-based reranking boost

Applies personalization boosts to ranking results based on:
- Recent clicks (exact item match)
- Category affinity
- Rolling online popularity signals
- Session history
"""
import logging
import math
from typing import Dict, List, Optional, Any

from src.utils.redis_metadata import canonical_article_id
from .snapshot import PersonalizationSnapshot

logger = logging.getLogger(__name__)


class BehaviorBoost:
    """Applies behavior-based boost to ranking results"""
    
    def __init__(
        self, 
        exact_click_boost: float = 1.5,
        category_affinity_boost: float = 1.2,
        popularity_1h_boost: float = 0.20,
        popularity_24h_boost: float = 0.10,
        popularity_7d_boost: float = 0.05,
        max_recent_clicks: int = 20,
        max_boost_cap: float = 3.0,  # P1.3: Cap to prevent complete domination
        debug_mode: bool = False
    ):
        """
        Args:
            exact_click_boost: Multiplier for exact item match
            category_affinity_boost: Multiplier for category match
            popularity_1h_boost: Additive multiplier budget for 1h popularity
            popularity_24h_boost: Additive multiplier budget for 24h popularity
            popularity_7d_boost: Additive multiplier budget for 7d popularity
            max_recent_clicks: Max recent clicks to consider
            max_boost_cap: Maximum total boost factor to prevent score domination
            debug_mode: If True, add _debug info to results
        """
        self.exact_click_boost = exact_click_boost
        self.category_affinity_boost = category_affinity_boost
        self.popularity_1h_boost = popularity_1h_boost
        self.popularity_24h_boost = popularity_24h_boost
        self.popularity_7d_boost = popularity_7d_boost
        self.max_recent_clicks = max_recent_clicks
        self.max_boost_cap = max_boost_cap
        self.debug_mode = debug_mode
    
    def apply_boost(
        self, 
        snapshot: PersonalizationSnapshot,
        results: List[Dict[str, Any]]
    ) -> Dict[str, Any]:
        """
        Apply user behavior-based boosting to reranked results using a request snapshot.
        
        Boosting strategies:
        1. Exact match (recently clicked item) → exact_click_boost
        2. Category affinity (same category as clicked items) → category_affinity_boost
        
        Uses batch Redis queries to avoid N+1 performance issues.
        
        Args:
            snapshot: Preloaded personalization snapshot for the request
            results: List of ranking results (dicts with article_id, rerank_score)
        
        Returns:
            Dict with boost statistics:
            {
                "boosted_items": int,
                "exact_boost_count": int,
                "category_boost_count": int,
                "popularity_boost_count": int,
                "avg_boost_factor": float
            }
        """
        if not snapshot.user_id or not results:
            return {
                "boosted_items": 0,
                "exact_boost_count": 0,
                "category_boost_count": 0,
                "popularity_boost_count": 0,
                "avg_boost_factor": 1.0
            }

        recent_clicks = snapshot.recent_clicks
        if not recent_clicks:
            logger.debug(
                "No recent clicks in snapshot for user %s, applying popularity-only boost if available",
                snapshot.user_id,
            )

        candidate_categories = snapshot.candidate_item_categories
        popularity_signals = snapshot.popularity_signals
        max_popularity = {
            "1h": max((signals.get("1h", 0.0) for signals in popularity_signals.values()), default=0.0),
            "24h": max((signals.get("24h", 0.0) for signals in popularity_signals.values()), default=0.0),
            "7d": max((signals.get("7d", 0.0) for signals in popularity_signals.values()), default=0.0),
        }
        
        # Apply boost to each result
        boosted_items = 0
        exact_boost_count = 0
        category_boost_count = 0
        popularity_boost_count = 0
        total_boost_factor = 0.0
        
        # P1.3: Create new scores instead of mutating in place
        for result in results:
            article_id = result.get("article_id")
            canonical_id = canonical_article_id(article_id)
            original_score = result.get("rerank_score", 0.0)
            boost_factor = 1.0
            boost_reason = None
            boost_reasons = []
            
            # Strategy 1: Exact item match (recently clicked)
            if canonical_id in recent_clicks:
                boost_factor *= self.exact_click_boost
                boost_reason = "recent_click"
                boost_reasons.append("recent_click")
                exact_boost_count += 1
            
            # Strategy 2: Category affinity (same category as clicked items)
            elif snapshot.clicked_categories:
                # Use pre-fetched category instead of individual lookup
                item_category = candidate_categories.get(canonical_id)
                if item_category and item_category in snapshot.clicked_categories:
                    boost_factor *= self.category_affinity_boost
                    boost_reason = "category_affinity"
                    boost_reasons.append("category_affinity")
                    category_boost_count += 1

            # Strategy 3: Rolling popularity windows for real-time hotness.
            popularity_signal = popularity_signals.get(canonical_id, {})
            popularity_factor = 1.0
            if max_popularity["1h"] > 0:
                popularity_factor += self.popularity_1h_boost * (
                    math.log1p(popularity_signal.get("1h", 0.0)) / math.log1p(max_popularity["1h"])
                )
            if max_popularity["24h"] > 0:
                popularity_factor += self.popularity_24h_boost * (
                    math.log1p(popularity_signal.get("24h", 0.0)) / math.log1p(max_popularity["24h"])
                )
            if max_popularity["7d"] > 0:
                popularity_factor += self.popularity_7d_boost * (
                    math.log1p(popularity_signal.get("7d", 0.0)) / math.log1p(max_popularity["7d"])
                )
            if popularity_factor > 1.0:
                boost_factor *= popularity_factor
                popularity_boost_count += 1
                if boost_reason is None:
                    boost_reason = "online_popularity"
                boost_reasons.append("online_popularity")
            
            # P1.3: Apply cap to prevent complete score domination
            if boost_factor > self.max_boost_cap:
                logger.debug(f"Capping boost factor {boost_factor:.2f} -> {self.max_boost_cap:.2f} for item {article_id}")
                boost_factor = self.max_boost_cap
            
            # Apply boost (cleaner scoring update)
            if boost_factor > 1.0:
                boosted_score = original_score * boost_factor
                result["rerank_score"] = boosted_score
                boosted_items += 1
                total_boost_factor += boost_factor
                
                # P1.3: Add debug info only if debug_mode enabled
                if self.debug_mode:
                    if "_debug" not in result:
                        result["_debug"] = {}
                    result["_debug"]["boost"] = {
                        "original_score": original_score,
                        "boost_factor": boost_factor,
                        "boosted_score": boosted_score,
                        "boost_reason": boost_reason,
                        "boost_reasons": boost_reasons,
                        "popularity_signal": popularity_signal,
                        "snapshot_degraded": snapshot.degraded,
                    }
        
        # Re-sort by boosted scores
        results.sort(key=lambda x: x.get("rerank_score", -1e9), reverse=True)
        
        avg_boost_factor = (total_boost_factor / boosted_items) if boosted_items > 0 else 1.0
        
        logger.debug(
            f"🎯 Behavior boost applied: {boosted_items} items, "
            f"exact={exact_boost_count}, category={category_boost_count}, popularity={popularity_boost_count}, "
            f"avg_factor={avg_boost_factor:.2f}"
        )
        
        return {
            "boosted_items": boosted_items,
            "exact_boost_count": exact_boost_count,
            "category_boost_count": category_boost_count,
            "popularity_boost_count": popularity_boost_count,
            "avg_boost_factor": round(avg_boost_factor, 2)
        }
    
    def update_config(
        self,
        exact_click_boost: Optional[float] = None,
        category_affinity_boost: Optional[float] = None,
        max_recent_clicks: Optional[int] = None,
        max_boost_cap: Optional[float] = None
    ):
        """
        Update boost configuration at runtime
        
        Args:
            exact_click_boost: New exact match boost multiplier
            category_affinity_boost: New category boost multiplier
            max_recent_clicks: New max recent clicks limit
            max_boost_cap: New maximum boost cap
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
        
        if max_boost_cap is not None:
            self.max_boost_cap = max_boost_cap
            logger.info(f"✅ Updated max_boost_cap → {max_boost_cap}")
