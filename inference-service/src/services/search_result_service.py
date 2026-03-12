"""Business-layer helpers for result enrichment and ranking inputs.

These functions are intentionally transport-agnostic so ingress handlers can stay
thin while domain assembly rules stay centralized and reusable.
"""

import asyncio
import logging
from typing import Optional

from src.utils.contract import _local_image_url
from src.utils.redis_metadata import canonical_article_id, item_key

logger = logging.getLogger("scalestyle.search_result_service")

# Required fields that must be present in every result item for API contract compliance
CONTRACT_FIELDS = ("title", "image_url", "dept", "desc", "price", "color")


def normalize_meta(article_id: str, raw: dict) -> tuple[dict, list[str]]:
    """Normalize and standardize item metadata for API contract compliance."""

    def g(k: str, default: str = "") -> str:
        v = raw.get(k, default) if raw else default
        return v if v is not None else default

    color = g("colour_group_name", "")
    dept = g("department_name", "")
    desc = g("detail_desc", "")
    price = g("price", "")
    product_type = g("product_type_name", "")
    prod_name = g("prod_name", "")

    title = prod_name or product_type or (f"{color} item" if color else "item")
    image_url = g("image_url", "")
    if not image_url:
        image_url = _local_image_url(article_id)

    meta = {
        "article_id": article_id,
        "title": title,
        "image_url": image_url,
        "dept": dept,
        "department_name": dept,
        "desc": desc,
        "price": price,
        "color": color,
        "colour_group_name": color,
        "product_type_name": product_type,
    }

    missing = []
    for f in CONTRACT_FIELDS:
        v = meta.get(f, "")
        if v is None or (isinstance(v, str) and not v.strip()):
            missing.append(f)

    return meta, missing


async def enrich_and_filter(
    redis_client,
    candidates,
    filters,
    k,
    limit: Optional[int] = None,
    cache_hit_metric=None,
    cache_miss_metric=None,
):
    """Enrich candidates with Redis metadata and apply post-retrieval filters."""
    colour = filters.get("colour_group_name")
    price_lt = filters.get("price_lt")

    ids = []
    for c in candidates:
        aid = c.get("article_id")
        if aid is not None:
            ids.append(canonical_article_id(aid))

    pipe = redis_client.pipeline()
    for aid in ids:
        pipe.hgetall(item_key(aid))
    metas = await asyncio.to_thread(pipe.execute, raise_on_error=False)

    results = []
    for c, meta in zip(candidates, metas):
        aid = canonical_article_id(c.get("article_id"))
        if isinstance(meta, Exception):
            logger.warning("redis_hgetall_failed article_id=%s err=%s", aid, meta)
            if cache_miss_metric:
                cache_miss_metric.labels(operation="metadata").inc()
            continue

        if not meta:
            if cache_miss_metric:
                cache_miss_metric.labels(operation="metadata").inc()
            continue

        if cache_hit_metric:
            cache_hit_metric.labels(operation="metadata").inc()

        if colour and meta.get("colour_group_name") != colour:
            continue

        if price_lt is not None:
            try:
                if float(meta.get("price", "inf")) >= float(price_lt):
                    continue
            except Exception:
                pass

        results.append({"article_id": aid, "score": c.get("score"), "meta": meta})
        if limit is not None and len(results) >= limit:
            break

    return results


def build_rerank_doc(meta: dict) -> str:
    """Build reranker input text from product metadata."""
    parts = [
        meta.get("prod_name") or meta.get("product_type_name") or "",
        meta.get("product_group_name") or "",
        meta.get("department_name") or "",
        meta.get("colour_group_name") or "",
        meta.get("detail_desc") or "",
    ]
    return " | ".join([p for p in parts if p])
