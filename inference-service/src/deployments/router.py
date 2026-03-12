from ray import serve
import re

from src.utils.bucketing import bucket_user


@serve.deployment(
    # Lightweight routing logic needs minimal CPU
    ray_actor_options={"num_cpus": 0.05}
)
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
            flow = "base" if bucket_user(user_id, 2) == 1 else "smart"
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
        flow = "base" if bucket_user(user_id, 2) == 1 else "smart"
        return {"intent": "SEARCH", "filters": filters, "flow": flow}
