from typing import Any

ARTICLE_ID_WIDTH = 10


def canonical_article_id(article_id: Any) -> str:
    value = "" if article_id is None else str(article_id).strip()
    if not value:
        return ""
    if value.isdigit():
        return str(int(value)).zfill(ARTICLE_ID_WIDTH)
    return value


def item_meta_key(article_id: Any) -> str:
    return f"item:{canonical_article_id(article_id)}:meta"
