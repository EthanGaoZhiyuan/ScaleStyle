"""
Null Feature Reader - no-op personalization fallback.

Used when Redis-backed personalization cannot be initialized. Exposes only
load_personalization_snapshot for the hot path.
"""

from typing import List, Optional

from src.degradation import DegradationReason
from .snapshot import PersonalizationSnapshot


class NullFeatureReader:
    """No-op snapshot loader; always returns empty degraded snapshot."""

    def load_personalization_snapshot(
        self,
        user_id: Optional[str],
        candidate_item_ids: List[str],
        *,
        max_recent_clicks: int = 20,
    ) -> PersonalizationSnapshot:
        return PersonalizationSnapshot.empty(
            user_id,
            degraded=True,
            degraded_reasons=(DegradationReason.PERSONALIZATION_UNAVAILABLE,),
        )
