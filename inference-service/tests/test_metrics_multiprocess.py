import asyncio
import importlib
import sys
import types
from types import SimpleNamespace

import pytest

from tests.utils import FakeHandle


@pytest.fixture
def metrics_module():
    module = importlib.import_module("src.utils.metrics")
    original_registry = dict(module._REGISTRY)
    module._REGISTRY.clear()
    yield module
    module._REGISTRY.clear()
    module._REGISTRY.update(original_registry)


def test_gauge_uses_livesum_mode_in_multiprocess(metrics_module, monkeypatch):
    captured = {}

    class FakeGauge:
        def __init__(self, name, doc, labels, **kwargs):
            captured["name"] = name
            captured["doc"] = doc
            captured["labels"] = labels
            captured["kwargs"] = kwargs

    monkeypatch.setattr(metrics_module, "_is_multiprocess_enabled", lambda: True)
    monkeypatch.setattr(metrics_module, "Gauge", FakeGauge)

    gauge = metrics_module.gauge("test_active", "doc")

    assert gauge is metrics_module._REGISTRY["test_active"]
    assert captured["kwargs"]["multiprocess_mode"] == "livesum"


def test_generate_latest_metrics_uses_multiprocess_collector(metrics_module, monkeypatch):
    collector_calls = []
    generated = []

    class FakeRegistry:
        pass

    fake_registry = FakeRegistry()

    monkeypatch.setattr(metrics_module, "_is_multiprocess_enabled", lambda: True)
    monkeypatch.setattr(metrics_module, "CollectorRegistry", lambda: fake_registry)
    monkeypatch.setattr(
        metrics_module.multiprocess,
        "MultiProcessCollector",
        lambda registry: collector_calls.append(registry),
    )
    monkeypatch.setattr(
        metrics_module,
        "generate_latest",
        lambda registry: generated.append(registry) or b"aggregated-metrics",
    )

    payload = metrics_module.generate_latest_metrics()

    assert payload == b"aggregated-metrics"
    assert collector_calls == [fake_registry]
    assert generated == [fake_registry]


def test_ingress_metrics_endpoint_exports_shared_metrics(monkeypatch):
    class DummyRedis:
        def ping(self):
            return True

    observability_stub = types.ModuleType("src.utils.observability")
    observability_stub.setup_tracing = lambda service_name: object()
    monkeypatch.setitem(sys.modules, "src.utils.observability", observability_stub)

    monkeypatch.setattr("src.deployments.ingress._redis_client", lambda: DummyRedis())
    monkeypatch.setattr("src.deployments.ingress.generate_latest_metrics", lambda: b"shared-metrics")

    from src.deployments.ingress import IngressDeployment

    router = FakeHandle(route=lambda q, user_id=None: {"intent": "SEARCH", "filters": {}, "flow": "smart"})
    embed = FakeHandle(embed=lambda q, is_query=True: [0.1, 0.2, 0.3])
    retrieval = FakeHandle(search=lambda vector, **kw: [])
    popularity = FakeHandle(topk=lambda k: [])
    reranker = FakeHandle(score=lambda q, docs: {"scores": [], "rerank_ms": 1, "mode": "stub"})
    generation = FakeHandle(explain=lambda q, item: {"reason": "test", "gen_ms": 1, "model": "stub", "device": "cpu"})

    deployment = IngressDeployment(router, embed, retrieval, popularity, reranker, generation)
    request = SimpleNamespace(url=SimpleNamespace(path="/metrics"), headers={}, method="GET")

    response = asyncio.run(deployment(request))

    assert response.body == b"shared-metrics"
