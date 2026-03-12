import asyncio
import json
import sys
from pathlib import Path
from types import ModuleType, SimpleNamespace

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

observability_stub = ModuleType("src.utils.observability")
observability_stub.setup_tracing = lambda *args, **kwargs: None
sys.modules.setdefault("src.utils.observability", observability_stub)

from deployments.ingress import IngressDeployment
from tests.utils import FakeHandle


class _DummyRedis:
    def ping(self):
        return True


class _DummyRequest:
    def __init__(self, payload=None, error=None):
        self.url = SimpleNamespace(path="/")
        self.headers = {}
        self.method = "POST"
        self._payload = payload
        self._error = error

    async def json(self):
        if self._error is not None:
            raise self._error
        return self._payload


def _build_ingress(monkeypatch):
    monkeypatch.setattr("deployments.ingress._redis_client", lambda: _DummyRedis())
    router = FakeHandle(route=lambda q, user_id=None: {"intent": "SEARCH", "filters": {}, "flow": "smart"})
    embed = FakeHandle(embed=lambda q, is_query=True: [0.1, 0.2, 0.3])
    retrieval = FakeHandle(search=lambda vector, **kw: [])
    popularity = FakeHandle(topk=lambda k: [])
    reranker = FakeHandle(score=lambda q, docs: {"scores": [], "rerank_ms": 1, "mode": "stub"})
    generation = FakeHandle(explain=lambda q, item: {"reason": "test", "gen_ms": 1, "model": "stub", "device": "cpu"})
    return IngressDeployment(router, embed, retrieval, popularity, reranker, generation)


def test_post_search_returns_422_for_validation_error(monkeypatch):
    ingress = _build_ingress(monkeypatch)

    response = asyncio.run(ingress(_DummyRequest(payload={"query": "dress", "k": 0})))

    assert response.status_code == 422
    assert response.body == b'{"error":"invalid_request"}'


def test_post_search_returns_422_for_malformed_json(monkeypatch):
    ingress = _build_ingress(monkeypatch)

    response = asyncio.run(
        ingress(_DummyRequest(error=json.JSONDecodeError("Expecting value", "{", 0)))
    )

    assert response.status_code == 422
    assert response.body == b'{"error":"invalid_request"}'


def test_post_search_returns_generic_500_for_internal_error(monkeypatch):
    ingress = _build_ingress(monkeypatch)

    async def _raise_internal(_request):
        raise RuntimeError("redis password leaked")

    ingress._search_impl = _raise_internal

    response = asyncio.run(ingress(_DummyRequest(payload={"query": "dress", "k": 1})))

    assert response.status_code == 500
    assert response.body == b'{"error":"internal_error"}'