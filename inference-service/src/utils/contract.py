"""
Stable API contract normalization for recommendation results.

Extracted for testability without ingress/observability dependencies.
"""


def _local_image_url(article_id: str) -> str:
    """
    Generate local static image URL from article ID.

    Uses the same format as backfill_redis_contract.py for consistency.
    """
    if not article_id:
        return ""
    aid = str(article_id).strip().zfill(10)
    folder = aid[:3]
    return f"/static/images/{folder}/{aid}.jpg"


def contract_normalize(results: list, limit: int):
    """
    Normalize results to a stable contract WITHOUT querying Redis again.

    Contract meta fields:
      title / image_url / dept / desc / price / color

    Returns:
      (normalized_results, contract_debug)
    """
    required = ["title", "image_url", "dept", "desc", "price", "color"]
    missing_by_field = {k: 0 for k in required}

    out = []
    for r in (results or [])[:limit]:
        raw = (r.get("meta") or {}) if isinstance(r, dict) else {}
        aid = str(r.get("article_id") or raw.get("article_id") or "")

        title = (
            raw.get("prod_name")
            or raw.get("title")
            or raw.get("product_type_name")
            or raw.get("product_group_name")
            or ""
        )
        existing_image = raw.get("image_url", "").strip()
        image_url = existing_image if existing_image else _local_image_url(aid)

        dept = raw.get("department_name") or raw.get("dept") or ""
        desc = raw.get("detail_desc") or raw.get("desc") or ""
        color = raw.get("colour_group_name") or raw.get("color") or ""

        price_val = raw.get("price")
        try:
            price = float(price_val) if price_val not in (None, "") else None
        except Exception:
            price = None

        meta = {
            "title": title,
            "image_url": image_url,
            "dept": dept,
            "desc": desc,
            "price": price,
            "color": color,
            "reason": str(raw.get("reason") or ""),
        }

        for k in required:
            v = meta.get(k)
            if v is None or (isinstance(v, str) and v.strip() == ""):
                missing_by_field[k] += 1

        r["article_id"] = int(aid) if aid.isdigit() else aid
        r["meta"] = meta
        out.append(r)

    missing_total = sum(missing_by_field.values())
    return out, {"missing_total": missing_total, "missing_by_field": missing_by_field}


# Alias for ingress compatibility
_contract_normalize = contract_normalize
