import pytest
import sys
from unittest.mock import MagicMock

# Mock Ray Serve decorator to return the original class
def mock_serve_deployment(*args, **kwargs):
    """Mock @serve.deployment decorator, returns original class with bind method added"""
    def decorator(cls):
        # Add bind class method that returns a Mock object (simulates bound deployment)
        @classmethod
        def bind_method(c, *a, **kw):
            return MagicMock()  # Return mock instead of the class itself
        cls.bind = bind_method
        return cls
    
    if len(args) == 1 and callable(args[0]):
        # @serve.deployment (no parameters)
        cls = args[0]
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
sys.modules['ray'] = ray_mock
sys.modules['ray.serve'] = ray_serve_mock
sys.modules['ray.serve.handle'] = ray_serve_handle_mock

# Make ray.serve accessible to handle
ray_mock.serve = ray_serve_mock
ray_serve_mock.handle = ray_serve_handle_mock


@pytest.fixture(autouse=True)
def reset_mocks():
    """Reset mocks after each test"""
    yield