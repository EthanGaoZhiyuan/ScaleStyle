"""
Ingress deployment for ScaleStyle recommendation service.

This module provides the main API endpoint for handling search/recommendation requests.
It orchestrates multiple deployments (router, embedding, retrieval, popularity, reranker)
to deliver personalized and semantically relevant product recommendations.
"""

import os
import time
import uuid
import logging
from typing import Optional

import redis
from fastapi import FastAPI, HTTPException
from pydantic import BaseModel, Field
from ray import serve
from ray.serve.handle import DeploymentHandle
import json

# Import Ray Serve deployment classes
from src.ray_serve.deployments.router import RouterDeployment
from src.ray_serve.deployments.embedding import EmbeddingDeployment
from src.ray_serve.deployments.retrieval import RetrievalDeployment
from src.ray_serve.deployments.popularity import PopularityDeployment
from src.ray_serve.deployments.reranker import RerankerDeployment

logger = logging.getLogger("scalestyle.ingress")
# FastAPI application for HTTP endpoints
app = FastAPI()


class SearchRequest(BaseModel):
    """
    Request model for search/recommendation API.

    Attributes:
        query: Search query string (minimum 1 character).
        k: Number of results to return (1-50, default 10).
        debug: Enable debug information in response (latency breakdown, etc.).
        user_id: Optional user identifier for personalization.
    """

    query: str = Field(..., min_length=1)
    k: int = Field(10, ge=1, le=50)
    debug: bool = False
    user_id: Optional[str] = None


def _redis_client() -> redis.Redis:
    """
    Create and return a Redis client connection.

    Reads connection parameters from environment variables with fallback defaults.

    Returns:
        redis.Redis: Configured Redis client with string decoding enabled.
    """
    host = os.getenv("REDIS_HOST", "localhost")
    port = int(os.getenv("REDIS_PORT", "6379"))
    return redis.Redis(host=host, port=port, decode_responses=True)


def _enrich_and_filter(r, candidates, filters, k, limit: Optional[int] = None):
    """
    Enrich candidates with metadata from Redis and apply post-retrieval filters.

    Fetches item metadata from Redis and applies filters (color, price) that may not
    have been fully applied during vector search. Uses Redis pipeline for efficiency.

    Args:
        r: Redis client instance.
        candidates: List of candidate items from retrieval (with article_id and score).
        filters: Dictionary of filter criteria (colour_group_name, price_lt).
        k: Target number of results (not strictly enforced, see limit).
        limit: Optional hard limit on number of results to return.

    Returns:
        List[Dict]: Filtered and enriched results with article_id, score, and metadata.
    """
    # Extract filter criteria
    colour = filters.get("colour_group_name")
    price_lt = filters.get("price_lt")

    # Collect all article IDs from candidates
    ids = []
    for c in candidates:
        aid = c.get("article_id")
        if aid is not None:
            ids.append(str(aid))

    # Batch fetch metadata from Redis using pipeline for efficiency
    # Returns all fields and values of the hash stored at key.
    # In the returned value, every field name is followed by its value,
    # so the length of the reply is twice the size of the hash.
    pipe = r.pipeline()
    for aid in ids:
        pipe.hgetall(f"item:{aid}")
    metas = pipe.execute()

    # Apply filters and build enriched results
    results = []
    for c, meta in zip(candidates, metas):
        aid = c.get("article_id")
        # Skip if metadata is missing
        if not meta:
            continue

        # Apply color filter if specified
        if colour and meta.get("colour_group_name") != colour:
            continue
        # Apply price filter if specified
        if price_lt is not None:
            try:
                if float(meta.get("price", "inf")) >= float(price_lt):
                    continue
            except Exception:
                pass

        # Add filtered and enriched result
        results.append({"article_id": aid, "score": c.get("score"), "meta": meta})
        # Stop if we've reached the limit
        if limit is not None and len(results) >= limit:
            break

    return results


def _build_rerank_doc(meta: dict) -> str:
    """
    Build a text document from item metadata for reranking.

    Concatenates relevant metadata fields into a single text string that can be
    used by the reranker to compute semantic relevance to the query.

    Args:
        meta: Dictionary containing item metadata fields.

    Returns:
        str: Concatenated text document with fields separated by " | ".
    """
    parts = [
        meta.get("prod_name") or meta.get("product_type_name") or "",
        meta.get("product_group_name") or "",
        meta.get("department_name") or "",
        meta.get("colour_group_name") or "",
        meta.get("detail_desc") or "",
    ]
    return " | ".join([p for p in parts if p])


@serve.deployment
@serve.ingress(app)
class IngressDeployment:
    """
    Main ingress deployment for handling recommendation requests.

    Orchestrates the full recommendation pipeline:
    1. Route intent detection (SEARCH vs BROWSE)
    2. Query embedding generation
    3. Vector similarity retrieval from Milvus
    4. Metadata enrichment and filtering from Redis
    5. Reranking for improved relevance
    6. Fallback to popularity-based recommendations on failures

    Exposes FastAPI endpoints for health checks and search.
    """

    def __init__(
        self,
        router_handle: DeploymentHandle,
        embedding_handle: DeploymentHandle,
        retrieval_handle: DeploymentHandle,
        popularity_handle: DeploymentHandle,
        reranker_handle: DeploymentHandle,
    ):
        """
        Initialize the ingress deployment with handles to other deployments.

        Args:
            router_handle: Handle to router deployment for intent detection.
            embedding_handle: Handle to embedding deployment for query vectorization.
            retrieval_handle: Handle to retrieval deployment for vector search.
            popularity_handle: Handle to popularity deployment for fallback recommendations.
            reranker_handle: Handle to reranker deployment for result reordering.
        """
        # Store handles to downstream deployments
        self.router_handle = router_handle
        self.embedding_handle = embedding_handle
        self.retrieval_handle = retrieval_handle
        self.popularity_handle = popularity_handle
        self.reranker_handle = reranker_handle

        # Initialize Redis client for metadata access
        self.redis = _redis_client()

    @app.get("/healthz")
    async def healthz(self):
        """
        Basic health check endpoint.

        Returns:
            dict: Simple ok status (always returns True if service is running).
        """
        return {"ok": True}

    @app.get("/readyz")
    async def readyz(self):
        """
        Comprehensive readiness check for all critical dependencies.

        Checks Redis, embedding service, and retrieval service (Milvus) readiness.
        Returns 503 if any critical dependency is not ready.

        Returns:
            dict: Readiness status for each dependency.

        Raises:
            HTTPException: If Redis or embedding service is not ready (503).
        """
        # Check Redis connectivity
        try:
            self.redis.ping()
        except Exception as e:
            raise HTTPException(status_code=503, detail=f"redis not ready: {e}")

        # Check embedding service readiness (critical for search)
        try:
            ready = await self.embedding_handle.is_ready.remote()
            if not ready:
                raise HTTPException(status_code=503, detail="embedding not ready")
        except HTTPException:
            raise
        except Exception as e:
            raise HTTPException(status_code=503, detail=f"embedding not ready: {e}")

        # Check Milvus/retrieval readiness (non-blocking, will use popularity fallback)
        retrieval_ready = False
        try:
            retrieval_ready = await self.retrieval_handle.is_ready.remote()
        except Exception:
            retrieval_ready = False

        return {
            "status": "ready",
            "deps": {
                "redis": True,
                "embedding": True,
                "retrieval": retrieval_ready,
            },
        }

    @app.post("/search")
    async def search(self, req: SearchRequest):
        """
        Main search/recommendation endpoint.

        Implements a multi-stage recommendation pipeline:
        1. Intent routing (BROWSE vs SEARCH)
        2. Query embedding (if SEARCH)
        3. Vector retrieval from Milvus
        4. Metadata enrichment and filtering
        5. Semantic reranking

        Includes graceful degradation with popularity-based fallbacks at each stage.

        Args:
            req: SearchRequest with query, k (result count), and optional user_id.

        Returns:
            dict: Search results with query, route info, results, and optional debug info.
        """
        # Generate unique request ID for tracing and logging
        request_id = str(uuid.uuid4())
        t0 = time.time()

        # Step 1: Route the query to determine intent and extract filters
        try:
            route = await self.router_handle.route.remote(req.query)
        except Exception as e:
            logger.exception("request_id=%s route_failed err=%s", request_id, e)
            # Fallback to default SEARCH intent on routing failure
            route = {"intent": "SEARCH", "filters": {}}

        # Extract intent and filters from routing result
        intent = route.get("intent", "SEARCH")
        filters = route.get("filters") or {}

        # Step 2: Handle BROWSE intent - return popular items directly
        if intent == "BROWSE":
            results = await self.popularity_handle.topk.remote(req.k)
            total_ms = (time.time() - t0) * 1000
            resp = {
                "query": req.query,
                "route": route,
                "results": results,
                "request_id": request_id,
            }
            if req.debug:
                resp["latency_ms"] = {"total": total_ms}
            logger.info(
                "request_id=%s intent=BROWSE k=%d total_ms=%.2f",
                request_id,
                req.k,
                total_ms,
            )
            return resp

        # Step 3: Generate query embedding for semantic search
        embed_ms = None
        try:
            t_embed0 = time.time()
            vector = await self.embedding_handle.embed.remote(req.query, is_query=True)
            embed_ms = (time.time() - t_embed0) * 1000
        except Exception as e:
            # Fallback to popularity on embedding failure
            logger.exception(
                "request_id=%s embed_failed err=%s -> fallback popularity",
                request_id,
                e,
            )
            results = await self.popularity_handle.topk.remote(req.k)
            total_ms = (time.time() - t0) * 1000
            resp = {
                "query": req.query,
                "route": route,
                "results": results,
                "request_id": request_id,
            }
            if req.debug:
                resp["latency_ms"] = {"embed": embed_ms, "total": total_ms}
            return resp

        # Step 4: Retrieve candidates from Milvus using vector similarity
        ret_ms = None
        try:
            t_ret0 = time.time()
            # Increase candidate pool when filters are present (some will be filtered out)
            candidate_k = 500 if filters else 200
            candidates = await self.retrieval_handle.search.remote(
                vector,
                top_k=req.k,
                candidate_k=candidate_k,
                filters=filters,
            )
            ret_ms = (time.time() - t_ret0) * 1000
        except Exception as e:
            # Fallback to popularity on retrieval failure
            logger.exception(
                "request_id=%s retrieval_failed err=%s -> fallback popularity",
                request_id,
                e,
            )
            results = await self.popularity_handle.topk.remote(req.k)
            total_ms = (time.time() - t0) * 1000
            resp = {
                "query": req.query,
                "route": route,
                "results": results,
                "request_id": request_id,
            }
            if req.debug:
                resp["latency_ms"] = {
                    "embed": embed_ms,
                    "retrieve": ret_ms,
                    "total": total_ms,
                }
            return resp

        # Step 5: Enrich candidates with metadata and apply post-retrieval filters
        enrich_ms = None
        rerank_ms = None
        rerank_mode = None
        try:
            t_enrich0 = time.time()
            # Limit enrichment to top N candidates to control reranking cost
            enrich_limit = int(os.getenv("RERANK_MAX_DOCS", "100"))
            results = _enrich_and_filter(
                self.redis, candidates, filters, req.k, limit=enrich_limit
            )
            enrich_ms = (time.time() - t_enrich0) * 1000

            # Fallback to popularity if all candidates filtered out
            if not results:
                results = await self.popularity_handle.topk.remote(req.k)
            else:
                # Step 6: Rerank results for improved semantic relevance
                try:
                    # Build text documents from metadata for reranking
                    docs = [_build_rerank_doc(r.get("meta", {})) for r in results]
                    info = await self.reranker_handle.score.remote(req.query, docs)
                    scores = info.get("scores", [])
                    rerank_ms = info.get("rerank_ms")
                    rerank_mode = info.get("mode")

                    # Attach rerank scores to results
                    for i, r in enumerate(results):
                        r["rerank_score"] = (
                            float(scores[i]) if i < len(scores) else -1e9
                        )

                    # Sort by rerank score (higher is better)
                    results.sort(
                        key=lambda x: x.get("rerank_score", -1e9), reverse=True
                    )
                except Exception as e:
                    # Continue without reranking on failure (use retrieval order)
                    logger.exception(
                        "request_id=%s rerank_failed err=%s -> continue without rerank",
                        request_id,
                        e,
                    )

                # Trim to requested number of results
                results = results[: req.k]
        except Exception as e:
            # Fallback to popularity on enrichment/filtering failure
            logger.exception(
                "request_id=%s enrich_filter_failed err=%s -> fallback popularity",
                request_id,
                e,
            )
            results = await self.popularity_handle.topk.remote(req.k)

        # Build final response with timing information
        total_ms = (time.time() - t0) * 1000
        resp = {
            "query": req.query,
            "route": route,
            "results": results,
            "request_id": request_id,
        }
        # Include detailed latency breakdown if debug mode enabled
        if req.debug:
            resp["latency_ms"] = {
                "embed": embed_ms,
                "retrieve": ret_ms,
                "enrich": enrich_ms,
                "rerank": rerank_ms,
                "total": total_ms,
            }
            resp["rerank"] = {"mode": rerank_mode}

        # Log request metrics for monitoring and analysis
        logger.info(
            "request_id=%s intent=SEARCH k=%d embed_ms=%.2f ret_ms=%.2f total_ms=%.2f filters=%s",
            request_id,
            req.k,
            embed_ms or -1,
            ret_ms or -1,
            total_ms,
            json.dumps(filters, ensure_ascii=False),
        )
        return resp


# Bind individual deployment nodes for the Ray Serve application graph
router_node = RouterDeployment.bind()
embedding_node = EmbeddingDeployment.bind()
retrieval_node = RetrievalDeployment.bind()
popularity_node = PopularityDeployment.bind()
reranker_node = RerankerDeployment.bind()

# Bind ingress deployment with all dependency handles
# This creates the complete application graph for Ray Serve
ingress_app = IngressDeployment.bind(
    router_node, embedding_node, retrieval_node, popularity_node, reranker_node
)
