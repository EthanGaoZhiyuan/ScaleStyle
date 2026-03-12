import sys
from pathlib import Path
from types import ModuleType

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

observability_stub = ModuleType("src.utils.observability")
observability_stub.setup_tracing = lambda *args, **kwargs: None
sys.modules.setdefault("src.utils.observability", observability_stub)

from deployments.ingress import IngressDeployment  # noqa: E402
from tests.utils import FakeHandle  # noqa: E402


class _DummyRedis:
    def ping(self):
        return True


class _DummyEvent:
    def __init__(self, wait_results):
        self._wait_results = list(wait_results)

    def wait(self, timeout=0):
        if self._wait_results:
            return self._wait_results.pop(0)
        return True


class _DummyThread:
    def __init__(self, target=None, name=None, daemon=None):
        self.target = target
        self.name = name
        self.daemon = daemon
        self.started = False

    def start(self):
        self.started = True


def _build_ingress(monkeypatch):
    monkeypatch.setattr("deployments.ingress._redis_client", lambda: _DummyRedis())
    router = FakeHandle(
        route=lambda q, user_id=None: {
            "intent": "SEARCH",
            "filters": {},
            "flow": "smart",
        }
    )
    embed = FakeHandle(embed=lambda q, is_query=True: [0.1, 0.2, 0.3])
    retrieval = FakeHandle(search=lambda vector, **kw: [])
    popularity = FakeHandle(topk=lambda k: [])
    reranker = FakeHandle(
        score=lambda q, docs: {"scores": [], "rerank_ms": 1, "mode": "stub"}
    )
    generation = FakeHandle(
        explain=lambda q, item: {
            "reason": "test",
            "gen_ms": 1,
            "model": "stub",
            "device": "cpu",
        }
    )
    return IngressDeployment(router, embed, retrieval, popularity, reranker, generation)


def test_feature_reader_probe_thread_reference_clears_after_recovery(monkeypatch):
    ingress = _build_ingress(monkeypatch)
    ingress._feature_reader_probe_thread = object()
    ingress._probe_stop_event = _DummyEvent([False])
    monkeypatch.setattr(ingress, "_recover_feature_reader_if_due", lambda: True)

    ingress._feature_reader_probe_loop()

    assert ingress._feature_reader_probe_thread is None


def test_feature_reader_probe_can_restart_after_successful_recovery(monkeypatch):
    ingress = _build_ingress(monkeypatch)
    ingress._feature_reader_probe_thread = object()
    ingress._probe_stop_event = _DummyEvent([False])
    monkeypatch.setattr(ingress, "_recover_feature_reader_if_due", lambda: True)

    ingress._feature_reader_probe_loop()

    created_threads = []

    def _thread_factory(*args, **kwargs):
        thread = _DummyThread(*args, **kwargs)
        created_threads.append(thread)
        return thread

    monkeypatch.setattr("deployments.ingress.threading.Thread", _thread_factory)

    ingress._ensure_feature_reader_probe_thread_started()

    assert len(created_threads) == 1
    assert created_threads[0].started is True
    assert ingress._feature_reader_probe_thread is created_threads[0]
