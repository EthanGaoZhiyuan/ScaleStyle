import pytest
import sys
from types import ModuleType as _ModuleType
from unittest.mock import MagicMock


# Mock Ray Serve decorator to return the original class
def mock_serve_deployment(*args, **kwargs):
    """Mock @serve.deployment decorator, returns original class with bind method added"""

    def decorator(cls):
        cls._serve_deployment_options = dict(kwargs)

        # Add bind class method that returns a Mock object (simulates bound deployment)
        @classmethod
        def bind_method(c, *a, **kw):
            return MagicMock()  # Return mock instead of the class itself

        cls.bind = bind_method
        return cls

    if len(args) == 1 and callable(args[0]):
        # @serve.deployment (no parameters)
        cls = args[0]
        cls._serve_deployment_options = {}

        @classmethod
        def bind_method(c, *a, **kw):
            return MagicMock()

        cls.bind = bind_method
        return cls
    else:
        # @serve.deployment(...) (with parameters)
        return decorator


# Mock DeploymentHandle
class MockDeploymentHandle:
    pass


# Mock Ray before importing any modules
ray_mock = MagicMock()
ray_serve_mock = MagicMock()
ray_serve_handle_mock = MagicMock()

# Set mock attributes
ray_serve_mock.deployment = mock_serve_deployment
ray_serve_mock.ingress = lambda app: lambda cls: cls  # Mock @serve.ingress decorator
ray_serve_handle_mock.DeploymentHandle = MockDeploymentHandle

# Register mocks
sys.modules["ray"] = ray_mock
sys.modules["ray.serve"] = ray_serve_mock
sys.modules["ray.serve.handle"] = ray_serve_handle_mock

# Make ray.serve accessible to handle
ray_mock.serve = ray_serve_mock
ray_serve_mock.handle = ray_serve_handle_mock

# Pre-load real packages BEFORE any test file is collected.
# test_deployment_scaling_config.py uses sys.modules.setdefault() to stub these;
# pre-loading here means setdefault() finds them already present and skips the stub.
# src.utils.redis_client must be imported as a proper submodule so monkeypatch.setattr
# with the dotted-string form can resolve src.utils.redis_client.RedisClient.get_client.
import fastapi  # noqa: E402, F401
import fastapi.responses  # noqa: E402, F401
import starlette.requests  # noqa: E402, F401
import numpy  # noqa: E402, F401
import src.utils.redis_client  # noqa: E402, F401
import src.personalization  # noqa: E402, F401
import src.personalization.metrics  # noqa: E402, F401

# Mock opentelemetry instrumentation (used by ingress; avoids missing optional dep in tests)
sys.modules["opentelemetry.instrumentation"] = MagicMock()
sys.modules["opentelemetry.instrumentation.fastapi"] = MagicMock()

# Pre-seed src.utils.observability so test files that use sys.modules.setdefault() during
# collection (test_ingress_probe_recovery, test_ingress_request_errors) cannot replace it
# with a stub where setup_tracing returns None — which would corrupt later test imports.
_observability_stub = _ModuleType("src.utils.observability")
_observability_stub.setup_tracing = lambda *args, **kwargs: MagicMock()
sys.modules["src.utils.observability"] = _observability_stub

# Pre-import deployment modules that test_multimodal_search.py stubs via setdefault().
# Without these pre-imports, the stubs (with Deployment = object) would be installed
# before the test files that need the real classes are collected, causing AttributeError.


@pytest.fixture(autouse=True)
def reset_mocks():
    """Reset mocks after each test"""
    yield
