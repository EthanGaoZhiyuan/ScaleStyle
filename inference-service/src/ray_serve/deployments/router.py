from ray import serve
import re
import hashlib
from typing import Optional


@serve.deployment
class RouterDeployment:
    """
    Router deployment for query intent classification and A/B test bucketing.

    Handles:
    1. Intent detection (BROWSE vs SEARCH)
    2. Filter extraction (color, price)
    3. A/B test flow assignment (smart vs base)
    """

    def __init__(self):
        """
        Initialize router with color mappings and price extraction patterns.
        """
        # Supported color mappings (lowercase -> proper case)
        self.colours = {"red": "Red", "blue": "Blue", "black": "Black"}

        # Regex patterns for price extraction (supports "under $50", "< 50", etc.)
        self.price_patterns = [
            re.compile(r"(?:under|below|max)\s*\$?\s*(\d+(?:\.\d+)?)", re.I),
            re.compile(r"(?:<=|<)\s*\$?\s*(\d+(?:\.\d+)?)"),
        ]
        # Price scale factor (Kaggle dataset uses fractional prices)
        self.price_scale = 1000.0

    def _bucket(self, user_id: Optional[str]) -> int:
        """
        Assign user to A/B test bucket using stable hashing.

        Uses MD5 hash for consistent bucket assignment across requests.
        Bucket 0 = smart flow (with reranking)
        Bucket 1 = base flow (without reranking)

        Args:
            user_id: User identifier, or None for default bucket.

        Returns:
            int: 0 for smart flow, 1 for base flow.
        """
        if not user_id:
            return 0  # Default to smart flow
        # Try to extract trailing digits for simpler bucketing
        m = re.search(r"(\d+)$", user_id)
        if m:
            return int(m.group(1)) % 2
        # Fallback to MD5 hash
        h = hashlib.md5(user_id.encode("utf-8")).hexdigest()
        return int(h, 16) % 2

    async def route(self, query: str, user_id: str | None = None) -> dict:
        """
        Route query to appropriate intent and extract filters (async).

        Wraps the routing logic to be fully async-compatible with Ray Serve.

        Args:
            query: User search query.
            user_id: Optional user identifier for A/B bucketing.

        Returns:
            dict: Routing result with intent, filters, and flow assignment.
        """
        q = (query or "").strip()
        ql = q.lower()

        # Detect BROWSE intent from keywords
        if any(x in ql for x in ["recommend", "trending", "hot"]):
            flow = "base" if self._bucket(user_id) == 1 else "smart"
            return {"intent": "BROWSE", "filters": {}, "flow": flow}

        # Extract filters for SEARCH intent
        filters: dict = {}

        # Extract color filter
        for k, v in self.colours.items():
            if re.search(rf"\b{k}\b", ql):
                filters["colour_group_name"] = v
                break

        # Extract price limit filter
        for pat in self.price_patterns:
            m = pat.search(ql)
            if m:
                val = float(m.group(1))
                # Normalize price (convert from dollars to fractional format)
                if val >= 1.0:
                    val = val / self.price_scale
                filters["price_lt"] = val
                break

        # Assign A/B test flow
        flow = "base" if self._bucket(user_id) == 1 else "smart"
        return {"intent": "SEARCH", "filters": filters, "flow": flow}
