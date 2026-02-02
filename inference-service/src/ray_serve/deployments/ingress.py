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
import redis
import json
import hashlib
import asyncio
from typing import Optional
from ray.serve.handle import DeploymentHandle
from fastapi import FastAPI, Response
from fastapi.responses import JSONResponse
from starlette.requests import Request
from pydantic import BaseModel, Field
from ray import serve

# OpenTelemetry tracing imports
from opentelemetry.trace.propagation.tracecontext import TraceContextTextMapPropagator
from opentelemetry.context import attach, detach
from opentelemetry.instrumentation.fastapi import FastAPIInstrumentor

# Prometheus metrics imports
from prometheus_client import Counter, Histogram, generate_latest, CONTENT_TYPE_LATEST

# Import Ray Serve deployment classes
from src.ray_serve.deployments.router import RouterDeployment
from src.ray_serve.deployments.embedding import EmbeddingDeployment
from src.ray_serve.deployments.retrieval import RetrievalDeployment
from src.ray_serve.deployments.popularity import PopularityDeployment
from src.ray_serve.deployments.reranker import RerankerDeployment
from src.ray_serve.deployments.generation import GenerationDeployment
from src.config import (
    RetrievalConfig,
    EmbeddingConfig,
    RerankerConfig,
    ABTestConfig,
)

# Initialize observability
from src.ray_serve.observability import setup_tracing

logger = logging.getLogger("scalestyle.ingress")

# Create and instrument FastAPI app for automatic trace context extraction
# This ensures Gateway -> Inference traces are properly linked in Jaeger
_app = FastAPI(title="ScaleStyle Inference Service")

# Idempotent instrumentation guard to prevent duplicate instrumentation
# when multiple replicas start or the module is reloaded
if not getattr(_app.state, "otel_instrumented", False):
    FastAPIInstrumentor.instrument_app(_app)
    _app.state.otel_instrumented = True
    logger.info("FastAPI app instrumented for OpenTelemetry trace propagation")

# Required fields that must be present in every result item for API contract compliance
CONTRACT_FIELDS = ("title", "image_url", "dept", "desc", "price", "color")

# Global metrics registry (avoids Ray serialization issues)
_METRICS_REGISTRY = {}


def _get_or_create_metric(name, metric_type, *args, **kwargs):
    """Get or create a Prometheus metric (singleton pattern to avoid Ray serialization issues)"""
    if name not in _METRICS_REGISTRY:
        if metric_type == "counter":
            _METRICS_REGISTRY[name] = Counter(*args, **kwargs)
        elif metric_type == "histogram":
            _METRICS_REGISTRY[name] = Histogram(*args, **kwargs)
    return _METRICS_REGISTRY[name]


class SearchRequest(BaseModel):
    """
    Request model for search/recommendation API.

    Attributes:
        query: Search query string (minimum 1 character).
        k: Number of results to return (1-50, default 10).
        debug: Enable debug information in response (latency breakdown, etc.).
        user_id: Optional user identifier for personalization.
        intent: Optional intent type for multi-intent support (search/similar/outfit/trend).
    """

    query: str = Field(..., min_length=1)
    k: int = Field(10, ge=1, le=50)
    debug: bool = False
    user_id: Optional[str] = None
    intent: Optional[str] = "search"  # P0-1+: Multi-intent support


def _local_image_url(article_id: str) -> str:
    """
    Generate local static image URL from article ID.

    Uses the same format as backfill_redis_contract.py for consistency.
    Generates path: /static/images/{folder}/{article_id}.jpg
    where folder is the first 3 digits of the zero-padded article ID.

    Args:
        article_id: Article identifier.

    Returns:
        str: Local static image path, or empty string if article_id is invalid.
    """
    if not article_id:
        return ""
    # Zero-pad to 10 digits for consistent formatting
    aid = str(article_id).strip().zfill(10)
    # Use first 3 digits as folder for organization
    folder = aid[:3]
    return f"/static/images/{folder}/{aid}.jpg"


def _contract_normalize(results: list, limit: int):
    """
    Normalize results to a stable contract WITHOUT querying Redis again.

    Contract meta fields:
      title / image_url / dept / desc / price / color

    Note: Only generates image_url if explicitly missing. Uses local static path
    format consistent with backfill_redis_contract.py.

    Returns:
      (normalized_results, contract_debug)
    """
    required = ["title", "image_url", "dept", "desc", "price", "color"]
    missing_by_field = {k: 0 for k in required}

    out = []
    for r in (results or [])[:limit]:
        raw = (r.get("meta") or {}) if isinstance(r, dict) else {}
        aid = str(r.get("article_id") or raw.get("article_id") or "")

        # build stable meta
        title = (
            raw.get("prod_name")
            or raw.get("title")
            or raw.get("product_type_name")
            or raw.get("product_group_name")
            or ""
        )
        # Only generate image_url if completely missing (prefer empty over invalid URL)
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

        # count missing fields
        for k in required:
            v = meta.get(k)
            if v is None or (isinstance(v, str) and v.strip() == ""):
                missing_by_field[k] += 1

        # overwrite meta to be contract-stable (so clients rely on fixed keys)
        r["article_id"] = int(aid) if aid.isdigit() else aid
        r["meta"] = meta
        out.append(r)

    missing_total = sum(missing_by_field.values())
    return out, {"missing_total": missing_total, "missing_by_field": missing_by_field}


def _normalize_meta(article_id: str, raw: dict) -> dict:
    """
    Normalize and standardize item metadata for API contract compliance.

    Extracts metadata fields from raw Redis data, generates fallback values
    for missing fields, and identifies which required contract fields are
    missing or empty.

    Args:
        article_id: Item article identifier.
        raw: Raw metadata dictionary from Redis.

    Returns:
        tuple: (normalized_meta_dict, list_of_missing_fields)
    """

    # Helper to safely get field with None-safe default handling
    def g(k: str, default: str = "") -> str:
        v = raw.get(k, default) if raw else default
        return v if v is not None else default

    # Extract metadata fields from raw data
    color = g("colour_group_name", "")
    dept = g("department_name", "")
    desc = g("detail_desc", "")
    price = g("price", "")
    product_type = g("product_type_name", "")
    prod_name = g("prod_name", "")

    # Generate title with fallback hierarchy: prod_name > product_type > color-based > generic
    title = prod_name or product_type or (f"{color} item" if color else "item")

    # Get stored image URL (prefer existing value, only generate if completely missing)
    image_url = g("image_url", "")

    if not image_url:
        # Generate local static image path consistent with backfill script
        image_url = _local_image_url(article_id)

    # Build normalized metadata dictionary with contract-compliant fields
    # Include both new (color/dept) and legacy (colour_group_name/department_name) for compatibility
    meta = {
        "article_id": article_id,
        "title": title,
        "image_url": image_url,
        "dept": dept,
        "department_name": dept,  # Legacy alias
        "desc": desc,
        "price": price,
        "color": color,
        "colour_group_name": color,  # Legacy alias
        "product_type_name": product_type,
    }

    # Identify missing or empty required contract fields
    missing = []
    for f in CONTRACT_FIELDS:
        v = meta.get(f, "")
        if v is None or (isinstance(v, str) and not v.strip()):
            missing.append(f)

    return meta, missing


def _bucket_flow(user_id: Optional[str]) -> str:
    """
    Determine A/B test bucket for user using stable hashing.

    Uses MD5 hash of user_id to consistently assign users to either "smart"
    (with reranking) or "base" (without reranking) flow. Even hash values
    get "smart", odd get "base".

    Args:
        user_id: Optional user identifier. If None, defaults to "smart".

    Returns:
        str: Either "smart" or "base" flow identifier.
    """
    if not user_id:
        return "smart"
    # Hash user_id for stable, deterministic bucketing
    h = hashlib.md5(user_id.encode("utf-8")).hexdigest()
    # Even hash -> smart flow, odd hash -> base flow
    return "smart" if (int(h, 16) % 2 == 0) else "base"


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


async def _enrich_and_filter(
    r,
    candidates,
    filters,
    k,
    limit: Optional[int] = None,
    cache_hit_metric=None,
    cache_miss_metric=None,
):
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
        cache_hit_metric: Optional Prometheus Counter for cache hits.
        cache_miss_metric: Optional Prometheus Counter for cache misses.

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
    # Make Redis execute() non-blocking with asyncio.to_thread()
    metas = await asyncio.to_thread(pipe.execute, raise_on_error=False)

    # Apply filters and build enriched results
    results = []
    for c, meta in zip(candidates, metas):
        aid = c.get("article_id")
        # Log Redis pipeline errors properly
        if isinstance(meta, Exception):
            logger.warning("redis_hgetall_failed key=item:%s err=%s", aid, meta)
            if cache_miss_metric:
                cache_miss_metric.labels(operation="metadata").inc()
            continue
        # Skip if metadata is missing
        if not meta:
            if cache_miss_metric:
                cache_miss_metric.labels(operation="metadata").inc()
            continue

        # Record cache hit
        if cache_hit_metric:
            cache_hit_metric.labels(operation="metadata").inc()

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


@serve.deployment(
    num_replicas=1,
    ray_actor_options={"num_cpus": 1},
)
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

    Exposes FastAPI endpoints via @serve.ingress(app) decorator.
    OpenTelemetry instrumentation provides automatic trace context extraction.
    """

    def __init__(
        self,
        router_handle: DeploymentHandle,
        embedding_handle: DeploymentHandle,
        retrieval_handle: DeploymentHandle,
        popularity_handle: DeploymentHandle,
        reranker_handle: DeploymentHandle,
        generation_handle: DeploymentHandle,
    ):
        """
        Initialize the ingress deployment with handles to other deployments.

        Args:
            router_handle: Handle to router deployment for intent detection.
            embedding_handle: Handle to embedding deployment for query vectorization.
            retrieval_handle: Handle to retrieval deployment for vector search.
            popularity_handle: Handle to popularity deployment for fallback recommendations.
            reranker_handle: Handle to reranker deployment for result reordering.
            generation_handle: Handle to generation deployment for recommendation explanations.
        """
        # Store handles to downstream deployments
        self.router_handle = router_handle
        self.embedding_handle = embedding_handle
        self.retrieval_handle = retrieval_handle
        self.popularity_handle = popularity_handle
        self.reranker_handle = reranker_handle
        self.generation_handle = generation_handle

        # Don't initialize Redis here - it causes serialization issues
        # Will be lazily initialized in property getter
        self._redis = None

        # Initialize OpenTelemetry tracer
        self.tracer = setup_tracing("inference-service")

    def _get_metrics(self):
        """Get metrics from global registry (avoid Ray serialization issues)"""
        return {
            "REQUEST_TOTAL": _get_or_create_metric(
                "REQUEST_TOTAL",
                "counter",
                "recommendation_requests_total",
                "Total number of recommendation requests",
                ["intent", "flow", "status"],
            ),
            "REQUEST_DURATION": _get_or_create_metric(
                "REQUEST_DURATION",
                "histogram",
                "recommendation_request_duration_seconds",
                "Recommendation request duration in seconds",
                ["intent", "flow"],
                buckets=(0.01, 0.05, 0.1, 0.25, 0.5, 1.0, 2.5, 5.0, 10.0),
            ),
            "CACHE_HIT_TOTAL": _get_or_create_metric(
                "CACHE_HIT_TOTAL",
                "counter",
                "redis_cache_hits_total",
                "Total number of Redis cache hits",
                ["operation"],
            ),
            "CACHE_MISS_TOTAL": _get_or_create_metric(
                "CACHE_MISS_TOTAL",
                "counter",
                "redis_cache_misses_total",
                "Total number of Redis cache misses",
                ["operation"],
            ),
        }

    @property
    def redis(self):
        """Lazy initialization of Redis client to avoid serialization issues"""
        if self._redis is None:
            self._redis = _redis_client()
        return self._redis

    async def __call__(self, request: Request):
        """
        Ray Serve HTTP handler - processes all incoming requests.
        Extracts trace context from HTTP headers to continue distributed trace from Gateway.
        """
        # Extract trace context from incoming HTTP headers (if present)
        # This allows continuing the trace from Gateway service
        propagator = TraceContextTextMapPropagator()
        context = propagator.extract(carrier=dict(request.headers))
        token = attach(context)

        try:
            path = request.url.path

            # Health check endpoint
            if path == "/healthz":
                return JSONResponse({"ok": True})

            # Metrics endpoint
            if path == "/metrics":
                return Response(
                    content=generate_latest(), media_type=CONTENT_TYPE_LATEST
                )

            # Readiness check endpoint
            if path == "/readyz":
                return await self._readyz_handler()

            # Main search endpoint (POST /)
            if request.method == "POST" and (path == "/" or path == "/search"):
                try:
                    body = await request.json()
                    req = SearchRequest(**body)
                    result = await self._search_impl(req)
                    return JSONResponse(result)
                except Exception as e:
                    logger.exception("Search request failed: %s", e)
                    return JSONResponse({"error": str(e)}, status_code=500)

            # Unknown path
            return JSONResponse({"error": "Not found", "path": path}, status_code=404)

        finally:
            # Detach trace context to clean up resources
            detach(token)

    async def _readyz_handler(self):
        """Readiness check for all dependencies"""

        # Check Redis connectivity
        try:
            await asyncio.to_thread(self.redis.ping)
        except Exception as e:
            return JSONResponse({"error": f"redis not ready: {e}"}, status_code=503)

        # Check embedding service readiness (critical for search)
        try:
            ready = await self.embedding_handle.is_ready.remote()
            if not ready:
                return JSONResponse({"error": "embedding not ready"}, status_code=503)
        except Exception as e:
            return JSONResponse({"error": f"embedding not ready: {e}"}, status_code=503)

        # Check Milvus/retrieval readiness (non-blocking, will use popularity fallback)
        retrieval_ready = False
        try:
            retrieval_ready = await self.retrieval_handle.is_ready.remote()
        except Exception:
            retrieval_ready = False

        return JSONResponse(
            {
                "status": "ready",
                "deps": {
                    "redis": True,
                    "embedding": True,
                    "retrieval": retrieval_ready,
                },
            }
        )

    async def _search_impl(self, req: SearchRequest):
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

        # Load configuration from centralized config
        recall_k = RetrievalConfig.RECALL_K
        enrich_limit = RerankerConfig.MAX_DOCS
        timeout_ms = RerankerConfig.TIMEOUT_MS

        # Track contract_dbg for post-generation correction
        contract_dbg_cache = {}

        # Unified return helper to ensure consistent contract normalization
        def _build_response(results, route, latency_patch=None, extra_debug=None):
            """Helper to build response with contract normalization for all branches."""
            # Always normalize results for contract compliance
            results, contract_dbg = _contract_normalize(results, limit=req.k)

            # Cache contract_dbg for potential correction after generation
            contract_dbg_cache.clear()
            contract_dbg_cache.update(contract_dbg)

            total_ms = (time.time() - t0) * 1000
            resp = {
                "query": req.query,
                "route": route,
                "results": results,
                "request_id": request_id,
            }

            if req.debug:
                # Build latency object
                latency = latency_patch or {}
                latency["total"] = total_ms
                resp["latency_ms"] = latency

                # Add pipeline configuration
                resp["pipeline"] = {
                    "flow": route.get("flow", "smart"),
                    "recall_k": recall_k,
                    "rerank_max_docs": enrich_limit,
                    "rerank_enabled": RerankerConfig.ENABLED
                    and (route.get("flow") == "smart"),
                }

                # Add contract compliance stats
                resp["contract"] = contract_dbg

                # Add any extra debug info
                if extra_debug:
                    resp.update(extra_debug)

            return resp

        # Helper to record Prometheus metrics before returning
        def _record_metrics(intent, flow, status="success"):
            """Record request metrics with consistent labels."""
            logger.debug(
                f"Metrics recorded: intent={intent} flow={flow} status={status}"
            )
            metrics = self._get_metrics()
            metrics["REQUEST_TOTAL"].labels(
                intent=intent, flow=flow, status=status
            ).inc()
            metrics["REQUEST_DURATION"].labels(intent=intent, flow=flow).observe(
                (time.time() - t0)
            )

        # Start main tracing span for entire request - ALL pipeline logic inside
        with self.tracer.start_as_current_span("search_request") as main_span:
            # Add request attributes to span
            main_span.set_attribute("query", req.query)
            main_span.set_attribute("k", req.k)
            if req.user_id:
                main_span.set_attribute("user_id", req.user_id)
            main_span.set_attribute("request_id", request_id)
            try:
                route = await self.router_handle.route.remote(req.query, req.user_id)
                main_span.set_attribute("intent", route.get("intent", "SEARCH"))
            except Exception as e:
                logger.exception("request_id=%s route_failed err=%s", request_id, e)
                # Fallback to default SEARCH intent on routing failure
                route = {"intent": "SEARCH", "filters": {}}
                main_span.set_attribute("error", True)
                main_span.set_attribute("error.message", str(e))

            # Extract intent and filters from routing result
            intent = route.get("intent", "SEARCH")
            filters = route.get("filters") or {}
            # Determine A/B test flow: smart (with reranking) or base (without reranking)
            flow = route.get("flow") or _bucket_flow(req.user_id)
            route["flow"] = flow

            # Enable reranking only if globally enabled AND user is in "smart" flow
            enable_rerank = RerankerConfig.ENABLED and (flow == "smart")

            # Step 2: Handle BROWSE intent - return popular items directly
            if intent == "BROWSE":
                results = await self.popularity_handle.topk.remote(req.k)

                logger.info(
                    "request_id=%s intent=BROWSE k=%d",
                    request_id,
                    req.k,
                )
                _record_metrics(intent="BROWSE", flow=flow, status="success")
                return _build_response(results, route)

            # Base Flow Mode - support pure popularity baseline for A/B test control group
            # When BASE_FLOW_MODE=popularity and flow=base, skip embedding/retrieval entirely
            base_flow_mode = ABTestConfig.BASE_FLOW_MODE
            if flow == "base" and base_flow_mode == "popularity":
                # Use pure popularity baseline (no vector search)
                results = await self.popularity_handle.topk.remote(req.k)
                logger.info(
                    "request_id=%s intent=SEARCH flow=base mode=popularity k=%d",
                    request_id,
                    req.k,
                )
                _record_metrics(intent="SEARCH", flow="base", status="success")
                return _build_response(
                    results,
                    route,
                    extra_debug={"base_flow_mode": "popularity"} if req.debug else None,
                )

            # Step 3: Generate query embedding for semantic search
            embed_ms = None
            embed_timeout_ms = EmbeddingConfig.TIMEOUT_MS
            with self.tracer.start_as_current_span("embed") as span:
                span.set_attribute("query", req.query)
                try:
                    t_embed0 = time.time()
                    try:
                        vector = await asyncio.wait_for(
                            self.embedding_handle.embed.remote(
                                req.query, is_query=True
                            ),
                            timeout=embed_timeout_ms / 1000.0,
                        )
                        embed_ms = (time.time() - t_embed0) * 1000
                        span.set_attribute("latency_ms", embed_ms)
                    except asyncio.TimeoutError:
                        embed_ms = float(embed_timeout_ms)
                        span.set_attribute("timeout", True)
                        span.set_attribute("latency_ms", embed_ms)
                        logger.warning(
                            "request_id=%s embed_timeout timeout_ms=%d -> fallback popularity",
                            request_id,
                            embed_timeout_ms,
                        )
                        results = await self.popularity_handle.topk.remote(req.k)
                        _record_metrics(intent=intent, flow=flow, status="fallback")
                        return _build_response(
                            results, route, latency_patch={"embed": embed_ms}
                        )
                except Exception as e:
                    # Fallback to popularity on embedding failure
                    span.set_attribute("error", True)
                    span.set_attribute("error.message", str(e))
                    logger.exception(
                        "request_id=%s embed_failed err=%s -> fallback popularity",
                        request_id,
                        e,
                    )
                    results = await self.popularity_handle.topk.remote(req.k)
                    _record_metrics(intent=intent, flow=flow, status="fallback")
                    return _build_response(
                        results, route, latency_patch={"embed": embed_ms}
                    )

            # Step 4: Retrieve candidates from vector database
            ret_ms = None
            retrieval_timeout_ms = RetrievalConfig.TIMEOUT_MS
            with self.tracer.start_as_current_span("retrieve") as span:
                span.set_attribute("candidate_k", recall_k)
                span.set_attribute("filters", json.dumps(filters))
                try:
                    t_ret0 = time.time()
                    # Use recall_k and enrich_limit already configured at function start
                    candidate_k = recall_k
                    try:
                        candidates = await asyncio.wait_for(
                            self.retrieval_handle.search.remote(
                                vector,
                                top_k=req.k,
                                candidate_k=candidate_k,
                                filters=filters,
                            ),
                            timeout=retrieval_timeout_ms / 1000.0,
                        )
                        ret_ms = (time.time() - t_ret0) * 1000
                        span.set_attribute("latency_ms", ret_ms)
                        span.set_attribute("result_count", len(candidates))
                    except asyncio.TimeoutError:
                        ret_ms = float(retrieval_timeout_ms)
                        span.set_attribute("timeout", True)
                        span.set_attribute("latency_ms", ret_ms)
                        logger.warning(
                            "request_id=%s retrieval_timeout timeout_ms=%d -> fallback popularity",
                            request_id,
                            retrieval_timeout_ms,
                        )
                        results = await self.popularity_handle.topk.remote(req.k)
                        _record_metrics(intent=intent, flow=flow, status="fallback")
                        return _build_response(
                            results,
                            route,
                            latency_patch={"embed": embed_ms, "retrieve": ret_ms},
                        )
                except Exception as e:
                    # Fallback to popularity on retrieval failure
                    span.set_attribute("error", True)
                    span.set_attribute("error.message", str(e))
                    logger.exception(
                        "request_id=%s retrieval_failed err=%s -> fallback popularity",
                        request_id,
                        e,
                    )
                    results = await self.popularity_handle.topk.remote(req.k)
                    _record_metrics(intent=intent, flow=flow, status="fallback")
                    return _build_response(
                        results,
                        route,
                        latency_patch={"embed": embed_ms, "retrieve": ret_ms},
                    )

            # Step 5: Enrich candidates with metadata and apply post-retrieval filters
            enrich_ms = None
            rerank_ms = None
            rerank_mode = None
            rerank_effect = None  # Initialize rerank_effect

            try:
                with self.tracer.start_as_current_span("enrich") as span:
                    try:
                        t_enrich0 = time.time()
                        # Limit enrichment to top N candidates to control reranking cost (use value from function start)
                        # Filter and enrich candidates with metadata (async to avoid blocking)
                        metrics = self._get_metrics()
                        results = await _enrich_and_filter(
                            self.redis,
                            candidates,
                            filters,
                            req.k,
                            limit=enrich_limit,
                            cache_hit_metric=metrics["CACHE_HIT_TOTAL"],
                            cache_miss_metric=metrics["CACHE_MISS_TOTAL"],
                        )
                        enrich_ms = (time.time() - t_enrich0) * 1000
                        span.set_attribute("latency_ms", enrich_ms)
                        span.set_attribute("result_count", len(results))
                    except Exception as e_enrich:
                        span.set_attribute("error", True)
                        span.set_attribute("error.message", str(e_enrich))
                        raise

                # Fallback to popularity if all candidates filtered out
                # Initialize generation_ms to avoid undefined in fallback path
                generation_ms = None

                if not results:
                    results = await self.popularity_handle.topk.remote(req.k)
                else:
                    # Step 6: Rerank results for improved semantic relevance (if enabled for user's flow)
                    rerank_effect = None
                    if enable_rerank:
                        with self.tracer.start_as_current_span("rerank") as span:
                            span.set_attribute("enabled", True)
                            span.set_attribute("doc_count", len(results))
                            try:
                                docs = [
                                    _build_rerank_doc(r.get("meta", {}))
                                    for r in results
                                ]
                                t_rr0 = time.time()

                                # Capture order before reranking for comparison
                                before_ids = [r.get("article_id") for r in results]

                                try:
                                    info = await asyncio.wait_for(
                                        self.reranker_handle.score.remote(
                                            req.query, docs
                                        ),
                                        timeout=timeout_ms / 1000.0,
                                    )
                                except asyncio.TimeoutError:
                                    rerank_mode = "timeout"
                                    rerank_ms = float(timeout_ms)
                                    span.set_attribute("timeout", True)
                                    span.set_attribute("latency_ms", rerank_ms)
                                    logger.warning(
                                        "request_id=%s rerank_timeout timeout_ms=%d -> skip rerank",
                                        request_id,
                                        timeout_ms,
                                    )
                                    info = None

                                if info:
                                    scores = info.get("scores", [])
                                    rerank_ms = info.get(
                                        "rerank_ms", (time.time() - t_rr0) * 1000
                                    )
                                    rerank_mode = info.get("mode", rerank_mode)
                                    span.set_attribute("latency_ms", rerank_ms)
                                    span.set_attribute("mode", rerank_mode)

                                    for i, r in enumerate(results):
                                        r["rerank_score"] = (
                                            float(scores[i])
                                            if i < len(scores)
                                            else -1e9
                                        )

                                    results.sort(
                                        key=lambda x: x.get("rerank_score", -1e9),
                                        reverse=True,
                                    )

                                    # Capture order after reranking
                                    after_ids = [r.get("article_id") for r in results]

                                    # Calculate rerank effect
                                    top_k_compare = min(
                                        len(before_ids), len(after_ids), req.k
                                    )
                                    changed_positions = sum(
                                        1
                                        for i in range(top_k_compare)
                                        if before_ids[i] != after_ids[i]
                                    )
                                    top1_changed = (
                                        before_ids[0] != after_ids[0]
                                        if before_ids and after_ids
                                        else False
                                    )

                                    rerank_effect = {
                                        "changed_positions": changed_positions,
                                        "top1_changed": top1_changed,
                                        "total_compared": top_k_compare,
                                    }
                                    span.set_attribute(
                                        "changed_positions", changed_positions
                                    )
                                    span.set_attribute("top1_changed", top1_changed)

                                    # Log rerank changes for debugging and milestone verification
                                    if req.debug:
                                        logger.info(
                                            "request_id=%s RERANK_EFFECT: changed=%d/%d top1_changed=%s "
                                            "before_top5=%s after_top5=%s",
                                            request_id,
                                            changed_positions,
                                            top_k_compare,
                                            top1_changed,
                                            before_ids[:5],
                                            after_ids[:5],
                                        )
                                        # Detailed score comparison for top 3
                                        for i in range(min(3, len(before_ids))):
                                            logger.info(
                                                "request_id=%s RERANK_DETAIL rank=%d: "
                                                "article_id=%s vector_score=%.4f rerank_score=%.4f",
                                                request_id,
                                                i + 1,
                                                after_ids[i],
                                                results[i].get("score", 0),
                                                results[i].get("rerank_score", 0),
                                            )

                            except Exception as e:
                                span.set_attribute("error", True)
                                span.set_attribute("error.message", str(e))
                                logger.exception(
                                    "request_id=%s rerank_failed err=%s -> continue without rerank",
                                    request_id,
                                    e,
                                )
                    else:
                        # Reranking disabled for this user's flow
                        rerank_mode = "off"

                    # Trim to requested number of results
                    results = results[: req.k]

                # Step 7: Generate recommendation reason for Top-1 item (if generation enabled)
                # Week 3: Only Top-1 with 500ms timeout and reason_source field
                generation_enabled = os.getenv("GENERATION_ENABLED", "0") == "1"
                generation_flow = os.getenv("GENERATION_FLOW", "smart")

                if results and self.generation_handle and generation_enabled:
                    # Check if should generate based on flow mode
                    should_generate = (
                        generation_flow == "all"
                        or (generation_flow == "search" and intent == "SEARCH")
                        or (generation_flow == "smart" and flow == "smart")
                    )

                    if should_generate:
                        with self.tracer.start_as_current_span(
                            "llm.generate_reason"
                        ) as span:
                            span.set_attribute("enabled", True)
                            span.set_attribute("query_len", len(req.query))
                            span.set_attribute("topk", req.k)
                            span.set_attribute(
                                "model", os.getenv("GENERATION_MODEL", "qwen2.5")
                            )
                            span.set_attribute(
                                "article_id", results[0].get("article_id", "")
                            )
                            try:
                                t_gen0 = time.time()
                                # Week 3: Reduced timeout to 500ms (was 600ms)
                                timeout = (
                                    float(os.getenv("GENERATION_TIMEOUT_MS", "500"))
                                    / 1000.0
                                )
                                span.set_attribute("timeout_ms", timeout * 1000)

                                out = await asyncio.wait_for(
                                    self.generation_handle.explain.remote(
                                        req.query, results[0]
                                    ),
                                    timeout=timeout,
                                )
                                generation_ms = (time.time() - t_gen0) * 1000
                                reason_value = out.get("reason", "")
                                mode = out.get(
                                    "mode", "unknown"
                                )  # Extract actual mode (template/llm)

                                # Week 3: Add reason and reason_source at root level
                                # Use actual mode instead of hardcoding "llm"
                                results[0]["reason"] = reason_value
                                results[0]["reason_source"] = (
                                    mode if reason_value else "fallback"
                                )

                                # Also keep in meta for backward compatibility
                                results[0].setdefault("meta", {})[
                                    "reason"
                                ] = reason_value

                                span.set_attribute("latency_ms", generation_ms)
                                span.set_attribute("mode", mode)
                                span.set_attribute("fallback", not bool(reason_value))

                                # Fix: Correct contract_dbg if reason was generated
                                if reason_value and contract_dbg_cache:
                                    missing_by_field = contract_dbg_cache.get(
                                        "missing_by_field", {}
                                    )
                                    if missing_by_field.get("reason", 0) > 0:
                                        missing_by_field["reason"] = 0
                                        contract_dbg_cache["missing_total"] = sum(
                                            missing_by_field.values()
                                        )

                                logger.info(
                                    "request_id=%s generation_success gen_ms=%.2f mode=%s reason_source=%s",
                                    request_id,
                                    generation_ms,
                                    out.get("mode", "unknown"),
                                    "llm" if reason_value else "fallback",
                                )
                            except asyncio.TimeoutError:
                                generation_ms = timeout * 1000
                                # Week 3: Add reason_source=fallback on timeout
                                results[0]["reason"] = ""
                                results[0]["reason_source"] = "fallback"
                                results[0].setdefault("meta", {})["reason"] = ""
                                span.set_attribute("timeout", True)
                                span.set_attribute("fallback", True)
                                span.set_attribute("latency_ms", generation_ms)
                                logger.warning(
                                    "request_id=%s generation_timeout timeout_ms=%.2f reason_source=fallback",
                                    request_id,
                                    timeout * 1000,
                                )
                            except Exception as e:
                                generation_ms = (
                                    (time.time() - t_gen0) * 1000
                                    if "t_gen0" in locals()
                                    else 0
                                )
                                # Week 3: Add reason_source=fallback on error
                                results[0]["reason"] = ""
                                results[0]["reason_source"] = "fallback"
                                results[0].setdefault("meta", {})["reason"] = ""
                                span.set_attribute("error", True)
                                span.set_attribute("fallback", True)
                                span.set_attribute("error.message", str(e))
                                logger.exception(
                                    "request_id=%s generation_failed err=%s",
                                    request_id,
                                    e,
                                )
            except Exception as e:
                # Fallback to popularity on enrichment/filtering failure (P0-2: also needs contract normalize)
                main_span.set_attribute("error", True)
                main_span.set_attribute("error.message", str(e))
                logger.exception(
                    "request_id=%s enrich_filter_failed err=%s -> fallback popularity",
                    request_id,
                    e,
                )
                results = await self.popularity_handle.topk.remote(req.k)
                metrics = self._get_metrics()
                metrics["REQUEST_TOTAL"].labels(
                    intent=intent, flow=flow, status="fallback"
                ).inc()
                return _build_response(
                    results,
                    route,
                    latency_patch={
                        "embed": embed_ms,
                        "retrieve": ret_ms,
                        "enrich": enrich_ms,
                        "rerank": rerank_ms,
                    },
                    extra_debug=(
                        {"rerank": {"mode": rerank_mode}} if rerank_mode else None
                    ),
                )

            # Build final response using unified helper (P0-1: ensures contract_dbg is always defined)
            rerank_debug = {"mode": rerank_mode} if rerank_mode else None
            if rerank_effect:
                if rerank_debug is None:
                    rerank_debug = {}
                rerank_debug["effect"] = rerank_effect

            # Build debug info for generation
            generation_debug = None
            if generation_ms is not None:
                generation_enabled = os.getenv("GENERATION_ENABLED", "0") == "1"
                generation_flow = os.getenv("GENERATION_FLOW", "smart")
                # Note: mode is logged but not exposed in debug for simplicity
                generation_debug = {
                    "enabled": generation_enabled,
                    "flow": generation_flow,
                    "latency_ms": round(generation_ms, 2),
                }

            # Aggregate all debug information
            extra_debug = {}
            if rerank_debug:
                extra_debug["rerank"] = rerank_debug
            if generation_debug:
                extra_debug["generation"] = generation_debug

            resp = _build_response(
                results,
                route,
                latency_patch={
                    "embed": embed_ms,
                    "retrieve": ret_ms,
                    "enrich": enrich_ms,
                    "rerank": rerank_ms,
                    "generation": generation_ms,
                },
                extra_debug=extra_debug if extra_debug else None,
            )

            # Log request metrics for monitoring and analysis
            total_ms = resp.get("latency_ms", {}).get("total")
            logger.info(
                "request_id=%s intent=SEARCH k=%d embed_ms=%.2f ret_ms=%.2f gen_ms=%.2f total_ms=%.2f filters=%s",
                request_id,
                req.k,
                embed_ms or -1,
                ret_ms or -1,
                generation_ms or -1,
                total_ms or -1,
                json.dumps(filters, ensure_ascii=False),
            )

            # Update span with final attributes
            main_span.set_attribute("intent", intent)
            main_span.set_attribute("flow", flow)
            main_span.set_attribute("result_count", len(results))
            main_span.set_attribute("total_latency_ms", total_ms or 0)
            if embed_ms:
                main_span.set_attribute("embed_latency_ms", embed_ms)
            if ret_ms:
                main_span.set_attribute("retrieve_latency_ms", ret_ms)
            if rerank_ms:
                main_span.set_attribute("rerank_latency_ms", rerank_ms)
            if generation_ms:
                main_span.set_attribute("generation_latency_ms", generation_ms)

            # Record Prometheus metrics
            metrics = self._get_metrics()
            metrics["REQUEST_TOTAL"].labels(
                intent=intent, flow=flow, status="success"
            ).inc()
            metrics["REQUEST_DURATION"].labels(intent=intent, flow=flow).observe(
                (time.time() - t0)
            )

            return resp


# Bind individual deployment nodes for the Ray Serve application graph
router_node = RouterDeployment.bind()
embedding_node = EmbeddingDeployment.bind()
retrieval_node = RetrievalDeployment.bind()
popularity_node = PopularityDeployment.bind()
reranker_node = RerankerDeployment.bind()
generation_node = GenerationDeployment.bind()

# Bind ingress deployment with all dependency handles
# This creates the complete application graph for Ray Serve
ingress_app = IngressDeployment.bind(
    router_node,
    embedding_node,
    retrieval_node,
    popularity_node,
    reranker_node,
    generation_node,
)
