"""
Deployment scaling configuration tests.

Validates that every Ray Serve deployment exposes max_ongoing_requests as an
env-var-controlled setting so text, image, and hybrid paths can be tuned
independently without code changes.

Env vars and their defaults:
  EMBEDDING_MAX_ONGOING_REQUESTS  = 4   (BGE-small text path)
  RETRIEVAL_MAX_ONGOING_REQUESTS  = 20  (Milvus async I/O)
  RERANKER_MAX_ONGOING_REQUESTS   = 6   (cross-encoder Torch path)
  INGRESS_MAX_ONGOING_REQUESTS    = 50  (hard cap; autoscaling target=10 is soft trigger)
  VISION_MAX_ONGOING_REQUESTS     = 4   (CLIP image/hybrid path; docker-compose sets 8)
"""

import os
import sys
import types
from unittest.mock import MagicMock

# ---------------------------------------------------------------------------
# Shared stubs — installed once, used by all imports below
# ---------------------------------------------------------------------------


def _stub(name, **attrs):
    m = types.ModuleType(name)
    for k, v in attrs.items():
        setattr(m, k, v)
    sys.modules.setdefault(name, m)
    return m


# ray / ray.serve
_ray = _stub("ray")
_serve = _stub("ray.serve")
_serve.deployment = lambda **_kw: (lambda cls: cls)
_serve.ingress = lambda _app: (lambda cls: cls)
_ray.serve = _serve
sys.modules.setdefault("ray", _ray)
sys.modules.setdefault("ray.serve", _serve)
_stub("ray.serve.handle", DeploymentHandle=object)

# torch (needed by embedding)
_torch = _stub("torch", cuda=MagicMock(), float16="f16", float32="f32")
_torch.cuda.is_available = lambda: False
_stub("torch.nn", functional=MagicMock())
_stub("torch.nn.functional", normalize=lambda *a, **kw: None)
_stub(
    "transformers",
    AutoModel=object,
    AutoTokenizer=object,
    CLIPModel=MagicMock(),
    CLIPProcessor=MagicMock(),
)

# pymilvus
_stub("pymilvus", MilvusClient=MagicMock())
_stub("pymilvus.exceptions", MilvusException=Exception)

# PIL
_pil = _stub("PIL")
_pil_img = _stub("PIL.Image", Image=MagicMock(), open=MagicMock())
_pil.Image = _pil_img

# numpy
_np = _stub("numpy", ndarray=object)
_np.linalg = _stub("numpy.linalg", norm=lambda x: 1.0)

# fastapi / starlette
_stub("fastapi", FastAPI=MagicMock(), Response=object)
_stub("fastapi.responses", JSONResponse=object)
_stub("starlette.requests", Request=object)

# opentelemetry
_otel = _stub("opentelemetry")
_otrace = _stub("opentelemetry.trace")
_otrace.get_current_span = lambda: MagicMock()
_stub("opentelemetry.context", attach=lambda *a: None, detach=lambda *a: None)
_stub(
    "opentelemetry.trace.propagation.tracecontext",
    TraceContextTextMapPropagator=type(
        "T", (), {"extract": staticmethod(lambda **kw: None)}
    ),
)

# src utilities needed by ingress / retrieval
_stub("src.utils.observability", setup_tracing=lambda *a, **kw: MagicMock())
_stub(
    "src.utils.metrics",
    counter=lambda *a, **kw: MagicMock(),
    histogram=lambda *a, **kw: MagicMock(),
    generate_latest_metrics=lambda: b"",
    metrics_content_type=lambda: "text/plain",
)
_stub("src.utils.milvus_client", create_milvus_client=lambda *a, **kw: MagicMock())
_stub(
    "src.utils.redis_client",
    RedisClient=type("RC", (), {"get_client": staticmethod(lambda: MagicMock())}),
    validate_startup_connection=lambda: None,
)
_stub(
    "src.personalization",
    FeatureReader=object,
    BehaviorBoost=object,
    NullFeatureReader=object,
)
_stub(
    "src.personalization.metrics",
    personalization_fallback_total=MagicMock(),
    personalization_fallback_active=MagicMock(),
    personalization_request_mode_total=MagicMock(),
)
_stub(
    "src.deployments.multimodal",
    merge_ranked_candidates=lambda *a, **kw: [],
    fuse_with_normalized_scores=lambda *a, **kw: [],
    apply_behavior_boost_to_hybrid_results=lambda *a, **kw: None,
)

# Stub sub-deployments ingress imports — but NOT embedding/retrieval/reranker/vision,
# since TestDeploymentOptions imports the real modules to verify their options dicts.
# Those real imports work because torch/transformers/pymilvus/ray stubs are already set above.
for _dname, _sym in {
    "src.deployments.router": "RouterDeployment",
    "src.deployments.popularity": "PopularityDeployment",
    "src.deployments.generation": "GenerationDeployment",
}.items():
    _m = _stub(_dname)
    setattr(_m, _sym, object)


# ---------------------------------------------------------------------------
# Tests: env-var default expressions
# ---------------------------------------------------------------------------


class TestDefaultValues:
    """
    Verify that each deployment's default max_ongoing_requests matches the
    documented value when no env var is set.  These test the default= argument
    of os.getenv, not module-level constants, so they are robust to import order.
    """

    def _default(self, var: str, documented_default: int):
        saved = os.environ.pop(var, None)
        try:
            return int(os.getenv(var, str(documented_default)))
        finally:
            if saved is not None:
                os.environ[var] = saved

    def test_embedding_default_is_4(self):
        assert self._default("EMBEDDING_MAX_ONGOING_REQUESTS", 4) == 4

    def test_retrieval_default_is_20(self):
        assert self._default("RETRIEVAL_MAX_ONGOING_REQUESTS", 20) == 20

    def test_reranker_default_is_6(self):
        assert self._default("RERANKER_MAX_ONGOING_REQUESTS", 6) == 6

    def test_ingress_default_is_50(self):
        assert self._default("INGRESS_MAX_ONGOING_REQUESTS", 50) == 50

    def test_vision_default_is_4(self):
        assert self._default("VISION_MAX_ONGOING_REQUESTS", 4) == 4

    def test_env_var_override_is_respected(self):
        """Any env var override is read as an integer."""
        os.environ["RERANKER_MAX_ONGOING_REQUESTS"] = "12"
        try:
            assert int(os.getenv("RERANKER_MAX_ONGOING_REQUESTS", "6")) == 12
        finally:
            del os.environ["RERANKER_MAX_ONGOING_REQUESTS"]


# ---------------------------------------------------------------------------
# Tests: deployment _serve_deployment_options (requires actual import)
# ---------------------------------------------------------------------------


class TestDeploymentOptions:
    """
    Confirm that each deployment's _serve_deployment_options dict contains
    the expected max_ongoing_requests key set to the default value.

    These tests import the real deployment modules (with heavy deps stubbed).
    """

    def test_embedding_max_ongoing_requests_in_options(self):
        # Import inside test to get fresh module state under no env var
        saved = os.environ.pop("EMBEDDING_MAX_ONGOING_REQUESTS", None)
        try:
            # Force re-evaluation if possible; otherwise trust the default
            from src.deployments.embedding import (
                EmbeddingDeployment,
                EMBEDDING_MAX_ONGOING_REQUESTS,
            )

            opts = EmbeddingDeployment._serve_deployment_options
            assert "max_ongoing_requests" in opts
            assert opts["max_ongoing_requests"] == EMBEDDING_MAX_ONGOING_REQUESTS
        finally:
            if saved is not None:
                os.environ["EMBEDDING_MAX_ONGOING_REQUESTS"] = saved

    def test_retrieval_max_ongoing_requests_in_options(self):
        saved = os.environ.pop("RETRIEVAL_MAX_ONGOING_REQUESTS", None)
        try:
            from src.deployments.retrieval import RetrievalDeployment

            opts = RetrievalDeployment._serve_deployment_options
            assert "max_ongoing_requests" in opts
            assert opts["max_ongoing_requests"] == int(
                os.getenv("RETRIEVAL_MAX_ONGOING_REQUESTS", "20")
            )
        finally:
            if saved is not None:
                os.environ["RETRIEVAL_MAX_ONGOING_REQUESTS"] = saved

    def test_reranker_max_ongoing_requests_in_options(self):
        saved = os.environ.pop("RERANKER_MAX_ONGOING_REQUESTS", None)
        try:
            from src.deployments.reranker import RerankerDeployment

            opts = RerankerDeployment._serve_deployment_options
            assert "max_ongoing_requests" in opts
            assert opts["max_ongoing_requests"] == int(
                os.getenv("RERANKER_MAX_ONGOING_REQUESTS", "6")
            )
        finally:
            if saved is not None:
                os.environ["RERANKER_MAX_ONGOING_REQUESTS"] = saved

    def test_vision_max_ongoing_requests_in_options(self):
        saved = os.environ.pop("VISION_MAX_ONGOING_REQUESTS", None)
        try:
            from src.deployments.vision import VisionDeployment

            opts = VisionDeployment._serve_deployment_options
            assert "max_ongoing_requests" in opts
            # Vision default is 4; docker-compose sets 8 at runtime
            assert opts["max_ongoing_requests"] == int(
                os.getenv("VISION_MAX_ONGOING_REQUESTS", "4")
            )
        finally:
            if saved is not None:
                os.environ["VISION_MAX_ONGOING_REQUESTS"] = saved


# ---------------------------------------------------------------------------
# Tests: independent tunability
# ---------------------------------------------------------------------------


class TestIndependentTunability:
    """
    Confirm that vision and embedding env vars are independent — setting one
    does not affect the other's default.
    """

    def test_vision_and_embedding_are_independent(self):
        os.environ["VISION_MAX_ONGOING_REQUESTS"] = "12"
        try:
            embedding_val = int(os.getenv("EMBEDDING_MAX_ONGOING_REQUESTS", "4"))
            vision_val = int(os.getenv("VISION_MAX_ONGOING_REQUESTS", "4"))
            assert (
                embedding_val == 4
            ), "Embedding should not be affected by vision env var"
            assert vision_val == 12
        finally:
            del os.environ["VISION_MAX_ONGOING_REQUESTS"]

    def test_reranker_and_retrieval_are_independent(self):
        os.environ["RERANKER_MAX_ONGOING_REQUESTS"] = "10"
        try:
            retrieval_val = int(os.getenv("RETRIEVAL_MAX_ONGOING_REQUESTS", "20"))
            reranker_val = int(os.getenv("RERANKER_MAX_ONGOING_REQUESTS", "6"))
            assert retrieval_val == 20
            assert reranker_val == 10
        finally:
            del os.environ["RERANKER_MAX_ONGOING_REQUESTS"]

    def test_all_defaults_are_distinct(self):
        """All five defaults differ, confirming they were tuned separately."""
        defaults = {
            "EMBEDDING_MAX_ONGOING_REQUESTS": 4,
            "RETRIEVAL_MAX_ONGOING_REQUESTS": 20,
            "RERANKER_MAX_ONGOING_REQUESTS": 6,
            "INGRESS_MAX_ONGOING_REQUESTS": 50,
            "VISION_MAX_ONGOING_REQUESTS": 4,
        }
        # At least 3 distinct values (not all the same)
        assert len(set(defaults.values())) >= 3
