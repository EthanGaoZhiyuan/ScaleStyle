from ray import serve
import re
import zlib
import hashlib
from typing import Optional


@serve.deployment
class RouterDeployment:
    def __init__(self):
        self.colours = {"red": "Red", "blue": "Blue", "black": "Black"}

        self.price_patterns = [
            re.compile(r"(?:under|below|max)\s*\$?\s*(\d+(?:\.\d+)?)", re.I),
            re.compile(r"(?:<=|<)\s*\$?\s*(\d+(?:\.\d+)?)"),
        ]
        self.price_scale = 1000.0

    def _bucket(self, user_id: Optional[str]) -> int:
        if not user_id:
            return 0  # 默认 smart
        m = re.search(r"(\d+)$", user_id)
        if m:
            return int(m.group(1)) % 2
        h = hashlib.md5(user_id.encode("utf-8")).hexdigest()
        return int(h, 16) % 2

    def route(self, query: str, user_id: str | None = None) -> dict:
        q = (query or "").strip()
        ql = q.lower()

        if any(x in ql for x in ["recommend", "trending", "hot"]):
            flow = "base" if self._bucket(user_id) == 1 else "smart"
            return {"intent": "BROWSE", "filters": {}, "flow": flow}

        filters: dict = {}

        # colour
        for k, v in self.colours.items():
            if re.search(rf"\b{k}\b", ql):
                filters["colour_group_name"] = v
                break

        # price_lt
        for pat in self.price_patterns:
            m = pat.search(ql)
            if m:
                val = float(m.group(1))
                if val >= 1.0:
                    val = val / self.price_scale
                filters["price_lt"] = val
                break

        flow = "base" if self._bucket(user_id) == 1 else "smart"
        return {"intent": "SEARCH", "filters": filters, "flow": flow}

    def _choose_flow(self, user_id: str | None) -> str:
        if not user_id:
            return "smart"
        bucket = zlib.crc32(user_id.encode("utf-8")) % 2
        return "smart" if bucket == 0 else "base"
