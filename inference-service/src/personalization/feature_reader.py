"""
Feature Reader - Read online features from Redis

Reads user behavior features for personalization:
- Recent clicks
- Category affinity (P2.8: using hash schema)
- Session clicks
- Item metadata

Week 2: Real-time behavior loop

P0.4: Fixed fake async - now properly synchronous since Redis client is sync
"""
import logging
from typing import Dict, List, Optional, Set

logger = logging.getLogger(__name__)


class FeatureReader:
    """
    Reads online features from Redis for personalization
    P0.4: Synchronous API (no fake async wrappers)
    """
    
    def __init__(self, redis_client):
        """
        Args:
            redis_client: Synchronous Redis client instance
        """
        self.redis = redis_client
    
    def get_user_recent_clicks(
        self, 
        user_id: Optional[str], 
        max_items: int = 20
    ) -> List[str]:
        """
        Get user's recent clicked items
        
        Args:
            user_id: User ID
            max_items: Max number of recent items to fetch
        
        Returns:
            List of item IDs (most recent first)
        """
        if not user_id:
            return []
        
        try:
            key = f"user:{user_id}:recent_clicks"
            # Get most recent N items
            items = self.redis.lrange(key, 0, max_items - 1)
            
            # Convert bytes to str if needed
            if items and isinstance(items[0], bytes):
                items = [item.decode('utf-8') for item in items]
            
            logger.debug(f"📖 User {user_id} recent clicks: {len(items)} items")
            return items
        
        except Exception as e:
            logger.warning(f"⚠️  Failed to read recent clicks for user {user_id}: {e}")
            return []
    
    def get_user_category_affinity(
        self, 
        user_id: Optional[str]
    ) -> Dict[str, float]:
        """
        Get user's category affinity scores
        P2.8: Now reads from hash schema (user:{uid}:category_affinity)
        
        Args:
            user_id: User ID
        
        Returns:
            Dict mapping category -> affinity score
        """
        if not user_id:
            return {}
        
        try:
            # P2.8: Read from hash (cleaner schema)
            key = f"user:{user_id}:category_affinity"
            affinity_raw = self.redis.hgetall(key)
            
            # Convert to dict[str, float]
            affinity = {}
            for cat, score in affinity_raw.items():
                if isinstance(cat, bytes):
                    cat = cat.decode('utf-8')
                affinity[cat] = float(score)
            
            logger.debug(f"📖 User {user_id} category affinity: {len(affinity)} categories")
            return affinity
        
        except Exception as e:
            logger.warning(f"⚠️  Failed to read category affinity for user {user_id}: {e}")
            return {}
    
    def get_item_category(self, item_id: str) -> Optional[str]:
        """
        Get item's category from Redis metadata
        
        Args:
            item_id: Item ID
        
        Returns:
            Category string, or None if not found
        """
        try:
            key = f"item:{item_id}:meta"
            category = self.redis.hget(key, "category")
            
            if isinstance(category, bytes):
                category = category.decode('utf-8')
            
            return category
        
        except Exception as e:
            logger.debug(f"Failed to read category for item {item_id}: {e}")
            return None
    
    def get_session_clicks(
        self, 
        session_id: Optional[str], 
        max_items: int = 10
    ) -> List[str]:
        """
        Get items clicked in current session
        
        Args:
            session_id: Session ID
            max_items: Max items to fetch
        
        Returns:
            List of item IDs
        """
        if not session_id:
            return []
        
        try:
            key = f"session:{session_id}:clicks"
            items = self.redis.lrange(key, 0, max_items - 1)
            
            if items and isinstance(items[0], bytes):
                items = [item.decode('utf-8') for item in items]
            
            return items
        
        except Exception as e:
            logger.debug(f"Failed to read session clicks for {session_id}: {e}")
            return []
    
    def get_clicked_categories(
        self, 
        user_id: Optional[str], 
        item_ids: List[str]
    ) -> Set[str]:
        """
        Get categories of items user has clicked
        
        Args:
            user_id: User ID
            item_ids: List of recent clicked item IDs
        
        Returns:
            Set of category strings
        """
        categories = set()
        
        for item_id in item_ids[:20]:  # Limit to prevent too many Redis calls
            category = self.get_item_category(item_id)
            if category and category != "unknown":
                categories.add(category)
        
        return categories
