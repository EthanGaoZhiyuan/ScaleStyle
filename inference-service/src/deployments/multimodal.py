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


def _normalize_weights(text_weight: float, image_weight: float) -> Dict[str, float]:
    text = max(0.0, float(text_weight or 0.0))
    image = max(0.0, float(image_weight or 0.0))
    total = text + image

    if total <= 0.0:
        return {"text": 0.5, "image": 0.5}

    return {"text": text / total, "image": image / total}
