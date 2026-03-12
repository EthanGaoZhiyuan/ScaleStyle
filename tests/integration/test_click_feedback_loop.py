import json
import os
import time
import uuid
from collections import Counter
from typing import Any, Dict, List, Optional, Tuple
from urllib import error, parse, request

import pytest
import redis


GATEWAY_BASE_URL = os.getenv("GATEWAY_URL", "http://localhost:8080").rstrip("/")
REDIS_HOST = os.getenv("REDIS_HOST", "localhost")
REDIS_PORT = int(os.getenv("REDIS_PORT", "6379"))

SEARCH_QUERY = os.getenv("E2E_CLICK_FEEDBACK_QUERY", "dress")
SEARCH_K = int(os.getenv("E2E_CLICK_FEEDBACK_K", "20"))
CLICK_COUNT = int(os.getenv("E2E_CLICK_FEEDBACK_CLICKS", "6"))
REDIS_PROCESS_TIMEOUT_SEC = float(os.getenv("E2E_CLICK_FEEDBACK_REDIS_TIMEOUT_SEC", "20"))
RANKING_SHIFT_TIMEOUT_SEC = float(os.getenv("E2E_CLICK_FEEDBACK_RANKING_TIMEOUT_SEC", "25"))
POLL_INTERVAL_SEC = float(os.getenv("E2E_CLICK_FEEDBACK_POLL_INTERVAL_SEC", "0.8"))


@pytest.fixture(scope="module")
def redis_client() -> redis.Redis:
    client = redis.Redis(host=REDIS_HOST, port=REDIS_PORT, db=0, decode_responses=True)
    try:
        client.ping()
    except redis.ConnectionError:
        pytest.skip("Redis not available (start docker-compose stack first)")
    yield client
    client.close()


def _http_json(method: str, url: str, payload: Optional[Dict[str, Any]] = None, timeout: float = 5.0) -> Tuple[int, Any]:
    body = None
    headers = {}
    if payload is not None:
        body = json.dumps(payload).encode("utf-8")
        headers["Content-Type"] = "application/json"

    req = request.Request(url=url, data=body, headers=headers, method=method)
    try:
        with request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read().decode("utf-8")
            return resp.status, json.loads(raw) if raw else {}
    except error.HTTPError as exc:
        raw = exc.read().decode("utf-8") if exc.fp else ""
        parsed = {}
        if raw:
            try:
                parsed = json.loads(raw)
            except json.JSONDecodeError:
                parsed = {"raw": raw}
        return exc.code, parsed


def _healthcheck_or_skip() -> None:
    status, _ = _http_json("GET", f"{GATEWAY_BASE_URL}/events/health", timeout=5.0)
    if status >= 500 or status == 0:
        pytest.skip(f"Gateway unavailable at {GATEWAY_BASE_URL} (status={status})")


def _extract_items(response_body: Any) -> List[Dict[str, Any]]:
    if not isinstance(response_body, dict):
        return []
    data = response_body.get("data")
    if isinstance(data, list):
        return data
    if isinstance(data, dict):
        for key in ("items", "results"):
            if isinstance(data.get(key), list):
                return data[key]
    for key in ("items", "results"):
        if isinstance(response_body.get(key), list):
            return response_body[key]
    return []


def _search(user_id: str, query_text: str, k: int) -> List[Dict[str, Any]]:
    params = parse.urlencode({"query": query_text, "userId": user_id, "k": k, "debug": "false"})
    url = f"{GATEWAY_BASE_URL}/api/recommendation/search?{params}"
    status, body = _http_json("GET", url, timeout=8.0)
    assert status == 200, f"search failed status={status}, body={body}"
    items = _extract_items(body)
    assert items, f"search returned no items: {body}"
    return items


def _send_click(user_id: str, session_id: str, item_id: str, query_text: str, position: int) -> None:
    payload = {
        "user_id": user_id,
        "item_id": item_id,
        "session_id": session_id,
        "source": "search",
        "query": query_text,
        "position": max(0, position),
        "device": "pytest-e2e",
    }
    status, body = _http_json("POST", f"{GATEWAY_BASE_URL}/api/events/click", payload=payload, timeout=8.0)
    assert status == 200, f"click failed status={status}, body={body}, payload={payload}"


def _cleanup_user_keys(client: redis.Redis, user_id: str) -> None:
    keys = list(client.scan_iter(match=f"user:{user_id}:*"))
    if keys:
        client.delete(*keys)


def _normalize_category(value: Optional[str]) -> str:
    return (value or "").strip().lower()


def _rank_map(items: List[Dict[str, Any]]) -> Dict[str, int]:
    out: Dict[str, int] = {}
    for idx, item in enumerate(items):
        item_id = str(item.get("itemId") or "").strip()
        if item_id:
            out[item_id] = idx
    return out


def _target_category_from_baseline(items: List[Dict[str, Any]]) -> Tuple[str, List[str]]:
    grouped: Dict[str, List[str]] = {}
    for item in items:
        item_id = str(item.get("itemId") or "").strip()
        category = str(item.get("category") or "").strip()
        if not item_id or not category:
            continue
        grouped.setdefault(category, []).append(item_id)

    if not grouped:
        return "", []

    category = max(grouped.items(), key=lambda kv: len(kv[1]))[0]
    return category, grouped[category]


def _poll_until(predicate, timeout_sec: float, interval_sec: float):
    deadline = time.monotonic() + timeout_sec
    last = None
    while time.monotonic() < deadline:
        ok, details = predicate()
        last = details
        if ok:
            return True, details
        time.sleep(interval_sec)
    return False, last


@pytest.mark.integration
def test_click_feedback_loop_changes_ranking(redis_client: redis.Redis) -> None:
    """
    Real E2E feedback loop validation:
    user click -> Kafka -> consumer -> Redis features -> next recommendation shifts.
    """
    _healthcheck_or_skip()

    stable_suffix = os.getenv("E2E_CLICK_FEEDBACK_USER", "feedback-loop")
    user_id = f"it_{stable_suffix}"
    session_id = f"sess_{uuid.uuid4().hex[:10]}"

    _cleanup_user_keys(redis_client, user_id)

    try:
        baseline_items = _search(user_id=user_id, query_text=SEARCH_QUERY, k=SEARCH_K)
        baseline_rank = _rank_map(baseline_items)

        target_category, target_item_ids = _target_category_from_baseline(baseline_items)
        if not target_category or len(target_item_ids) < 2:
            pytest.skip("Not enough categorized baseline items to run deterministic feedback assertion")

        clicked_item_ids = target_item_ids[: min(3, len(target_item_ids))]
        category_before_top5 = sum(
            1
            for item in baseline_items[:5]
            if _normalize_category(item.get("category")) == _normalize_category(target_category)
        )

        for idx in range(CLICK_COUNT):
            clicked = clicked_item_ids[idx % len(clicked_item_ids)]
            position = baseline_rank.get(clicked, idx)
            _send_click(
                user_id=user_id,
                session_id=session_id,
                item_id=clicked,
                query_text=SEARCH_QUERY,
                position=position,
            )

        affinity_key = f"user:{user_id}:category_affinity"
        recent_key = f"user:{user_id}:recent_clicks"

        def redis_processed_predicate():
            affinity_score_raw = redis_client.hget(affinity_key, target_category)
            recent_clicks = redis_client.lrange(recent_key, 0, 25)
            recent_set = set(recent_clicks)
            has_recent = any(item_id in recent_set for item_id in clicked_item_ids)

            affinity_score = 0.0
            if affinity_score_raw is not None:
                try:
                    affinity_score = float(affinity_score_raw)
                except ValueError:
                    affinity_score = 0.0

            details = {
                "affinity_score": affinity_score,
                "has_recent": has_recent,
                "recent_sample": recent_clicks[:5],
            }
            return affinity_score >= 1.0 and has_recent, details

        redis_ready, redis_details = _poll_until(
            redis_processed_predicate,
            timeout_sec=REDIS_PROCESS_TIMEOUT_SEC,
            interval_sec=POLL_INTERVAL_SEC,
        )
        assert redis_ready, f"consumer update not observed in Redis within timeout: {redis_details}"

        baseline_order_top10 = [str(item.get("itemId")) for item in baseline_items[:10]]

        def ranking_shift_predicate():
            after_items = _search(user_id=user_id, query_text=SEARCH_QUERY, k=SEARCH_K)
            after_rank = _rank_map(after_items)
            after_order_top10 = [str(item.get("itemId")) for item in after_items[:10]]
            ranking_changed = after_order_top10 != baseline_order_top10

            lifts = []
            for item_id in clicked_item_ids:
                if item_id in baseline_rank and item_id in after_rank:
                    lifts.append(baseline_rank[item_id] - after_rank[item_id])
            best_lift = max(lifts) if lifts else 0

            category_after_top5 = sum(
                1
                for item in after_items[:5]
                if _normalize_category(item.get("category")) == _normalize_category(target_category)
            )
            concentration_delta = category_after_top5 - category_before_top5

            measurable = ranking_changed and (best_lift >= 1 or concentration_delta >= 1)
            details = {
                "ranking_changed": ranking_changed,
                "best_clicked_item_rank_lift": best_lift,
                "category_before_top5": category_before_top5,
                "category_after_top5": category_after_top5,
                "concentration_delta_top5": concentration_delta,
                "baseline_top10": baseline_order_top10,
                "after_top10": after_order_top10,
                "clicked_items": clicked_item_ids,
                "target_category": target_category,
            }
            return measurable, details

        shifted, shift_details = _poll_until(
            ranking_shift_predicate,
            timeout_sec=RANKING_SHIFT_TIMEOUT_SEC,
            interval_sec=POLL_INTERVAL_SEC,
        )

        assert shifted, (
            "feedback loop did not produce measurable ranking shift within timeout: "
            f"{json.dumps(shift_details, ensure_ascii=True)}"
        )

    finally:
        _cleanup_user_keys(redis_client, user_id)
