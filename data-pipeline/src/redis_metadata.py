from collections.abc import Mapping
from typing import Any

ARTICLE_ID_WIDTH = 10


def canonical_article_id(article_id: Any) -> str:
    value = "" if article_id is None else str(article_id).strip()
    if not value:
        return ""
    if value.isdigit():
        return str(int(value)).zfill(ARTICLE_ID_WIDTH)
    return value


def item_key(article_id: Any) -> str:
    return f"item:{canonical_article_id(article_id)}"


def item_meta_key(article_id: Any) -> str:
    return f"{item_key(article_id)}:meta"


def _local_image_url(article_id: str) -> str:
    if not article_id:
        return ""
    folder = article_id[:3]
    return f"/static/images/{folder}/{article_id}.jpg"


def build_item_metadata(row: Mapping[str, Any]) -> dict[str, str]:
    article_id = canonical_article_id(row.get("article_id"))
    product_type_name = str(row.get("product_type_name", "") or "")
    raw_image_url = str(row.get("image_url", "") or "")
    image_url = raw_image_url if raw_image_url else _local_image_url(article_id)
    return {
        "article_id": article_id,
        "prod_name": str(row.get("prod_name", "Unknown Product") or "Unknown Product"),
        "colour_group_name": str(row.get("colour_group_name", "") or ""),
        "product_type_name": product_type_name,
        "department_name": str(row.get("department_name", "") or ""),
        "detail_desc": str(row.get("detail_desc", "") or "")[:5000],
        "category": product_type_name or "unknown",
        "price": str(row.get("price", "") or ""),
        "image_url": image_url,
    }
