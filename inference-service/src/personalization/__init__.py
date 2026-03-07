"""
Personalization Module

Handles user behavior-based personalization:
- Feature reading from Redis
- Behavior boost calculation
- Ranking adjustment

Week 2: Real-time behavior loop personalization
"""

from .feature_reader import FeatureReader
from .behavior_boost import BehaviorBoost

__all__ = ["FeatureReader", "BehaviorBoost"]
