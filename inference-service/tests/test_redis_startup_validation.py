import importlib
import sys
import types
from unittest.mock import Mock

import pytest

from src.utils import redis_client as redis_client_module


@pytest.fixture(autouse=True)
def reset_redis_client_singleton():
    original_client = redis_client_module.RedisClient._client
    yield
    redis_client_module.RedisClient._client = original_client


def test_validate_startup_connection_pings_shared_client(monkeypatch, caplog):
    client = Mock()
    get_client = Mock(return_value=client)

    monkeypatch.setattr(redis_client_module.RedisClient, "get_client", get_client)

    with caplog.at_level("INFO"):
        returned_client = redis_client_module.validate_startup_connection()

    assert returned_client is client
    get_client.assert_called_once_with()
    client.ping.assert_called_once_with()
    assert "Redis startup validation succeeded" in caplog.text


def test_validate_startup_connection_logs_error_and_raises(monkeypatch, caplog):
    client = Mock()
    client.ping.side_effect = RuntimeError("redis unavailable")

    monkeypatch.setattr(
        redis_client_module.RedisClient, "get_client", Mock(return_value=client)
    )

    with caplog.at_level("ERROR"):
        with pytest.raises(RuntimeError, match="redis unavailable"):
            redis_client_module.validate_startup_connection()

    assert "Redis startup validation failed" in caplog.text


def test_server_boot_validates_redis_before_starting_ray(monkeypatch):
    class _Deployment:
        @classmethod
        def bind(cls, *args, **kwargs):
            return object()

    module_stubs = {
        "src.deployments.router": "RouterDeployment",
        "src.deployments.embedding": "EmbeddingDeployment",
        "src.deployments.retrieval": "RetrievalDeployment",
        "src.deployments.ingress": "IngressDeployment",
        "src.deployments.popularity": "PopularityDeployment",
        "src.deployments.reranker": "RerankerDeployment",
        "src.deployments.generation": "GenerationDeployment",
    }

    original_modules = {}
    for module_name, symbol_name in module_stubs.items():
        original_modules[module_name] = sys.modules.get(module_name)
        stub_module = types.ModuleType(module_name)
        setattr(stub_module, symbol_name, _Deployment)
        sys.modules[module_name] = stub_module

    validate_calls = []
    ray_init_calls = []
    serve_start_calls = []
    serve_run_calls = []

    monkeypatch.setattr(
        "src.utils.redis_client.validate_startup_connection",
        lambda: validate_calls.append("validated"),
    )
    monkeypatch.setattr(
        "ray.init", lambda *args, **kwargs: ray_init_calls.append((args, kwargs))
    )
    monkeypatch.setattr(
        "ray.serve.start",
        lambda *args, **kwargs: serve_start_calls.append((args, kwargs)),
    )
    monkeypatch.setattr(
        "ray.serve.run", lambda *args, **kwargs: serve_run_calls.append((args, kwargs))
    )

    sys.modules.pop("src.server", None)
    try:
        importlib.import_module("src.server")
    finally:
        sys.modules.pop("src.server", None)
        for module_name, original_module in original_modules.items():
            if original_module is None:
                sys.modules.pop(module_name, None)
            else:
                sys.modules[module_name] = original_module

    assert validate_calls == ["validated"]
    assert len(ray_init_calls) == 1
    assert len(serve_start_calls) == 1
    assert len(serve_run_calls) == 1
