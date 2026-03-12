"""
Personalization snapshot loader protocol.

The request hot path MUST use only this interface. No fine-grained Redis
readers. Load snapshot once, consume snapshot only.
"""

from __future__ import annotations

from typing import List, Optional, Protocol, runtime_checkable

from .snapshot import PersonalizationSnapshot


@runtime_checkable
class PersonalizationSnapshotLoader(Protocol):
    """Protocol for loading request-scoped personalization data.

    Serving code (ingress, reranker) MUST depend only on this. No get_user_*,
    get_item_* or other fine-grained methods in the hot path.
    """

    def load_personalization_snapshot(
        self,
        user_id: Optional[str],
        candidate_item_ids: List[str],
        *,
        max_recent_clicks: int = 20,
    ) -> PersonalizationSnapshot: ...
