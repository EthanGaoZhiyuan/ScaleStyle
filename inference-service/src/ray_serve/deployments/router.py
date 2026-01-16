from ray import serve
import re


@serve.deployment
class RouterDeployment:
    def __init__(self):
        self.colours = {"red": "Red", "blue": "Blue", "black": "Black"}

        self.price_patterns = [
            re.compile(r"(?:under|below|max)\s*\$?\s*(\d+(?:\.\d+)?)", re.I),
            re.compile(r"(?:<=|<)\s*\$?\s*(\d+(?:\.\d+)?)"),
        ]

    def route(self, query: str):
        q = (query or "").strip()
        ql = q.lower()

        if any(x in ql for x in ["recommend", "trending", "hot"]):
            return {"intent": "BROWSE", "filters": {}}

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
                filters["price_lt"] = float(m.group(1))
                break

        return {"intent": "SEARCH", "filters": filters}
