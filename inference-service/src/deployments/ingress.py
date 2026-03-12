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
import threading
import json
import hashlib
import asyncio
from typing import Optional
from ray.serve.handle import DeploymentHandle
from fastapi import FastAPI, Response
from fastapi.responses import JSONResponse
from starlette.requests import Request
from pydantic import BaseModel, Field, ValidationError
from ray import serve

# OpenTelemetry tracing imports
from opentelemetry.trace.propagation.tracecontext import TraceContextTextMapPropagator
from opentelemetry.context import attach, detach
from opentelemetry import trace as otel_trace
from opentelemetry.instrumentation.fastapi import FastAPIInstrumentor

# Import Ray Serve deployment classes
from src.deployments.router import RouterDeployment
from src.deployments.embedding import EmbeddingDeployment
from src.deployments.retrieval import RetrievalDeployment
from src.deployments.popularity import PopularityDeployment
from src.deployments.reranker import RerankerDeployment
from src.deployments.generation import GenerationDeployment
from src.deployments.multimodal import merge_ranked_candidates

from src.config import (
    RetrievalConfig,
    EmbeddingConfig,
    RerankerConfig,
    ABTestConfig,
    PersonalizationConfig,
)
from src.degradation import DegradationReason

# Initialize observability
from src.utils.observability import setup_tracing
from src.utils.bucketing import bucket_user
from src.utils.metrics import counter, generate_latest_metrics, histogram, metrics_content_type

# Personalization module
from src.personalization import FeatureReader, BehaviorBoost, NullFeatureReader
from src.personalization.metrics import (
    personalization_fallback_total,
    personalization_fallback_active,
    personalization_request_mode_total,
)
from src.services.search_result_service import (
    normalize_meta as _normalize_meta,
    enrich_and_filter as _enrich_and_filter,
    build_rerank_doc as _build_rerank_doc,
)
from src.utils.redis_client import RedisClient

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

def _current_trace_id() -> str:
    span_context = otel_trace.get_current_span().get_span_context()
    return span_context.trace_id.to_bytes(16, "big").hex() if span_context.is_valid else "trace-unavailable"


def _record_request_degraded(metrics: dict, reason: DegradationReason) -> None:
    metrics["REQUEST_DEGRADED_TOTAL"].labels(reason=reason.value).inc()


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
    intent: Optional[str] = "search"


from src.utils.contract import _contract_normalize, _local_image_url
from src.utils.redis_metadata import canonical_article_id, item_key


@serve.deployment(
    # EKS keeps a fixed pod count for inference and Ray Serve owns only in-pod actor scaling.
    # Declared per-pod Ray CPU budget after INFRA-01:
    # ingress 1-3 x 0.25 + embedding 0.5 + retrieval 0.25 + reranker 0.25
    # + router/popularity/generation 0.15 + optional vision 0.1 = 1.5 min / 2.0 max.
    autoscaling_config={
        "min_replicas": 1,
        "max_replicas": 3,
        "target_num_ongoing_requests_per_replica": 10,
    },
    ray_actor_options={"num_cpus": 0.25},
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
        vision_handle: Optional[DeploymentHandle] = None,
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
            vision_handle: Optional handle to vision deployment for multimodal search.
        """
        # Store handles to downstream deployments
        self.router_handle = router_handle
        self.embedding_handle = embedding_handle
        self.retrieval_handle = retrieval_handle
        self.popularity_handle = popularity_handle
        self.reranker_handle = reranker_handle
        self.generation_handle = generation_handle
        self.vision_handle = vision_handle

        # Initialize Redis client eagerly (Ray Serve handles actor initialization correctly)
        # This prevents concurrent async initialization race conditions in the redis property
        self.redis = RedisClient.get_client()
        
        # Personalization modules (lazy init to avoid serialization issues)
        self._feature_reader = None
        self._behavior_boost = None
        self._feature_reader_last_init_failed_at: Optional[float] = None
        self._feature_reader_retry_interval_sec = float(
            os.getenv("PERSONALIZATION_INIT_RETRY_INTERVAL_SEC", "30")
        )
        self._feature_reader_lock = threading.RLock()
        self._feature_reader_probe_thread = None
        self._probe_stop_event = threading.Event()  # Signal probe thread to stop after recovery
        personalization_fallback_active.set(0)

        # Initialize OpenTelemetry tracer
        self.tracer = setup_tracing("inference-service")

        # Initialize metrics once to avoid repeated dict construction + registry lookups
        self._metrics = {
            "REQUEST_TOTAL": counter(
                "recommendation_requests_total",
                "Total number of recommendation requests",
                ["intent", "flow", "status"],
            ),
            "REQUEST_DURATION": histogram(
                "recommendation_request_duration_seconds",
                "Recommendation request duration in seconds",
                ["intent", "flow"],
                buckets=(0.01, 0.05, 0.1, 0.25, 0.5, 1.0, 2.5, 5.0, 10.0),
            ),
            "REQUEST_PHASE_DURATION": histogram(
                "recommendation_request_phase_duration_seconds",
                "Recommendation request phase duration in seconds",
                ["phase", "outcome"],
                buckets=(0.001, 0.0025, 0.005, 0.01, 0.02, 0.05, 0.1, 0.25, 0.5, 1.0),
            ),
            "REQUEST_DEGRADED_TOTAL": counter(
                "recommendation_request_degraded_total",
                "Total degraded or fallback recommendation responses by reason",
                ["reason"],
            ),
            "CACHE_HIT_TOTAL": counter(
                "redis_cache_hits_total",
                "Total number of Redis cache hits",
                ["operation"],
            ),
            "CACHE_MISS_TOTAL": counter(
                "redis_cache_misses_total",
                "Total number of Redis cache misses",
                ["operation"],
            ),
        }

    def _get_metrics(self):
        """Get metrics from global registry (avoid Ray serialization issues)"""
        return self._metrics

    def _record_phase_metric(self, phase: str, started_at: float, outcome: str = "success") -> float:
        elapsed = time.perf_counter() - started_at
        self._get_metrics()["REQUEST_PHASE_DURATION"].labels(phase=phase, outcome=outcome).observe(elapsed)
        return elapsed * 1000.0

    @property
    def feature_reader(self):
        """Return current personalization reader without triggering recovery work."""
        if self._feature_reader is None:
            self._initialize_feature_reader()
        return self._feature_reader

    def _initialize_feature_reader(self) -> None:
        """Perform first-time personalization initialization on demand."""
        with self._feature_reader_lock:
            if self._feature_reader is not None:
                return
            self._feature_reader = self._build_feature_reader_locked()

    def _build_feature_reader_locked(self):
        """Construct the current reader under lock; returns real or null reader."""
        try:
            feature_reader = FeatureReader(self.redis)
            self._feature_reader_last_init_failed_at = None
            personalization_fallback_active.set(0)
            return feature_reader
        except Exception as e:
            logger.warning(
                "personalization_init_failed err=%s -> using NullFeatureReader",
                e,
            )
            personalization_fallback_total.labels(reason=DegradationReason.PERSONALIZATION_UNAVAILABLE.value).inc()
            self._feature_reader_last_init_failed_at = time.time()
            personalization_fallback_active.set(1)
            self._ensure_feature_reader_probe_thread_started()
            return NullFeatureReader()

    def _ensure_feature_reader_probe_thread_started(self) -> None:
        """Start background recovery probe after personalization init failure."""
        if self._feature_reader_probe_thread is not None:
            return
        with self._feature_reader_lock:
            if self._feature_reader_probe_thread is not None:
                return
            probe_thread = threading.Thread(
                target=self._feature_reader_probe_loop,
                name="personalization-feature-reader-probe",
                daemon=True,
            )
            probe_thread.start()
            self._feature_reader_probe_thread = probe_thread

    def _feature_reader_probe_loop(self) -> None:
        """Retry failed personalization initialization off the request hot path."""
        while not self._probe_stop_event.wait(timeout=1.0):
            if self._recover_feature_reader_if_due():
                # Recovery succeeded - thread can exit
                logger.info("personalization_probe_thread_exiting reason=recovery_successful")
                with self._feature_reader_lock:
                    self._feature_reader_probe_thread = None
                break

    def _recover_feature_reader_if_due(self) -> bool:
        """Best-effort background recovery for a previously failed feature reader."""
        with self._feature_reader_lock:
            if not isinstance(self._feature_reader, NullFeatureReader):
                return False
            if self._feature_reader_last_init_failed_at is None:
                return False
            elapsed = time.time() - self._feature_reader_last_init_failed_at
            if elapsed < self._feature_reader_retry_interval_sec:
                return False
            recovered_reader = self._build_feature_reader_locked()
            self._feature_reader = recovered_reader
            return not isinstance(recovered_reader, NullFeatureReader)
    
    @property
    def behavior_boost(self):
        """Lazy initialization of BehaviorBoost for personalization"""
        if self._behavior_boost is None:
            self._behavior_boost = BehaviorBoost(
                exact_click_boost=PersonalizationConfig.EXACT_CLICK_BOOST,
                category_affinity_boost=PersonalizationConfig.CATEGORY_AFFINITY_BOOST,
                popularity_1h_boost=PersonalizationConfig.POPULARITY_1H_BOOST,
                popularity_24h_boost=PersonalizationConfig.POPULARITY_24H_BOOST,
                popularity_7d_boost=PersonalizationConfig.POPULARITY_7D_BOOST,
                max_recent_clicks=PersonalizationConfig.MAX_RECENT_CLICKS_USED,
                debug_mode=PersonalizationConfig.DEBUG_MODE
            )
        return self._behavior_boost

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
                    content=generate_latest_metrics(), media_type=metrics_content_type
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
                except (ValidationError, json.JSONDecodeError) as e:
                    logger.warning("Invalid search request: %s", e)
                    return JSONResponse({"error": "invalid_request"}, status_code=422)
                except Exception as e:
                    logger.exception("Search request failed: %s", e)
                    return JSONResponse({"error": "internal_error"}, status_code=500)

            # CLIP image search endpoint (POST /search/image)
            if request.method == "POST" and path == "/search/image":
                return await self._image_search_handler(request)

            # Unknown path
            return JSONResponse({"error": "Not found", "path": path}, status_code=404)

        finally:
            # Detach trace context to clean up resources
            detach(token)

    async def _readyz_handler(self):
        """Readiness check for all dependencies"""

        # Redis is a hard readiness dependency because core metadata enrich and
        # popularity fallback both require Redis, independent of personalization.
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

        # Popularity is a hard readiness dependency because BROWSE traffic and
        # multiple graceful-degradation branches rely on it for partial success.
        try:
            popularity_ready = await self.popularity_handle.is_ready.remote()
            if not popularity_ready:
                return JSONResponse({"error": "popularity not ready"}, status_code=503)
        except Exception as e:
            return JSONResponse({"error": f"popularity not ready: {e}"}, status_code=503)

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
                    "popularity": True,
                    "retrieval": retrieval_ready,
                },
            }
        )

    async def _safe_popularity_topk(self, k: int) -> list[dict]:
        """Best-effort popularity fallback that never raises into request handling."""
        try:
            return await self.popularity_handle.topk.remote(k)
        except Exception as e:
            logger.exception("popularity_fallback_failed k=%d err=%s", k, e)
            return []

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
        trace_id = _current_trace_id()
        t0 = time.time()
        route_ms = None
        snapshot_ms = None
        fallback_ms = None

        # Load configuration from centralized config
        recall_k = RetrievalConfig.RECALL_K
        enrich_limit = RerankerConfig.MAX_DOCS
        timeout_ms = RerankerConfig.TIMEOUT_MS

        # Track contract_dbg for post-generation correction
        contract_dbg_cache = {}
        personalization_mode = "disabled"
        personalization_mode_recorded = False

        # Unified return helper to ensure consistent contract normalization
        def _build_response(results, route, latency_patch=None, extra_debug=None):
            """Helper to build response with contract normalization for all branches."""
            nonlocal personalization_mode_recorded

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

            if not personalization_mode_recorded:
                personalization_request_mode_total.labels(mode=personalization_mode).inc()
                personalization_mode_recorded = True

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
            main_span.set_attribute("trace_id", trace_id)
            try:
                t_route_phase0 = time.perf_counter()
                route = await self.router_handle.route.remote(req.query, req.user_id)
                route_ms = self._record_phase_metric("route", t_route_phase0, "success")
                main_span.set_attribute("intent", route.get("intent", "SEARCH"))
                logger.info("request_id=%s trace_id=%s phase=route outcome=success latency_ms=%.2f",
                            request_id, trace_id, route_ms)
            except Exception as e:
                route_ms = self._record_phase_metric("route", t_route_phase0, "error")
                logger.exception("request_id=%s trace_id=%s route_failed error=%s", request_id, trace_id, e)
                # Fallback to default SEARCH intent on routing failure
                route = {"intent": "SEARCH", "filters": {}}
                main_span.set_attribute("error", True)
                main_span.set_attribute("error.message", str(e))
                logger.warning("request_id=%s trace_id=%s phase=route outcome=error latency_ms=%.2f error=%s",
                               request_id, trace_id, route_ms, e)

            # Extract intent and filters from routing result
            intent = route.get("intent", "SEARCH")
            filters = route.get("filters") or {}
            # Determine A/B test flow: smart (with reranking) or base (without reranking)
            flow = route.get("flow") or ("smart" if bucket_user(req.user_id, 2) == 0 else "base")
            route["flow"] = flow

            # Enable reranking only if globally enabled AND user is in "smart" flow
            enable_rerank = RerankerConfig.ENABLED and (flow == "smart")

            if PersonalizationConfig.ENABLED:
                personalization_mode = (
                    "degraded_init_fallback"
                    if isinstance(self.feature_reader, NullFeatureReader)
                    else "normal"
                )
            else:
                personalization_mode = "disabled"

            # Handle BROWSE intent - return popular items directly
            if intent == "BROWSE":
                t_fallback0 = time.perf_counter()
                results = await self._safe_popularity_topk(req.k)
                fallback_ms = self._record_phase_metric("fallback", t_fallback0, "browse_popularity")

                logger.info(
                    "request_id=%s trace_id=%s intent=BROWSE k=%d phase=fallback outcome=browse_popularity latency_ms=%.2f",
                    request_id,
                    trace_id,
                    req.k,
                    fallback_ms,
                )
                _record_metrics(intent="BROWSE", flow=flow, status="success")
                return _build_response(results, route, latency_patch={"route": route_ms, "fallback": fallback_ms})

            # Base Flow Mode - support pure popularity baseline for A/B test control group
            # When BASE_FLOW_MODE=popularity and flow=base, skip embedding/retrieval entirely
            base_flow_mode = ABTestConfig.BASE_FLOW_MODE
            if flow == "base" and base_flow_mode == "popularity":
                # Use pure popularity baseline (no vector search)
                t_fallback0 = time.perf_counter()
                results = await self._safe_popularity_topk(req.k)
                fallback_ms = self._record_phase_metric("fallback", t_fallback0, "base_popularity")
                logger.info(
                    "request_id=%s trace_id=%s intent=SEARCH flow=base mode=popularity k=%d phase=fallback outcome=base_popularity latency_ms=%.2f",
                    request_id,
                    trace_id,
                    req.k,
                    fallback_ms,
                )
                _record_metrics(intent="SEARCH", flow="base", status="success")
                return _build_response(
                    results,
                    route,
                    latency_patch={"route": route_ms, "fallback": fallback_ms},
                    extra_debug={"base_flow_mode": "popularity"} if req.debug else None,
                )

            # Generate query embedding for semantic search
            embed_ms = None
            embed_timeout_ms = EmbeddingConfig.TIMEOUT_MS
            with self.tracer.start_as_current_span("embed") as span:
                span.set_attribute("query", req.query)
                t_embed_phase0 = time.perf_counter()
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
                        self._record_phase_metric("embed", t_embed_phase0, "success")
                        span.set_attribute("latency_ms", embed_ms)
                    except asyncio.TimeoutError:
                        embed_ms = float(embed_timeout_ms)
                        self._record_phase_metric("embed", t_embed_phase0, "timeout")
                        span.set_attribute("timeout", True)
                        span.set_attribute("latency_ms", embed_ms)
                        logger.warning(
                            "request_id=%s trace_id=%s embed_timeout timeout_ms=%d -> fallback popularity",
                            request_id,
                            trace_id,
                            embed_timeout_ms,
                        )
                        t_fallback0 = time.perf_counter()
                        results = await self._safe_popularity_topk(req.k)
                        fallback_ms = self._record_phase_metric("fallback", t_fallback0, DegradationReason.INFERENCE_TIMEOUT.value)
                        _record_request_degraded(self._get_metrics(), DegradationReason.INFERENCE_TIMEOUT)
                        _record_metrics(intent=intent, flow=flow, status="fallback")
                        return _build_response(
                            results, route, latency_patch={"route": route_ms, "embed": embed_ms, "fallback": fallback_ms}
                        )
                except Exception as e:
                    # Fallback to popularity on embedding failure
                    self._record_phase_metric("embed", t_embed_phase0, "error")
                    span.set_attribute("error", True)
                    span.set_attribute("error.message", str(e))
                    logger.exception(
                            "request_id=%s trace_id=%s embed_failed error=%s -> fallback popularity",
                        request_id,
                        trace_id,
                        e,
                    )
                    t_fallback0 = time.perf_counter()
                    results = await self._safe_popularity_topk(req.k)
                    fallback_ms = self._record_phase_metric("fallback", t_fallback0, DegradationReason.INFERENCE_UNAVAILABLE.value)
                    _record_request_degraded(self._get_metrics(), DegradationReason.INFERENCE_UNAVAILABLE)
                    _record_metrics(intent=intent, flow=flow, status="fallback")
                    return _build_response(
                        results, route, latency_patch={"route": route_ms, "embed": embed_ms, "fallback": fallback_ms}
                    )

            # Retrieve candidates from vector database
            ret_ms = None
            retrieval_timeout_ms = RetrievalConfig.TIMEOUT_MS
            with self.tracer.start_as_current_span("retrieve") as span:
                span.set_attribute("candidate_k", recall_k)
                span.set_attribute("filters", json.dumps(filters))
                t_retrieve_phase0 = time.perf_counter()
                try:
                    t_ret0 = time.time()
                    # Use recall_k and enrich_limit already configured at function start
                    candidate_k = recall_k
                    try:
                        candidates = await asyncio.wait_for(
                            self.retrieval_handle.search.remote(
                                vector,
                                candidate_k=candidate_k,
                                filters=filters,
                            ),
                            timeout=retrieval_timeout_ms / 1000.0,
                        )
                        ret_ms = (time.time() - t_ret0) * 1000
                        self._record_phase_metric("retrieve", t_retrieve_phase0, "success")
                        span.set_attribute("latency_ms", ret_ms)
                        span.set_attribute("result_count", len(candidates))
                    except asyncio.TimeoutError:
                        ret_ms = float(retrieval_timeout_ms)
                        self._record_phase_metric("retrieve", t_retrieve_phase0, "timeout")
                        span.set_attribute("timeout", True)
                        span.set_attribute("latency_ms", ret_ms)
                        logger.warning(
                            "request_id=%s trace_id=%s retrieval_timeout timeout_ms=%d -> fallback popularity",
                            request_id,
                            trace_id,
                            retrieval_timeout_ms,
                        )
                        t_fallback0 = time.perf_counter()
                        results = await self._safe_popularity_topk(req.k)
                        fallback_ms = self._record_phase_metric("fallback", t_fallback0, DegradationReason.INFERENCE_TIMEOUT.value)
                        _record_request_degraded(self._get_metrics(), DegradationReason.INFERENCE_TIMEOUT)
                        _record_metrics(intent=intent, flow=flow, status="fallback")
                        return _build_response(
                            results,
                            route,
                            latency_patch={"route": route_ms, "embed": embed_ms, "retrieve": ret_ms, "fallback": fallback_ms},
                        )
                except Exception as e:
                    # Fallback to popularity on retrieval failure
                    self._record_phase_metric("retrieve", t_retrieve_phase0, "error")
                    span.set_attribute("error", True)
                    span.set_attribute("error.message", str(e))
                    logger.exception(
                            "request_id=%s trace_id=%s retrieval_failed error=%s -> fallback popularity",
                        request_id,
                        trace_id,
                        e,
                    )
                    t_fallback0 = time.perf_counter()
                    results = await self._safe_popularity_topk(req.k)
                    fallback_ms = self._record_phase_metric("fallback", t_fallback0, DegradationReason.INFERENCE_UNAVAILABLE.value)
                    _record_request_degraded(self._get_metrics(), DegradationReason.INFERENCE_UNAVAILABLE)
                    _record_metrics(intent=intent, flow=flow, status="fallback")
                    return _build_response(
                        results,
                        route,
                        latency_patch={"route": route_ms, "embed": embed_ms, "retrieve": ret_ms, "fallback": fallback_ms},
                    )

            # Enrich candidates with metadata and apply post-retrieval filters
            enrich_ms = None
            rerank_ms = None
            rerank_mode = None
            rerank_effect = None  # Initialize rerank_effect

            try:
                with self.tracer.start_as_current_span("enrich") as span:
                    t_enrich_phase0 = time.perf_counter()
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
                        self._record_phase_metric("enrich", t_enrich_phase0, "success")
                        span.set_attribute("latency_ms", enrich_ms)
                        span.set_attribute("result_count", len(results))
                    except Exception as e_enrich:
                        self._record_phase_metric("enrich", t_enrich_phase0, "error")
                        span.set_attribute("error", True)
                        span.set_attribute("error.message", str(e_enrich))
                        raise

                # Fallback to popularity if all candidates filtered out
                # Initialize generation_ms to avoid undefined in fallback path
                generation_ms = None

                if not results:
                    t_fallback0 = time.perf_counter()
                    results = await self._safe_popularity_topk(req.k)
                    fallback_ms = self._record_phase_metric("fallback", t_fallback0, DegradationReason.EMPTY_RESULTS_ALLOWED.value)
                    _record_request_degraded(self._get_metrics(), DegradationReason.EMPTY_RESULTS_ALLOWED)
                else:
                    # Rerank results for improved semantic relevance (if enabled for user's flow)
                    rerank_effect = None
                    if enable_rerank:
                        with self.tracer.start_as_current_span("rerank") as span:
                            span.set_attribute("enabled", True)
                            span.set_attribute("doc_count", len(results))
                            t_rerank_phase0 = time.perf_counter()
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
                                    self._record_phase_metric("rerank", t_rerank_phase0, "timeout")
                                    span.set_attribute("timeout", True)
                                    span.set_attribute("latency_ms", rerank_ms)
                                    logger.warning(
                                        "request_id=%s trace_id=%s rerank_timeout timeout_ms=%d -> skip rerank",
                                        request_id,
                                        trace_id,
                                        timeout_ms,
                                    )
                                    info = None

                                if info:
                                    scores = info.get("scores", [])
                                    rerank_ms = info.get(
                                        "rerank_ms", (time.time() - t_rr0) * 1000
                                    )
                                    self._record_phase_metric("rerank", t_rerank_phase0, "success")
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

                                    # Load one request-scoped personalization snapshot and
                                    # apply boost without any ad hoc Redis fan-out.
                                    behavior_boost_info = {"boosted_items": 0}
                                    if PersonalizationConfig.ENABLED:
                                        try:
                                            t_snapshot_phase0 = time.perf_counter()
                                            candidate_item_ids = [
                                                r.get("article_id") for r in results if r.get("article_id")
                                            ]
                                            snapshot = await asyncio.to_thread(
                                                self.feature_reader.load_personalization_snapshot,
                                                req.user_id,
                                                candidate_item_ids,
                                                max_recent_clicks=PersonalizationConfig.MAX_RECENT_CLICKS_USED,
                                            )
                                            snapshot_ms = self._record_phase_metric(
                                                "personalization_snapshot",
                                                t_snapshot_phase0,
                                                "degraded" if snapshot.degraded else "success",
                                            )
                                            behavior_boost_info = self.behavior_boost.apply_boost(
                                                snapshot, results
                                            )
                                            logger.info(
                                                "request_id=%s trace_id=%s phase=personalization_snapshot outcome=%s latency_ms=%.2f redis_round_trips=%d degrade_reasons=%s",
                                                request_id,
                                                trace_id,
                                                "degraded" if snapshot.degraded else "success",
                                                snapshot_ms,
                                                snapshot.redis_round_trips,
                                                ",".join(reason.value for reason in snapshot.degraded_reasons) if snapshot.degraded_reasons else "none",
                                            )
                                            if snapshot.degraded:
                                                for degraded_reason in snapshot.degraded_reasons:
                                                    _record_request_degraded(self._get_metrics(), degraded_reason)
                                            if req.debug:
                                                behavior_boost_info["snapshot"] = {
                                                    "redis_round_trips": snapshot.redis_round_trips,
                                                    "degraded": snapshot.degraded,
                                                    "degraded_reasons": [reason.value for reason in snapshot.degraded_reasons],
                                                }
                                        except Exception as e:
                                            logger.warning(
                                                "request_id=%s trace_id=%s behavior_boost_failed user_id=%s error=%s -> keep rerank result",
                                                request_id,
                                                trace_id,
                                                req.user_id,
                                                e,
                                            )
                                            behavior_boost_info = {"boosted_items": 0, "degraded": True}
                                            personalization_mode = "degraded_runtime_boost_failure"
                                            self._record_phase_metric("personalization_snapshot", t_snapshot_phase0, DegradationReason.PERSONALIZATION_UNAVAILABLE.value)
                                            _record_request_degraded(self._get_metrics(), DegradationReason.PERSONALIZATION_UNAVAILABLE)
                                    else:
                                        logger.debug("⏸️  Personalization disabled via PERSONALIZATION_ENABLED")

                                    # Capture order after reranking (and boosting)
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
                                            "before_top5=%s after_top5=%s behavior_boost=%s",
                                            request_id,
                                            changed_positions,
                                            top_k_compare,
                                            top1_changed,
                                            before_ids[:5],
                                            after_ids[:5],
                                            behavior_boost_info,
                                        )
                                        # Detailed score comparison for top 3
                                        for i in range(min(3, len(before_ids))):
                                            boost_reason = (
                                                ((results[i].get("_debug") or {}).get("boost") or {}).get("boost_reason")
                                                or results[i].get("boost_reason")
                                                or "none"
                                            )
                                            logger.info(
                                                "request_id=%s RERANK_DETAIL rank=%d: "
                                                "article_id=%s vector_score=%.4f rerank_score=%.4f boost=%s",
                                                request_id,
                                                i + 1,
                                                after_ids[i],
                                                results[i].get("score", 0),
                                                results[i].get("rerank_score", 0),
                                                boost_reason,
                                            )

                            except Exception as e:
                                self._record_phase_metric("rerank", t_rerank_phase0, "error")
                                span.set_attribute("error", True)
                                span.set_attribute("error.message", str(e))
                                logger.exception(
                                    "request_id=%s trace_id=%s rerank_failed error=%s -> continue without rerank",
                                    request_id,
                                    trace_id,
                                    e,
                                )
                    else:
                        # Reranking disabled for this user's flow
                        rerank_mode = "off"

                    # Trim to requested number of results
                    results = results[: req.k]

                # Generate recommendation reason for Top-1 item (if generation enabled)
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
                                # Reduced timeout to 500ms for faster response
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

                                # Add reason and reason_source at root level
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

                                # Update contract_dbg if reason was generated
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
                                # Add reason_source=fallback on timeout
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
                                # Add reason_source=fallback on error
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
                # Fallback to popularity on enrichment/filtering failure (also needs contract normalize)
                main_span.set_attribute("error", True)
                main_span.set_attribute("error.message", str(e))
                logger.exception(
                    "request_id=%s trace_id=%s enrich_filter_failed error=%s -> fallback popularity",
                    request_id,
                    trace_id,
                    e,
                )
                t_fallback0 = time.perf_counter()
                results = await self._safe_popularity_topk(req.k)
                fallback_ms = self._record_phase_metric("fallback", t_fallback0, DegradationReason.INFERENCE_UNAVAILABLE.value)
                _record_request_degraded(self._get_metrics(), DegradationReason.INFERENCE_UNAVAILABLE)
                metrics = self._get_metrics()
                metrics["REQUEST_TOTAL"].labels(
                    intent=intent, flow=flow, status="fallback"
                ).inc()
                return _build_response(
                    results,
                    route,
                    latency_patch={
                    "route": route_ms,
                        "embed": embed_ms,
                        "retrieve": ret_ms,
                        "enrich": enrich_ms,
                        "rerank": rerank_ms,
                    "personalization_snapshot": snapshot_ms,
                    "fallback": fallback_ms,
                    },
                    extra_debug=(
                        {"rerank": {"mode": rerank_mode}} if rerank_mode else None
                    ),
                )

            # Build final response using unified helper (ensures contract_dbg is always defined)
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
                    "route": route_ms,
                    "embed": embed_ms,
                    "retrieve": ret_ms,
                    "enrich": enrich_ms,
                    "rerank": rerank_ms,
                    "personalization_snapshot": snapshot_ms,
                    "fallback": fallback_ms,
                    "generation": generation_ms,
                },
                extra_debug=extra_debug if extra_debug else None,
            )

            # Log request metrics for monitoring and analysis
            total_ms = resp.get("latency_ms", {}).get("total")
            logger.info(
                "request_id=%s trace_id=%s intent=SEARCH k=%d route_ms=%.2f embed_ms=%.2f ret_ms=%.2f snapshot_ms=%.2f gen_ms=%.2f total_ms=%.2f filters=%s personalization_mode=%s",
                request_id,
                trace_id,
                req.k,
                route_ms or -1,
                embed_ms or -1,
                ret_ms or -1,
                snapshot_ms or -1,
                generation_ms or -1,
                total_ms or -1,
                json.dumps(filters, ensure_ascii=False),
                personalization_mode,
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

    async def _image_search_handler(self, request: Request) -> JSONResponse:
        """
        Handle vision-based image/multimodal search requests.

        Request body:
        {
            "mode": "image"|"text_to_image"|"multimodal",
            "image_url": "https://example.com/image.jpg",  # OR
            "image_base64": "iVBORw0KGgo...",
            "query": "red dress",  # For text_to_image or multimodal
            "k": 10
        }

        Response:
        {
            "items": [
                {"article_id": "12345", "score": 0.95, "meta": {...}},
                ...
            ],
            "k": 10,
            "query_time_ms": 234,
            "status": "success"
        }
        """
        t0 = time.time()

        try:
            # Check if vision is available
            if self.vision_handle is None:
                return JSONResponse(
                    {
                        "error": "Vision search not available. Set VISION_ENABLED=1 and install transformers + pymilvus to enable.",
                        "status": "unavailable",
                    },
                    status_code=503,
                )

            # Parse request
            body = await request.json()
            k = body.get("k", 10)
            mode = str(body.get("mode") or "").strip().lower()
            request_id = str(uuid.uuid4())

            if mode == "multimodal":
                query = str(body.get("query") or "").strip()
                has_image = bool(body.get("image_url") or body.get("image_base64"))
                if not query or not has_image:
                    return JSONResponse(
                        {
                            "error": "multimodal mode requires both query and image_url/image_base64",
                            "status": "error",
                            "mode": "multimodal",
                            "request_id": request_id,
                        },
                        status_code=400,
                    )

                return await self._multimodal_image_search(body, request_id)

            vision_mode = "image" if mode == "image_to_image" else mode
            body["mode"] = vision_mode

            # Call vision deployment
            vision_result = await self.vision_handle.remote(body)

            # Check for errors
            if vision_result.get("status") == "error":
                return JSONResponse(
                    {
                        "error": vision_result.get("error", "Vision search failed"),
                        "status": "error",
                    },
                    status_code=500,
                )

            # Get article IDs from vision response
            vision_items = vision_result.get("items", [])

            # Enrich with metadata from Redis
            results = []
            for item in vision_items[:k]:
                aid = item.get("article_id")
                if not aid:
                    continue
                aid = canonical_article_id(aid)
                    
                score = item.get("score", 0.0)

                # Get metadata from Redis
                meta_key = item_key(aid)
                try:
                    raw_meta = await asyncio.to_thread(self.redis.hgetall, meta_key)
                    if raw_meta:
                        # Decode bytes to strings
                        raw_meta = {
                            k.decode() if isinstance(k, bytes) else k: (
                                v.decode() if isinstance(v, bytes) else v
                            )
                            for k, v in raw_meta.items()
                        }
                        meta, _ = _normalize_meta(aid, raw_meta)
                    else:
                        # Fallback if no metadata
                        meta = {
                            "title": f"Product {aid}",
                            "image_url": _local_image_url(aid),
                            "dept": "",
                            "desc": "",
                            "price": None,
                            "color": "",
                            "reason": "",
                        }
                except Exception as e:
                    logger.warning(f"Failed to fetch metadata for {aid}: {e}")
                    meta = {
                        "title": f"Product {aid}",
                        "image_url": _local_image_url(aid),
                        "dept": "",
                        "desc": "",
                        "price": None,
                        "color": "",
                        "reason": "",
                    }

                results.append({"article_id": aid, "score": score, "meta": meta})

            # Normalize contract
            results, contract_debug = _contract_normalize(results, k)

            total_ms = int((time.time() - t0) * 1000)

            return JSONResponse(
                {
                    "items": results,
                    "k": k,
                    "request_id": request_id,
                    "latency_ms": total_ms,
                    "query_time_ms": total_ms,
                    "mode": vision_result.get("mode", "unknown"),
                    "degraded": False,
                    "contract_debug": contract_debug,
                    "status": "success",
                }
            )

        except Exception as e:
            logger.exception("Image search failed: %s", e)
            return JSONResponse({"error": str(e), "status": "error"}, status_code=500)

    async def _multimodal_image_search(self, body: dict, request_id: str) -> JSONResponse:
        """Run bounded dual-recall merge-rerank for explicit multimodal requests."""
        started_at = time.perf_counter()
        query = str(body.get("query") or "").strip()
        k = int(body.get("k", 10))
        debug = bool(body.get("debug", False))
        text_weight = float(body.get("text_weight", 0.5) or 0.5)
        image_weight = float(body.get("image_weight", 0.5) or 0.5)
        per_recall_k = min(RetrievalConfig.RECALL_K, max(k * 3, min(RerankerConfig.MAX_DOCS, 50)))
        merge_limit = min(RerankerConfig.MAX_DOCS, per_recall_k * 2)

        async def recall_text_candidates() -> list[dict]:
            vector = await asyncio.wait_for(
                self.embedding_handle.embed.remote(query, is_query=True),
                timeout=EmbeddingConfig.TIMEOUT_MS / 1000.0,
            )
            return await asyncio.wait_for(
                self.retrieval_handle.search.remote(
                    vector,
                    candidate_k=per_recall_k,
                    filters={},
                ),
                timeout=RetrievalConfig.TIMEOUT_MS / 1000.0,
            )

        async def recall_image_candidates() -> list[dict]:
            vision_body = {
                "mode": "image",
                "image_url": body.get("image_url"),
                "image_base64": body.get("image_base64"),
                "k": per_recall_k,
                "ef": body.get("ef", 100),
            }
            vision_result = await self.vision_handle.remote(vision_body)
            if vision_result.get("status") == "error":
                raise RuntimeError(vision_result.get("error", "image recall failed"))
            return list(vision_result.get("items") or [])

        text_started = time.perf_counter()
        text_task = asyncio.create_task(recall_text_candidates())
        image_started = time.perf_counter()
        image_task = asyncio.create_task(recall_image_candidates())
        text_result, image_result = await asyncio.gather(text_task, image_task, return_exceptions=True)
        text_ms = (time.perf_counter() - text_started) * 1000.0
        image_ms = (time.perf_counter() - image_started) * 1000.0

        degraded_reasons = []
        text_candidates: list[dict] = []
        image_candidates: list[dict] = []

        if isinstance(text_result, Exception):
            degraded_reasons.append("text_recall_failed")
            logger.warning("multimodal text recall failed: %s", text_result)
        else:
            text_candidates = list(text_result)

        if isinstance(image_result, Exception):
            degraded_reasons.append("image_recall_failed")
            logger.warning("multimodal image recall failed: %s", image_result)
        else:
            image_candidates = list(image_result)

        if not text_candidates and not image_candidates:
            return JSONResponse(
                {
                    "error": "Both multimodal recall branches failed",
                    "status": "error",
                    "mode": "multimodal",
                    "architecture": "dual_recall_merge_rerank",
                    "request_id": request_id,
                },
                status_code=500,
            )

        merged_candidates = merge_ranked_candidates(
            text_candidates,
            image_candidates,
            limit=merge_limit,
            text_weight=text_weight,
            image_weight=image_weight,
        )

        metrics = self._get_metrics()
        enrich_started = time.perf_counter()
        results = await _enrich_and_filter(
            self.redis,
            merged_candidates,
            {},
            k,
            limit=merge_limit,
            cache_hit_metric=metrics["CACHE_HIT_TOTAL"],
            cache_miss_metric=metrics["CACHE_MISS_TOTAL"],
        )
        enrich_ms = (time.perf_counter() - enrich_started) * 1000.0

        merged_by_id = {candidate["article_id"]: candidate for candidate in merged_candidates}
        for result in results:
            merged = merged_by_id.get(result.get("article_id")) or {}
            result["candidate_sources"] = merged.get("candidate_sources", [])
            result["merge_score"] = merged.get("merge_score")
            result["source_scores"] = merged.get("source_scores", {})
            result["score"] = merged.get("merge_score", result.get("score"))

        rerank_ms = None
        rerank_mode = None
        if results and RerankerConfig.ENABLED:
            docs = [_build_rerank_doc(result.get("meta", {})) for result in results]
            rerank_started = time.perf_counter()
            try:
                rerank_info = await asyncio.wait_for(
                    self.reranker_handle.score.remote(query, docs),
                    timeout=RerankerConfig.TIMEOUT_MS / 1000.0,
                )
                rerank_ms = rerank_info.get("rerank_ms", (time.perf_counter() - rerank_started) * 1000.0)
                rerank_mode = rerank_info.get("mode")
                scores = rerank_info.get("scores", [])
                for idx, result in enumerate(results):
                    result["rerank_score"] = float(scores[idx]) if idx < len(scores) else -1e9
                results.sort(key=lambda item: item.get("rerank_score", -1e9), reverse=True)
            except asyncio.TimeoutError:
                rerank_ms = float(RerankerConfig.TIMEOUT_MS)
                rerank_mode = "timeout"
                degraded_reasons.append("rerank_timeout")
            except Exception as exc:
                rerank_ms = (time.perf_counter() - rerank_started) * 1000.0
                rerank_mode = "error"
                degraded_reasons.append("rerank_failed")
                logger.warning("multimodal rerank failed: %s", exc)

        results, contract_debug = _contract_normalize(results, k)
        total_ms = (time.perf_counter() - started_at) * 1000.0
        degraded = bool(degraded_reasons)

        response = {
            "items": results,
            "k": k,
            "mode": "multimodal",
            "architecture": "dual_recall_merge_rerank",
            "status": "success",
            "request_id": request_id,
            "latency_ms": total_ms,
            "query_time_ms": total_ms,
            "degraded": degraded,
            "degraded_reason": ",".join(degraded_reasons) if degraded_reasons else None,
            "contract_debug": contract_debug,
        }

        if debug:
            response["debug"] = {
                "multimodal": {
                    "candidate_k_per_recall": per_recall_k,
                    "text_candidates": len(text_candidates),
                    "image_candidates": len(image_candidates),
                    "merged_candidates": len(merged_candidates),
                    "latency_ms": {
                        "text_recall": text_ms,
                        "image_recall": image_ms,
                        "enrich": enrich_ms,
                        "rerank": rerank_ms,
                        "total": total_ms,
                    },
                    "rerank_mode": rerank_mode,
                }
            }

        return JSONResponse(response)
