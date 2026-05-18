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
from src.deployments.multimodal import (
    merge_ranked_candidates,
    fuse_with_normalized_scores,
    apply_behavior_boost_to_hybrid_results,
)

from src.config import (
    RetrievalConfig,
    EmbeddingConfig,
    RerankerConfig,
    ABTestConfig,
    PersonalizationConfig,
    GenerationConfig,
)
from src.degradation import DegradationReason

# Initialize observability
from src.utils.observability import setup_tracing
from src.utils.bucketing import bucket_user
from src.utils.metrics import (
    counter,
    generate_latest_metrics,
    histogram,
    metrics_content_type,
)

# Personalization module
from src.personalization import FeatureReader, BehaviorBoost, NullFeatureReader
from src.services.search_result_service import (
    normalize_meta as _normalize_meta,
    enrich_and_filter as _enrich_and_filter,
    build_rerank_doc as _build_rerank_doc,
)
from src.utils.contract import _contract_normalize, _local_image_url
from src.utils.redis_client import RedisClient
from src.utils.redis_metadata import canonical_article_id, item_key

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
    return (
        span_context.trace_id.to_bytes(16, "big").hex()
        if span_context.is_valid
        else "trace-unavailable"
    )


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
    # Hard per-replica cap; autoscaling target (10) is the soft trigger.
    # Default 50 is non-restrictive at current traffic; override via INGRESS_MAX_ONGOING_REQUESTS.
    max_ongoing_requests=int(os.getenv("INGRESS_MAX_ONGOING_REQUESTS", "50")),
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
        generation_handle: Optional[DeploymentHandle] = None,
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
        self._probe_stop_event = (
            threading.Event()
        )  # Signal probe thread to stop after recovery

        # Deferred import: Prometheus Gauge objects contain a _thread.lock that
        # Ray 2.x cloudpickle cannot serialize at class-definition time.  Storing
        # them as instance attributes keeps them out of the module-level global
        # scope that Ray inspects when building ReplicaConfig.
        from src.personalization.metrics import (
            personalization_fallback_active,
            personalization_fallback_total,
            personalization_request_mode_total,
        )
        self._personalization_fallback_active = personalization_fallback_active
        self._personalization_fallback_total = personalization_fallback_total
        self._personalization_request_mode_total = personalization_request_mode_total

        self._personalization_fallback_active.set(0)

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

    def _record_phase_metric(
        self, phase: str, started_at: float, outcome: str = "success"
    ) -> float:
        elapsed = time.perf_counter() - started_at
        self._get_metrics()["REQUEST_PHASE_DURATION"].labels(
            phase=phase, outcome=outcome
        ).observe(elapsed)
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
            self._personalization_fallback_active.set(0)
            return feature_reader
        except Exception as e:
            logger.warning(
                "personalization_init_failed err=%s -> using NullFeatureReader",
                e,
            )
            self._personalization_fallback_total.labels(
                reason=DegradationReason.PERSONALIZATION_UNAVAILABLE.value
            ).inc()
            self._feature_reader_last_init_failed_at = time.time()
            self._personalization_fallback_active.set(1)
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
                logger.info(
                    "personalization_probe_thread_exiting reason=recovery_successful"
                )
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
                debug_mode=PersonalizationConfig.DEBUG_MODE,
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

            # Hybrid text + image search endpoint (POST /search/hybrid)
            if request.method == "POST" and path == "/search/hybrid":
                return await self._hybrid_search_handler(request)

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
            return JSONResponse(
                {"error": f"popularity not ready: {e}"}, status_code=503
            )

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

    def _build_text_search_response(
        self,
        req: "SearchRequest",
        t0: float,
        request_id: str,
        route: dict,
        results: list,
        *,
        ctx: dict,
        recall_k: int,
        enrich_limit: int,
        latency_patch: Optional[dict] = None,
        extra_debug: Optional[dict] = None,
    ) -> dict:
        """Build response with contract normalization; records personalization mode metric once."""
        results, contract_dbg = _contract_normalize(results, limit=req.k)
        ctx["contract_dbg_cache"].clear()
        ctx["contract_dbg_cache"].update(contract_dbg)

        total_ms = (time.time() - t0) * 1000
        resp = {
            "query": req.query,
            "route": route,
            "results": results,
            "request_id": request_id,
        }

        if req.debug:
            latency = latency_patch or {}
            latency["total"] = total_ms
            resp["latency_ms"] = latency
            resp["pipeline"] = {
                "flow": route.get("flow", "smart"),
                "recall_k": recall_k,
                "rerank_max_docs": enrich_limit,
                "rerank_enabled": RerankerConfig.ENABLED and (route.get("flow") == "smart"),
            }
            resp["contract"] = contract_dbg
            if extra_debug:
                resp.update(extra_debug)

        if not ctx["personalization_mode_recorded"]:
            self._personalization_request_mode_total.labels(
                mode=ctx["personalization_mode"]
            ).inc()
            ctx["personalization_mode_recorded"] = True

        return resp

    def _record_search_metrics(self, t0: float, intent: str, flow: str, status: str = "success") -> None:
        """Record request-level Prometheus metrics."""
        logger.debug(f"Metrics recorded: intent={intent} flow={flow} status={status}")
        metrics = self._get_metrics()
        metrics["REQUEST_TOTAL"].labels(intent=intent, flow=flow, status=status).inc()
        metrics["REQUEST_DURATION"].labels(intent=intent, flow=flow).observe((time.time() - t0))

    async def _run_browse_fallback(
        self, req: "SearchRequest", request_id: str, trace_id: str, route_ms: Optional[float]
    ) -> tuple:
        """Fetch popularity top-k for BROWSE intent; returns (results, fallback_ms)."""
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
        return results, fallback_ms

    async def _run_base_flow_fallback(
        self, req: "SearchRequest", request_id: str, trace_id: str, route_ms: Optional[float]
    ) -> tuple:
        """Fetch popularity top-k for base-flow popularity mode; returns (results, fallback_ms)."""
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
        return results, fallback_ms

    async def _run_embed(
        self,
        req: "SearchRequest",
        request_id: str,
        trace_id: str,
        intent: str,
        flow: str,
    ) -> tuple:
        """
        Run query embedding phase.

        Returns (vector, embed_ms, degradation_reason_or_None).
        On success degradation_reason is None; on timeout it is INFERENCE_TIMEOUT;
        on other errors it is INFERENCE_UNAVAILABLE.
        Does NOT fetch the popularity fallback — caller handles that based on the reason.
        """
        embed_ms = None
        embed_timeout_ms = EmbeddingConfig.TIMEOUT_MS
        with self.tracer.start_as_current_span("embed") as span:
            span.set_attribute("query", req.query)
            t_embed_phase0 = time.perf_counter()
            try:
                t_embed0 = time.time()
                try:
                    vector = await asyncio.wait_for(
                        self.embedding_handle.embed.remote(req.query, is_query=True),
                        timeout=embed_timeout_ms / 1000.0,
                    )
                    embed_ms = (time.time() - t_embed0) * 1000
                    self._record_phase_metric("embed", t_embed_phase0, "success")
                    span.set_attribute("latency_ms", embed_ms)
                    return vector, embed_ms, None
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
                    return None, embed_ms, DegradationReason.INFERENCE_TIMEOUT
            except Exception as e:
                self._record_phase_metric("embed", t_embed_phase0, "error")
                span.set_attribute("error", True)
                span.set_attribute("error.message", str(e))
                logger.exception(
                    "request_id=%s trace_id=%s embed_failed error=%s -> fallback popularity",
                    request_id,
                    trace_id,
                    e,
                )
                return None, embed_ms, DegradationReason.INFERENCE_UNAVAILABLE

    async def _run_retrieval(
        self,
        vector: list,
        req: "SearchRequest",
        request_id: str,
        trace_id: str,
        filters: dict,
        intent: str,
        flow: str,
    ) -> tuple:
        """
        Run vector retrieval phase.

        Returns (candidates, ret_ms, degradation_reason_or_None).
        On success degradation_reason is None; on timeout it is INFERENCE_TIMEOUT;
        on other errors it is INFERENCE_UNAVAILABLE.
        """
        ret_ms = None
        retrieval_timeout_ms = RetrievalConfig.TIMEOUT_MS
        recall_k = RetrievalConfig.RECALL_K
        with self.tracer.start_as_current_span("retrieve") as span:
            span.set_attribute("candidate_k", recall_k)
            span.set_attribute("filters", json.dumps(filters))
            t_retrieve_phase0 = time.perf_counter()
            try:
                t_ret0 = time.time()
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
                    return candidates, ret_ms, None
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
                    return None, ret_ms, DegradationReason.INFERENCE_TIMEOUT
            except Exception as e:
                self._record_phase_metric("retrieve", t_retrieve_phase0, "error")
                span.set_attribute("error", True)
                span.set_attribute("error.message", str(e))
                logger.exception(
                    "request_id=%s trace_id=%s retrieval_failed error=%s -> fallback popularity",
                    request_id,
                    trace_id,
                    e,
                )
                return None, ret_ms, DegradationReason.INFERENCE_UNAVAILABLE

    async def _run_enrich(
        self,
        candidates: list,
        req: "SearchRequest",
        filters: dict,
        enrich_limit: int,
        request_id: str,
        trace_id: str,
    ) -> tuple:
        """
        Run metadata enrichment phase.

        Returns (results, enrich_ms). Re-raises on failure (caller must handle).
        """
        enrich_ms = None
        with self.tracer.start_as_current_span("enrich") as span:
            t_enrich_phase0 = time.perf_counter()
            try:
                t_enrich0 = time.time()
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
                return results, enrich_ms
            except Exception as e_enrich:
                self._record_phase_metric("enrich", t_enrich_phase0, "error")
                span.set_attribute("error", True)
                span.set_attribute("error.message", str(e_enrich))
                raise

    async def _run_rerank_and_boost(
        self,
        results: list,
        req: "SearchRequest",
        request_id: str,
        trace_id: str,
        ctx: dict,
        timeout_ms: float,
    ) -> tuple:
        """
        Run reranking and personalization boost phase.

        Mutates results in place (sorting). Sets ctx["snapshot_ms"] and
        ctx["personalization_mode"] as side effects.
        Returns (rerank_ms, rerank_mode, rerank_effect, behavior_boost_info).
        """
        rerank_ms = None
        rerank_mode = None
        rerank_effect = None
        behavior_boost_info = {"boosted_items": 0}

        with self.tracer.start_as_current_span("rerank") as span:
            span.set_attribute("enabled", True)
            span.set_attribute("doc_count", len(results))
            t_rerank_phase0 = time.perf_counter()
            try:
                docs = [_build_rerank_doc(r.get("meta", {})) for r in results]
                t_rr0 = time.time()

                # Capture order before reranking for comparison
                before_ids = [r.get("article_id") for r in results]

                try:
                    info = await asyncio.wait_for(
                        self.reranker_handle.score.remote(req.query, docs),
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
                    rerank_ms = info.get("rerank_ms", (time.time() - t_rr0) * 1000)
                    self._record_phase_metric("rerank", t_rerank_phase0, "success")
                    rerank_mode = info.get("mode", rerank_mode)
                    span.set_attribute("latency_ms", rerank_ms)
                    span.set_attribute("mode", rerank_mode)

                    for i, r in enumerate(results):
                        r["rerank_score"] = float(scores[i]) if i < len(scores) else -1e9

                    results.sort(key=lambda x: x.get("rerank_score", -1e9), reverse=True)

                    # Load one request-scoped personalization snapshot and
                    # apply boost without any ad hoc Redis fan-out.
                    if PersonalizationConfig.ENABLED:
                        try:
                            t_snapshot_phase0 = time.perf_counter()
                            candidate_item_ids = [
                                r.get("article_id") for r in results if r.get("article_id")
                            ]
                            snapshot = await asyncio.wait_for(
                                asyncio.to_thread(
                                    self.feature_reader.load_personalization_snapshot,
                                    req.user_id,
                                    candidate_item_ids,
                                    max_recent_clicks=PersonalizationConfig.MAX_RECENT_CLICKS_USED,
                                ),
                                timeout=PersonalizationConfig.SNAPSHOT_TIMEOUT_MS / 1000.0,
                            )
                            ctx["snapshot_ms"] = self._record_phase_metric(
                                "personalization_snapshot",
                                t_snapshot_phase0,
                                "degraded" if snapshot.degraded else "success",
                            )
                            behavior_boost_info = self.behavior_boost.apply_boost(snapshot, results)
                            logger.info(
                                "request_id=%s trace_id=%s phase=personalization_snapshot outcome=%s latency_ms=%.2f redis_round_trips=%d degrade_reasons=%s",
                                request_id,
                                trace_id,
                                "degraded" if snapshot.degraded else "success",
                                ctx["snapshot_ms"],
                                snapshot.redis_round_trips,
                                (
                                    ",".join(reason.value for reason in snapshot.degraded_reasons)
                                    if snapshot.degraded_reasons
                                    else "none"
                                ),
                            )
                            if snapshot.degraded:
                                for degraded_reason in snapshot.degraded_reasons:
                                    _record_request_degraded(self._get_metrics(), degraded_reason)
                            if req.debug:
                                behavior_boost_info["snapshot"] = {
                                    "redis_round_trips": snapshot.redis_round_trips,
                                    "degraded": snapshot.degraded,
                                    "degraded_reasons": [
                                        reason.value for reason in snapshot.degraded_reasons
                                    ],
                                }
                        except asyncio.TimeoutError:
                            logger.warning(
                                "request_id=%s trace_id=%s personalization_snapshot_timeout user_id=%s timeout_ms=%.0f -> skip boost",
                                request_id,
                                trace_id,
                                req.user_id,
                                PersonalizationConfig.SNAPSHOT_TIMEOUT_MS,
                            )
                            behavior_boost_info = {"boosted_items": 0, "degraded": True}
                            ctx["personalization_mode"] = "degraded_timeout"
                            ctx["snapshot_ms"] = self._record_phase_metric(
                                "personalization_snapshot",
                                t_snapshot_phase0,
                                "timeout",
                            )
                            _record_request_degraded(
                                self._get_metrics(),
                                DegradationReason.REDIS_TIMEOUT,
                            )
                        except Exception as e:
                            logger.warning(
                                "request_id=%s trace_id=%s behavior_boost_failed user_id=%s error=%s -> keep rerank result",
                                request_id,
                                trace_id,
                                req.user_id,
                                e,
                            )
                            behavior_boost_info = {"boosted_items": 0, "degraded": True}
                            ctx["personalization_mode"] = "degraded_runtime_boost_failure"
                            self._record_phase_metric(
                                "personalization_snapshot",
                                t_snapshot_phase0,
                                DegradationReason.PERSONALIZATION_UNAVAILABLE.value,
                            )
                            _record_request_degraded(
                                self._get_metrics(),
                                DegradationReason.PERSONALIZATION_UNAVAILABLE,
                            )
                    else:
                        logger.debug("Personalization disabled via PERSONALIZATION_ENABLED")

                    # Capture order after reranking (and boosting)
                    after_ids = [r.get("article_id") for r in results]

                    # Calculate rerank effect
                    top_k_compare = min(len(before_ids), len(after_ids), req.k)
                    changed_positions = sum(
                        1 for i in range(top_k_compare) if before_ids[i] != after_ids[i]
                    )
                    top1_changed = (
                        before_ids[0] != after_ids[0] if before_ids and after_ids else False
                    )

                    rerank_effect = {
                        "changed_positions": changed_positions,
                        "top1_changed": top1_changed,
                        "total_compared": top_k_compare,
                    }
                    span.set_attribute("changed_positions", changed_positions)
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
                                (
                                    (results[i].get("_debug") or {}).get("boost") or {}
                                ).get("boost_reason")
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

        return rerank_ms, rerank_mode, rerank_effect, behavior_boost_info

    async def _run_generation(
        self,
        results: list,
        req: "SearchRequest",
        request_id: str,
        ctx: dict,
        *,
        intent: str,
        flow: str,
    ) -> Optional[float]:
        """
        Run generation phase for Top-1 recommendation reason.

        Mutates results[0] in place on success or failure. Updates ctx["contract_dbg_cache"]
        on successful generation. Returns generation_ms or None if not attempted.
        """
        generation_enabled = os.getenv("GENERATION_ENABLED", "0") == "1"
        generation_flow = os.getenv("GENERATION_FLOW", "smart")

        if not (results and self.generation_handle and generation_enabled):
            return None

        should_generate = (
            generation_flow == "all"
            or (generation_flow == "search" and intent == "SEARCH")
            or (generation_flow == "smart" and flow == "smart")
        )
        if not should_generate:
            return None

        generation_ms = None
        with self.tracer.start_as_current_span("llm.generate_reason") as span:
            span.set_attribute("enabled", True)
            span.set_attribute("query_len", len(req.query))
            span.set_attribute("topk", req.k)
            span.set_attribute("model", os.getenv("GENERATION_MODEL", "qwen2.5"))
            span.set_attribute("article_id", results[0].get("article_id", ""))
            try:
                t_gen0 = time.time()
                timeout = GenerationConfig.TIMEOUT_MS / 1000.0
                span.set_attribute("timeout_ms", GenerationConfig.TIMEOUT_MS)

                out = await asyncio.wait_for(
                    self.generation_handle.explain.remote(req.query, results[0]),
                    timeout=timeout,
                )
                generation_ms = (time.time() - t_gen0) * 1000
                reason_value = out.get("reason", "")
                mode = out.get("mode", "unknown")  # Extract actual mode (template/llm)

                # Add reason and reason_source at root level
                # Use actual mode instead of hardcoding "llm"
                results[0]["reason"] = reason_value
                results[0]["reason_source"] = mode if reason_value else "fallback"

                # Also keep in meta for backward compatibility
                results[0].setdefault("meta", {})["reason"] = reason_value

                span.set_attribute("latency_ms", generation_ms)
                span.set_attribute("mode", mode)
                span.set_attribute("fallback", not bool(reason_value))

                # Update contract_dbg if reason was generated
                if reason_value and ctx["contract_dbg_cache"]:
                    missing_by_field = ctx["contract_dbg_cache"].get("missing_by_field", {})
                    if missing_by_field.get("reason", 0) > 0:
                        missing_by_field["reason"] = 0
                        ctx["contract_dbg_cache"]["missing_total"] = sum(
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
                    (time.time() - t_gen0) * 1000 if "t_gen0" in locals() else 0
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

        return generation_ms

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
        # --- Setup ---
        request_id = str(uuid.uuid4())
        trace_id = _current_trace_id()
        t0 = time.time()
        route_ms = embed_ms = ret_ms = enrich_ms = rerank_ms = fallback_ms = generation_ms = None
        rerank_mode = rerank_effect = None
        behavior_boost_info = {"boosted_items": 0}

        recall_k = RetrievalConfig.RECALL_K
        enrich_limit = RerankerConfig.MAX_DOCS
        timeout_ms = RerankerConfig.TIMEOUT_MS

        ctx = {
            "personalization_mode": "disabled",
            "personalization_mode_recorded": False,
            "contract_dbg_cache": {},
            "snapshot_ms": None,
        }

        def _respond(results, latency_patch=None, extra_debug=None):
            return self._build_text_search_response(
                req, t0, request_id, route, results,
                ctx=ctx, recall_k=recall_k, enrich_limit=enrich_limit,
                latency_patch=latency_patch, extra_debug=extra_debug,
            )

        with self.tracer.start_as_current_span("search_request") as main_span:
            main_span.set_attribute("query", req.query)
            main_span.set_attribute("k", req.k)
            if req.user_id:
                main_span.set_attribute("user_id", req.user_id)
            main_span.set_attribute("request_id", request_id)
            main_span.set_attribute("trace_id", trace_id)

            # --- Route phase ---
            try:
                t_route_phase0 = time.perf_counter()
                route = await self.router_handle.route.remote(req.query, req.user_id)
                route_ms = self._record_phase_metric("route", t_route_phase0, "success")
                main_span.set_attribute("intent", route.get("intent", "SEARCH"))
                logger.info(
                    "request_id=%s trace_id=%s phase=route outcome=success latency_ms=%.2f",
                    request_id, trace_id, route_ms,
                )
            except Exception as e:
                route_ms = self._record_phase_metric("route", t_route_phase0, "error")
                logger.exception(
                    "request_id=%s trace_id=%s route_failed error=%s",
                    request_id, trace_id, e,
                )
                route = {"intent": "SEARCH", "filters": {}}
                main_span.set_attribute("error", True)
                main_span.set_attribute("error.message", str(e))
                logger.warning(
                    "request_id=%s trace_id=%s phase=route outcome=error latency_ms=%.2f error=%s",
                    request_id, trace_id, route_ms, e,
                )

            intent = route.get("intent", "SEARCH")
            filters = route.get("filters") or {}
            flow = route.get("flow") or (
                "smart" if bucket_user(req.user_id, 2) == 0 else "base"
            )
            route["flow"] = flow
            enable_rerank = RerankerConfig.ENABLED and (flow == "smart")

            if PersonalizationConfig.ENABLED:
                ctx["personalization_mode"] = (
                    "degraded_init_fallback"
                    if isinstance(self.feature_reader, NullFeatureReader)
                    else "normal"
                )
            else:
                ctx["personalization_mode"] = "disabled"

            # --- BROWSE branch ---
            if intent == "BROWSE":
                results, fallback_ms = await self._run_browse_fallback(
                    req, request_id, trace_id, route_ms
                )
                self._record_search_metrics(t0, "BROWSE", flow)
                return _respond(results, latency_patch={"route": route_ms, "fallback": fallback_ms})

            # --- Base flow popularity branch ---
            base_flow_mode = ABTestConfig.BASE_FLOW_MODE
            if flow == "base" and base_flow_mode == "popularity":
                results, fallback_ms = await self._run_base_flow_fallback(
                    req, request_id, trace_id, route_ms
                )
                self._record_search_metrics(t0, "SEARCH", "base")
                return _respond(
                    results,
                    latency_patch={"route": route_ms, "fallback": fallback_ms},
                    extra_debug={"base_flow_mode": "popularity"} if req.debug else None,
                )

            # --- Embed phase ---
            vector, embed_ms, embed_fail = await self._run_embed(
                req, request_id, trace_id, intent, flow
            )
            if embed_fail:
                t_f0 = time.perf_counter()
                results = await self._safe_popularity_topk(req.k)
                fallback_ms = self._record_phase_metric("fallback", t_f0, embed_fail.value)
                _record_request_degraded(self._get_metrics(), embed_fail)
                self._record_search_metrics(t0, intent, flow, "fallback")
                return _respond(
                    results,
                    latency_patch={"route": route_ms, "embed": embed_ms, "fallback": fallback_ms},
                )

            # --- Retrieve phase ---
            candidates, ret_ms, ret_fail = await self._run_retrieval(
                vector, req, request_id, trace_id, filters, intent, flow
            )
            if ret_fail:
                t_f0 = time.perf_counter()
                results = await self._safe_popularity_topk(req.k)
                fallback_ms = self._record_phase_metric("fallback", t_f0, ret_fail.value)
                _record_request_degraded(self._get_metrics(), ret_fail)
                self._record_search_metrics(t0, intent, flow, "fallback")
                return _respond(
                    results,
                    latency_patch={
                        "route": route_ms, "embed": embed_ms,
                        "retrieve": ret_ms, "fallback": fallback_ms,
                    },
                )

            # --- Enrich + Rerank + Generate ---
            try:
                results, enrich_ms = await self._run_enrich(
                    candidates, req, filters, enrich_limit, request_id, trace_id
                )

                if not results:
                    t_f0 = time.perf_counter()
                    results = await self._safe_popularity_topk(req.k)
                    fallback_ms = self._record_phase_metric(
                        "fallback", t_f0, DegradationReason.EMPTY_RESULTS_ALLOWED.value
                    )
                    _record_request_degraded(
                        self._get_metrics(), DegradationReason.EMPTY_RESULTS_ALLOWED
                    )
                else:
                    if enable_rerank:
                        rerank_ms, rerank_mode, rerank_effect, behavior_boost_info = (
                            await self._run_rerank_and_boost(
                                results, req, request_id, trace_id, ctx, timeout_ms
                            )
                        )
                    else:
                        rerank_mode = "off"
                    results = results[: req.k]

                generation_ms = await self._run_generation(
                    results, req, request_id, ctx, intent=intent, flow=flow
                )

            except Exception as e:
                main_span.set_attribute("error", True)
                main_span.set_attribute("error.message", str(e))
                logger.exception(
                    "request_id=%s trace_id=%s enrich_filter_failed error=%s -> fallback popularity",
                    request_id, trace_id, e,
                )
                t_f0 = time.perf_counter()
                results = await self._safe_popularity_topk(req.k)
                fallback_ms = self._record_phase_metric(
                    "fallback", t_f0, DegradationReason.INFERENCE_UNAVAILABLE.value
                )
                _record_request_degraded(
                    self._get_metrics(), DegradationReason.INFERENCE_UNAVAILABLE
                )
                self._record_search_metrics(t0, intent, flow, "fallback")
                return _respond(
                    results,
                    latency_patch={
                        "route": route_ms, "embed": embed_ms, "retrieve": ret_ms,
                        "enrich": enrich_ms, "rerank": rerank_ms,
                        "personalization_snapshot": ctx["snapshot_ms"],
                        "fallback": fallback_ms,
                    },
                    extra_debug={"rerank": {"mode": rerank_mode}} if rerank_mode else None,
                )

            # --- Final response ---
            snapshot_ms = ctx["snapshot_ms"]

            rerank_debug = {"mode": rerank_mode} if rerank_mode else None
            if rerank_effect:
                if rerank_debug is None:
                    rerank_debug = {}
                rerank_debug["effect"] = rerank_effect

            generation_debug = None
            if generation_ms is not None:
                generation_enabled = os.getenv("GENERATION_ENABLED", "0") == "1"
                generation_flow = os.getenv("GENERATION_FLOW", "smart")
                generation_debug = {
                    "enabled": generation_enabled,
                    "flow": generation_flow,
                    "latency_ms": round(generation_ms, 2),
                }

            extra_debug = {}
            if rerank_debug:
                extra_debug["rerank"] = rerank_debug
            if generation_debug:
                extra_debug["generation"] = generation_debug

            resp = _respond(
                results,
                latency_patch={
                    "route": route_ms, "embed": embed_ms, "retrieve": ret_ms,
                    "enrich": enrich_ms, "rerank": rerank_ms,
                    "personalization_snapshot": snapshot_ms,
                    "fallback": fallback_ms, "generation": generation_ms,
                },
                extra_debug=extra_debug if extra_debug else None,
            )

            total_ms = resp.get("latency_ms", {}).get("total")
            logger.info(
                "request_id=%s trace_id=%s intent=SEARCH k=%d route_ms=%.2f embed_ms=%.2f ret_ms=%.2f snapshot_ms=%.2f gen_ms=%.2f total_ms=%.2f filters=%s personalization_mode=%s",
                request_id, trace_id, req.k,
                route_ms or -1, embed_ms or -1, ret_ms or -1,
                snapshot_ms or -1, generation_ms or -1, total_ms or -1,
                json.dumps(filters, ensure_ascii=False),
                ctx["personalization_mode"],
            )

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

            metrics = self._get_metrics()
            metrics["REQUEST_TOTAL"].labels(intent=intent, flow=flow, status="success").inc()
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

            dto_items = []
            for r in results:
                meta = r.get("meta") or {}
                price_val = meta.get("price")
                try:
                    price = float(price_val) if price_val not in (None, "") else 0.0
                except (TypeError, ValueError):
                    price = 0.0
                dto_items.append(
                    {
                        "itemId": str(r.get("article_id", "")).zfill(10),
                        "name": meta.get("title") or "",
                        "category": meta.get("dept") or "",
                        "description": meta.get("desc") or "",
                        "price": price,
                        "imgUrl": meta.get("image_url") or "",
                        "source": "ray",
                        "degraded": False,
                        "degradedReason": None,
                        "reason": meta.get("reason") or "",
                        "reasonSource": None,
                    }
                )

            return JSONResponse(
                {
                    "items": dto_items,
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

    async def _multimodal_image_search(
        self, body: dict, request_id: str
    ) -> JSONResponse:
        """Run bounded dual-recall merge-rerank for explicit multimodal requests."""
        started_at = time.perf_counter()
        query = str(body.get("query") or "").strip()
        k = int(body.get("k", 10))
        debug = bool(body.get("debug", False))
        text_weight = float(body.get("text_weight", 0.5) or 0.5)
        image_weight = float(body.get("image_weight", 0.5) or 0.5)
        per_recall_k = min(
            RetrievalConfig.RECALL_K, max(k * 3, min(RerankerConfig.MAX_DOCS, 50))
        )
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
        text_result, image_result = await asyncio.gather(
            text_task, image_task, return_exceptions=True
        )
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

        merged_by_id = {
            candidate["article_id"]: candidate for candidate in merged_candidates
        }
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
                rerank_ms = rerank_info.get(
                    "rerank_ms", (time.perf_counter() - rerank_started) * 1000.0
                )
                rerank_mode = rerank_info.get("mode")
                scores = rerank_info.get("scores", [])
                for idx, result in enumerate(results):
                    result["rerank_score"] = (
                        float(scores[idx]) if idx < len(scores) else -1e9
                    )
                results.sort(
                    key=lambda item: item.get("rerank_score", -1e9), reverse=True
                )
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

        dto_items = []
        for r in results:
            meta = r.get("meta") or {}
            price_val = meta.get("price")
            try:
                price = float(price_val) if price_val not in (None, "") else 0.0
            except (TypeError, ValueError):
                price = 0.0
            dto_items.append(
                {
                    "itemId": str(r.get("article_id", "")).zfill(10),
                    "name": meta.get("title") or "",
                    "category": meta.get("dept") or "",
                    "description": meta.get("desc") or "",
                    "price": price,
                    "imgUrl": meta.get("image_url") or "",
                    "source": "ray",
                    "degraded": degraded,
                    "degradedReason": ",".join(degraded_reasons) if len(degraded_reasons) == 1 else (
                        ",".join(degraded_reasons) if degraded_reasons else None
                    ),
                    "reason": meta.get("reason") or "",
                    "reasonSource": None,
                    "candidateSources": r.get("candidate_sources", []),
                }
            )

        response = {
            "items": dto_items,
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

    async def _hybrid_search_handler(self, request: Request) -> JSONResponse:
        """
        Hybrid text + image search: dual recall with min-max normalized score fusion.

        Runs BGE-small text retrieval and CLIP image retrieval in parallel, then merges
        candidates using per-list min-max normalization and weighted score fusion:

            final_score = image_weight * norm_image_score
                        + text_weight  * norm_text_score
                        + behavior_weight * behavior_score

        Fallback behaviour:
          - Image recall fails, text succeeds → text-only results, degraded=true,
            degradedReason=HYBRID_IMAGE_PATH_FAILED_TEXT_ONLY
          - Text recall fails, image succeeds → image-only results, degraded=true,
            degradedReason=HYBRID_TEXT_PATH_FAILED_IMAGE_ONLY
          - Both fail → 500 (gateway falls back to popularity)
        """
        t0 = time.perf_counter()

        try:
            if self.vision_handle is None:
                return JSONResponse(
                    {"error": "Vision search not available (VISION_ENABLED=0)", "status": "unavailable"},
                    status_code=503,
                )

            body = await request.json()
            query = str(body.get("query") or "").strip()
            image_base64 = body.get("image_base64")
            k = int(body.get("k", 10))
            request_id = str(uuid.uuid4())
            debug = bool(body.get("debug", False))

            if not query:
                return JSONResponse(
                    {"error": "query is required for hybrid search", "status": "error"},
                    status_code=400,
                )
            if not image_base64:
                return JSONResponse(
                    {"error": "image_base64 is required for hybrid search", "status": "error"},
                    status_code=400,
                )

            # Parse weights; normalize so they sum to 1 inside fuse_with_normalized_scores
            image_weight = max(0.0, float(body.get("image_weight", 0.5) or 0.5))
            text_weight = max(0.0, float(body.get("text_weight", 0.4) or 0.4))
            behavior_weight = max(0.0, float(body.get("behavior_weight", 0.1) or 0.0))
            user_id = str(body.get("user_id") or "").strip() or None

            # Recall more candidates than k so fusion has enough overlap to work with
            recall_k = min(RetrievalConfig.RECALL_K, max(50, k * 10))

            async def recall_text() -> list[dict]:
                vector = await asyncio.wait_for(
                    self.embedding_handle.embed.remote(query, is_query=True),
                    timeout=EmbeddingConfig.TIMEOUT_MS / 1000.0,
                )
                return await asyncio.wait_for(
                    self.retrieval_handle.search.remote(vector, candidate_k=recall_k, filters={}),
                    timeout=RetrievalConfig.TIMEOUT_MS / 1000.0,
                )

            async def recall_image() -> list[dict]:
                vision_body = {"mode": "image", "image_base64": image_base64, "k": recall_k}
                vision_result = await self.vision_handle.remote(vision_body)
                if vision_result.get("status") == "error":
                    raise RuntimeError(vision_result.get("error", "image recall failed"))
                return list(vision_result.get("items") or [])

            text_task = asyncio.create_task(recall_text())
            image_task = asyncio.create_task(recall_image())
            text_result, image_result = await asyncio.gather(
                text_task, image_task, return_exceptions=True
            )

            degraded_reasons: list[str] = []
            text_candidates: list[dict] = []
            image_candidates: list[dict] = []

            if isinstance(text_result, Exception):
                degraded_reasons.append("HYBRID_TEXT_PATH_FAILED_IMAGE_ONLY")
                logger.warning("hybrid text recall failed: %s", text_result)
            else:
                text_candidates = list(text_result)

            if isinstance(image_result, Exception):
                degraded_reasons.append("HYBRID_IMAGE_PATH_FAILED_TEXT_ONLY")
                logger.warning("hybrid image recall failed: %s", image_result)
            else:
                image_candidates = list(image_result)

            if not text_candidates and not image_candidates:
                return JSONResponse(
                    {
                        "error": "Both hybrid recall branches failed",
                        "status": "error",
                        "mode": "hybrid",
                        "request_id": request_id,
                    },
                    status_code=500,
                )

            # Min-max normalized weighted fusion
            merged_candidates = fuse_with_normalized_scores(
                text_candidates,
                image_candidates,
                limit=recall_k,
                image_weight=image_weight,
                text_weight=text_weight,
                behavior_weight=behavior_weight,
            )

            # Redis metadata enrichment
            metrics = self._get_metrics()
            results = await _enrich_and_filter(
                self.redis,
                merged_candidates,
                {},
                k,
                limit=recall_k,
                cache_hit_metric=metrics["CACHE_HIT_TOTAL"],
                cache_miss_metric=metrics["CACHE_MISS_TOTAL"],
            )

            # Annotate enriched results with fusion scores (keyed by canonical article_id)
            merged_by_id = {c["article_id"]: c for c in merged_candidates}
            for result in results:
                fused = merged_by_id.get(result.get("article_id")) or {}
                result["final_score"] = fused.get("final_score", fused.get("score"))
                result["image_score"] = fused.get("image_score")
                result["text_score"] = fused.get("text_score")
                result["behavior_score"] = fused.get("behavior_score", 0.0)
                result["candidate_sources"] = fused.get("candidate_sources", [])
                result["score"] = result["final_score"]

            # Post-fusion behavior boost via existing BehaviorBoost / FeatureReader path.
            # Snapshot load is offloaded to a thread with a hard 50 ms timeout so Redis
            # latency cannot affect the hybrid response budget. Any failure degrades
            # gracefully: behavior_score stays 0.0 and ordering is unchanged.
            if PersonalizationConfig.ENABLED and user_id and results:
                try:
                    candidate_item_ids = [
                        r.get("article_id") for r in results if r.get("article_id")
                    ]
                    snapshot = await asyncio.wait_for(
                        asyncio.to_thread(
                            self.feature_reader.load_personalization_snapshot,
                            user_id,
                            candidate_item_ids,
                            max_recent_clicks=PersonalizationConfig.MAX_RECENT_CLICKS_USED,
                        ),
                        timeout=0.050,
                    )
                    apply_behavior_boost_to_hybrid_results(
                        results, snapshot, self.behavior_boost
                    )
                    logger.debug(
                        "request_id=%s hybrid_behavior_boost user_id=%s degraded=%s",
                        request_id,
                        user_id,
                        snapshot.degraded,
                    )
                except asyncio.TimeoutError:
                    logger.warning(
                        "request_id=%s hybrid_behavior_boost_timeout user_id=%s -> no boost applied",
                        request_id,
                        user_id,
                    )
                except Exception as e:
                    logger.warning(
                        "request_id=%s hybrid_behavior_boost_failed user_id=%s error=%s -> no boost applied",
                        request_id,
                        user_id,
                        e,
                    )

            results, contract_debug = _contract_normalize(results, k)
            total_ms = (time.perf_counter() - t0) * 1000.0
            degraded = bool(degraded_reasons)

            dto_items = []
            for r in results:
                meta = r.get("meta") or {}
                price_val = meta.get("price")
                try:
                    price = float(price_val) if price_val not in (None, "") else 0.0
                except (TypeError, ValueError):
                    price = 0.0
                dto_items.append(
                    {
                        "itemId": str(r.get("article_id", "")).zfill(10),
                        "name": meta.get("title") or "",
                        "category": meta.get("dept") or "",
                        "description": meta.get("desc") or "",
                        "price": price,
                        "imgUrl": meta.get("image_url") or "",
                        "source": "hybrid",
                        "degraded": degraded,
                        "degradedReason": degraded_reasons[0] if len(degraded_reasons) == 1 else (
                            ",".join(degraded_reasons) if degraded_reasons else None
                        ),
                        "reason": meta.get("reason") or "",
                        "reasonSource": None,
                        # Hybrid-specific score fields
                        "finalScore": r.get("final_score"),
                        "imageScore": r.get("image_score"),
                        "textScore": r.get("text_score"),
                        "behaviorScore": r.get("behavior_score", 0.0),
                        "candidateSources": r.get("candidate_sources", []),
                    }
                )

            response: dict = {
                "items": dto_items,
                "k": k,
                "mode": "hybrid",
                "architecture": "dual_recall_normalized_fusion",
                "status": "success",
                "request_id": request_id,
                "latency_ms": total_ms,
                "degraded": degraded,
                "degraded_reason": ",".join(degraded_reasons) if degraded_reasons else None,
                "contract_debug": contract_debug,
            }

            if debug:
                response["debug"] = {
                    "hybrid": {
                        "recall_k": recall_k,
                        "text_candidates": len(text_candidates),
                        "image_candidates": len(image_candidates),
                        "merged_candidates": len(merged_candidates),
                        "weights": {
                            "image": image_weight,
                            "text": text_weight,
                            "behavior": behavior_weight,
                        },
                        "latency_ms": {"total": total_ms},
                    }
                }

            return JSONResponse(response)

        except Exception as e:
            logger.exception("Hybrid search failed: %s", e)
            return JSONResponse({"error": str(e), "status": "error"}, status_code=500)
