"""Request-scoped personalization snapshot for bounded hot-path reads."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Dict, Optional, Set

from src.degradation import DegradationReason


@dataclass(slots=True)
class PersonalizationSnapshot:
    """All Redis-backed personalization inputs for a single rerank request."""

    user_id: Optional[str]
    recent_clicks: tuple[str, ...] = ()
    category_affinity: Dict[str, float] = field(default_factory=dict)
    clicked_categories: Set[str] = field(default_factory=set)
    candidate_item_categories: Dict[str, str] = field(default_factory=dict)
    popularity_signals: Dict[str, Dict[str, float]] = field(default_factory=dict)
    redis_round_trips: int = 0
    degraded: bool = False
    degraded_reasons: tuple[DegradationReason, ...] = ()

    @classmethod
    def empty(
        cls,
        user_id: Optional[str],
        *,
        degraded: bool = False,
        degraded_reasons: tuple[DegradationReason, ...] = (),
        redis_round_trips: int = 0,
    ) -> "PersonalizationSnapshot":
        return cls(
            user_id=user_id,
            degraded=degraded,
            degraded_reasons=degraded_reasons,
            redis_round_trips=redis_round_trips,
        )
