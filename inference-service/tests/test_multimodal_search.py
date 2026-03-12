import asyncio
import sys
import types
from contextlib import nullcontext

from tests.utils import FakeHandle


def _install_ingress_import_shims() -> None:
    tracecontext_module = types.ModuleType("opentelemetry.trace.propagation.tracecontext")
    tracecontext_module.TraceContextTextMapPropagator = type(
        "TraceContextTextMapPropagator",
        (),
        {"extract": staticmethod(lambda carrier=None: None)},
    )

    context_module = types.ModuleType("opentelemetry.context")
    context_module.attach = lambda context: None
    context_module.detach = lambda token: None

    span_context = type(
        "SpanContext",
        (),
        {"is_valid": True, "trace_id": 1},
    )()
    span = type("Span", (), {"get_span_context": lambda self: span_context})()
    trace_module = types.ModuleType("opentelemetry.trace")
    trace_module.get_current_span = lambda: span

    opentelemetry_module = types.ModuleType("opentelemetry")
    opentelemetry_module.trace = trace_module

    observability_module = types.ModuleType("src.utils.observability")
    metrics_module = types.ModuleType("src.utils.metrics")
    personalization_module = types.ModuleType("src.personalization")
    personalization_metrics_module = types.ModuleType("src.personalization.metrics")
    deployment_stub_names = {
        "src.deployments.router": "RouterDeployment",
        "src.deployments.embedding": "EmbeddingDeployment",
        "src.deployments.retrieval": "RetrievalDeployment",
        "src.deployments.popularity": "PopularityDeployment",
        "src.deployments.reranker": "RerankerDeployment",
        "src.deployments.generation": "GenerationDeployment",
    }

    class DummyTracer:
        def start_as_current_span(self, *_args, **_kwargs):
            return nullcontext(type("Span", (), {"set_attribute": lambda self, *a, **k: None})())

    class DummyMetric:
        def labels(self, **_kwargs):
            return self

        def inc(self, *_args, **_kwargs):
            return None

        def observe(self, *_args, **_kwargs):
            return None

        def set(self, *_args, **_kwargs):
            return None

    observability_module.setup_tracing = lambda _service_name=None: DummyTracer()
    metrics_module.counter = lambda *_args, **_kwargs: DummyMetric()
    metrics_module.histogram = lambda *_args, **_kwargs: DummyMetric()
    metrics_module.generate_latest_metrics = lambda: b""
    metrics_module.metrics_content_type = lambda: "text/plain"
    personalization_module.__path__ = []
    personalization_module.FeatureReader = object
    personalization_module.BehaviorBoost = object
    personalization_module.NullFeatureReader = type("NullFeatureReader", (), {})
    personalization_metrics_module.personalization_fallback_total = DummyMetric()
    personalization_metrics_module.personalization_fallback_active = DummyMetric()
    personalization_metrics_module.personalization_request_mode_total = DummyMetric()

    sys.modules.setdefault("opentelemetry", opentelemetry_module)
    sys.modules.setdefault("opentelemetry.trace", trace_module)
    sys.modules.setdefault("opentelemetry.context", context_module)
    sys.modules.setdefault(
        "opentelemetry.trace.propagation.tracecontext", tracecontext_module
    )
    sys.modules.setdefault("src.utils.observability", observability_module)
    sys.modules.setdefault("src.utils.metrics", metrics_module)
    sys.modules.setdefault("src.personalization", personalization_module)
    sys.modules.setdefault("src.personalization.metrics", personalization_metrics_module)
    for module_name, symbol_name in deployment_stub_names.items():
        module = types.ModuleType(module_name)
        setattr(module, symbol_name, object)
        sys.modules.setdefault(module_name, module)


_install_ingress_import_shims()


class FakeRequest:
    def __init__(self, payload: dict):
        self._payload = payload

    async def json(self):
        return self._payload


def test_multimodal_handler_uses_dual_recall_merge_rerank(monkeypatch):
    class DummyRedis:
        def ping(self):
            return True

        def pipeline(self):
            class Pipe:
                def __init__(self):
                    self.keys = []

                def hgetall(self, key):
                    self.keys.append(key)

                def execute(self, **kwargs):
                    out = []
                    for key in self.keys:
                        article_id = key.split(":")[-1]
                        out.append(
                            {
                                "article_id": article_id,
                                "prod_name": f"Product {article_id}",
                                "product_type_name": "dress",
                                "department_name": "Ladieswear",
                                "colour_group_name": "Red",
                                "detail_desc": f"Description for {article_id}",
                                "image_url": f"/images/{article_id}.jpg",
                                "price": "0.1",
                            }
                        )
                    return out

            return Pipe()

    monkeypatch.setattr("src.deployments.ingress._redis_client", lambda: DummyRedis())

    from src.deployments.ingress import IngressDeployment

    calls = {"text": 0, "image": 0, "rerank": 0}

    async def vision_remote(body):
        if body.get("mode") == "image":
            calls["image"] += 1
            return {
                "status": "success",
                "mode": "image",
                "items": [
                    {"article_id": "b", "score": 0.95},
                    {"article_id": "c", "score": 0.80},
                ],
            }
        raise AssertionError(f"unexpected vision mode: {body}")

    def retrieval_search(vector, **kwargs):
        calls["text"] += 1
        return [
            {"article_id": "a", "score": 0.91},
            {"article_id": "b", "score": 0.88},
        ]

    def rerank_score(query, docs):
        calls["rerank"] += 1
        assert query == "red dress"
        return {
            "scores": [0.2, 0.9, 0.1][: len(docs)],
            "rerank_ms": 5.0,
            "mode": "stub",
        }

    ingress = IngressDeployment(
        FakeHandle(route=lambda q, user_id=None: {"intent": "SEARCH", "filters": {}, "flow": "smart"}),
        FakeHandle(embed=lambda q, is_query=True: [0.1, 0.2, 0.3]),
        FakeHandle(search=retrieval_search),
        FakeHandle(topk=lambda k: []),
        FakeHandle(score=rerank_score),
        FakeHandle(explain=lambda q, item: {}),
        FakeHandle(__call__=vision_remote),
    )
    ingress.vision_handle.remote = vision_remote

    response = asyncio.run(
        ingress._image_search_handler(
            FakeRequest(
                {
                    "mode": "multimodal",
                    "query": "red dress",
                    "image_base64": "ZmFrZQ==",
                    "k": 3,
                    "debug": True,
                }
            )
        )
    )

    payload = response.body
    if isinstance(payload, bytes):
        import json

        payload = json.loads(payload)
    assert response.status_code == 200
    assert payload["mode"] == "multimodal"
    assert payload["architecture"] == "dual_recall_merge_rerank"
    assert len(payload["items"]) == 3
    merged_item = next(item for item in payload["items"] if item["article_id"] == "b")
    assert sorted(merged_item["candidate_sources"]) == ["image", "text"]
    assert calls == {"text": 1, "image": 1, "rerank": 1}
    assert payload["debug"]["multimodal"]["merged_candidates"] == 3


def test_merge_ranked_candidates_dedupes_and_tracks_sources():
    from src.deployments.multimodal import merge_ranked_candidates

    merged = merge_ranked_candidates(
        [{"article_id": "a", "score": 0.9}, {"article_id": "b", "score": 0.8}],
        [{"article_id": "b", "score": 0.95}, {"article_id": "c", "score": 0.7}],
        limit=5,
        text_weight=0.7,
        image_weight=0.3,
    )

    assert [item["article_id"] for item in merged] == ["b", "a", "c"]
    assert merged[0]["candidate_sources"] == ["text", "image"]
    assert merged[0]["source_ranks"] == {"text": 2, "image": 1}