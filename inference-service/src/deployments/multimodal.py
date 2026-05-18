"""Helpers for bounded multimodal candidate merge on the serving hot path."""

from __future__ import annotations

from typing import Any, Dict, Iterable, List


def merge_ranked_candidates(
    text_candidates: Iterable[Dict[str, Any]],
    image_candidates: Iterable[Dict[str, Any]],
    *,
    limit: int,
    text_weight: float = 0.5,
    image_weight: float = 0.5,
    rrf_k: int = 60,
) -> List[Dict[str, Any]]:
    """
    Merge two ranked candidate lists with weighted reciprocal-rank fusion.

    This keeps multimodal merge explicit and bounded without calibrating raw
    scores across heterogeneous embedding spaces. The function deduplicates by
    article_id and annotates each merged candidate with contributing sources.
    """

    normalized = _normalize_weights(text_weight, image_weight)
    merged: Dict[str, Dict[str, Any]] = {}

    def ingest(
        candidates: Iterable[Dict[str, Any]],
        *,
        source: str,
        weight: float,
    ) -> None:
        if weight <= 0.0:
            return

        for rank, candidate in enumerate(candidates, start=1):
            article_id = str(candidate.get("article_id") or "").strip()
            if not article_id:
                continue

            entry = merged.setdefault(
                article_id,
                {
                    "article_id": article_id,
                    "score": 0.0,
                    "merge_score": 0.0,
                    "candidate_sources": [],
                    "source_scores": {},
                    "source_ranks": {},
                },
            )

            if source not in entry["candidate_sources"]:
                entry["candidate_sources"].append(source)

            entry["source_scores"][source] = float(candidate.get("score", 0.0) or 0.0)
            entry["source_ranks"][source] = rank
            fused_score = weight / float(rrf_k + rank)
            entry["merge_score"] += fused_score
            entry["score"] = entry["merge_score"]

    ingest(text_candidates, source="text", weight=normalized["text"])
    ingest(image_candidates, source="image", weight=normalized["image"])

    ranked = sorted(
        merged.values(),
        key=lambda item: (
            item["merge_score"],
            -min(item["source_ranks"].values()) if item["source_ranks"] else 0,
            item["article_id"],
        ),
        reverse=True,
    )
    return ranked[: max(0, limit)]


def fuse_with_normalized_scores(
    text_candidates: Iterable[Dict[str, Any]],
    image_candidates: Iterable[Dict[str, Any]],
    *,
    limit: int,
    image_weight: float = 0.5,
    text_weight: float = 0.4,
    behavior_weight: float = 0.1,
) -> List[Dict[str, Any]]:
    """
    Merge two ranked candidate lists using per-list min-max normalization
    and weighted score fusion.

    Fusion formula:
        final_score = image_weight * norm_image_score
                    + text_weight  * norm_text_score
                    + behavior_weight * behavior_score  (always 0.0 until Phase 7)

    Article IDs are canonicalized to 10-digit zero-padded strings so that
    int 108775015 and string "0108775015" resolve to the same candidate.
    Candidates absent from one side receive a normalized score of 0.0 for that side.
    """
    iw = max(0.0, float(image_weight or 0.0))
    tw = max(0.0, float(text_weight or 0.0))
    bw = max(0.0, float(behavior_weight or 0.0))
    total = iw + tw + bw
    if total <= 0.0:
        iw, tw, bw = 0.5, 0.4, 0.1
        total = 1.0
    iw /= total
    tw /= total
    bw /= total

    def _canon(candidate: Dict[str, Any]) -> str:
        aid = str(candidate.get("article_id") or "").strip()
        return aid.zfill(10) if aid else ""

    image_raw: Dict[str, float] = {}
    text_raw: Dict[str, float] = {}

    for c in image_candidates:
        aid = _canon(c)
        if aid:
            image_raw[aid] = float(c.get("score", 0.0) or 0.0)

    for c in text_candidates:
        aid = _canon(c)
        if aid:
            text_raw[aid] = float(c.get("score", 0.0) or 0.0)

    image_norm = _minmax_normalize(image_raw)
    text_norm = _minmax_normalize(text_raw)

    results: List[Dict[str, Any]] = []
    for aid in set(image_raw) | set(text_raw):
        norm_img = image_norm.get(aid, 0.0)
        norm_txt = text_norm.get(aid, 0.0)
        behavior_score = 0.0
        final_score = iw * norm_img + tw * norm_txt + bw * behavior_score

        sources: List[str] = []
        if aid in image_raw:
            sources.append("image")
        if aid in text_raw:
            sources.append("text")

        results.append(
            {
                "article_id": aid,
                "score": final_score,
                "final_score": final_score,
                "image_score": image_raw.get(aid),
                "text_score": text_raw.get(aid),
                "behavior_score": behavior_score,
                "candidate_sources": sources,
                "merge_score": final_score,
            }
        )

    results.sort(key=lambda x: x["final_score"], reverse=True)
    return results[: max(0, limit)]


def _minmax_normalize(scores: Dict[str, float]) -> Dict[str, float]:
    """Min-max normalize a score dict to [0, 1]. All-equal scores → 1.0."""
    if not scores:
        return {}
    vals = list(scores.values())
    lo, hi = min(vals), max(vals)
    if hi == lo:
        return {k: 1.0 for k in scores}
    span = hi - lo
    return {k: (v - lo) / span for k, v in scores.items()}


def _normalize_weights(text_weight: float, image_weight: float) -> Dict[str, float]:
    text = max(0.0, float(text_weight or 0.0))
    image = max(0.0, float(image_weight or 0.0))
    total = text + image

    if total <= 0.0:
        return {"text": 0.5, "image": 0.5}

    return {"text": text / total, "image": image / total}


def apply_behavior_boost_to_hybrid_results(
    results: List[Dict[str, Any]],
    snapshot: Any,
    behavior_boost: Any,
) -> None:
    """Apply BehaviorBoost to fused hybrid results in-place.

    BehaviorBoost operates on rerank_score; hybrid results carry final_score.
    This bridges the two by temporarily aliasing final_score → rerank_score,
    running apply_boost, then propagating the boosted value back to final_score
    and score. The absolute boost delta is stored as per-item behavior_score.
    """
    if not results:
        return

    original_scores: Dict[str, float] = {
        r.get("article_id", ""): r.get("final_score", 0.0) for r in results
    }
    for r in results:
        r["rerank_score"] = r.get("final_score", 0.0)

    behavior_boost.apply_boost(snapshot, results)

    for r in results:
        boosted = r.get("rerank_score", 0.0)
        original = original_scores.get(r.get("article_id", ""), 0.0)
        r["final_score"] = boosted
        r["score"] = boosted
        r["behavior_score"] = max(0.0, boosted - original)
