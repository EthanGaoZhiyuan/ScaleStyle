"""
H-1: VisionDeployment async interface tests.

Verifies that:
1. All public entry points (__call__, search_by_image, search_by_text,
   search_multimodal) are coroutine functions.
2. Each async method delegates CPU/IO work via asyncio.to_thread so the
   Ray Serve event loop stays free during CLIP inference and Milvus search.
3. The _*_sync variants contain the actual computation and are not coroutines.

Tests use mocks and avoid importing torch/transformers/pymilvus.
"""

import asyncio
import inspect
import sys
import types
from unittest.mock import AsyncMock, MagicMock, patch

import pytest


# ---------------------------------------------------------------------------
# Stub out heavy optional dependencies so vision.py can be imported
# ---------------------------------------------------------------------------

def _install_vision_stubs():
    # torch stub
    torch_mod = types.ModuleType("torch")
    torch_mod.no_grad = MagicMock(return_value=MagicMock(
        __enter__=lambda s, *a: s,
        __exit__=lambda s, *a: None,
    ))
    torch_mod.cuda = MagicMock()
    torch_mod.cuda.is_available = lambda: False
    sys.modules.setdefault("torch", torch_mod)

    # transformers stub
    transformers_mod = types.ModuleType("transformers")
    transformers_mod.CLIPModel = MagicMock()
    transformers_mod.CLIPProcessor = MagicMock()
    sys.modules.setdefault("transformers", transformers_mod)

    # pymilvus stub
    pymilvus_mod = types.ModuleType("pymilvus")
    pymilvus_mod.MilvusClient = MagicMock()
    sys.modules.setdefault("pymilvus", pymilvus_mod)

    # PIL stub
    pil_mod = types.ModuleType("PIL")
    pil_image_mod = types.ModuleType("PIL.Image")
    pil_image_mod.Image = MagicMock()
    pil_image_mod.open = MagicMock()
    pil_mod.Image = pil_image_mod
    sys.modules.setdefault("PIL", pil_mod)
    sys.modules.setdefault("PIL.Image", pil_image_mod)

    # numpy stub — only need ndarray and linalg.norm
    np_mod = types.ModuleType("numpy")
    np_mod.ndarray = object
    linalg = types.ModuleType("numpy.linalg")
    linalg.norm = lambda x: 1.0
    np_mod.linalg = linalg
    sys.modules.setdefault("numpy", np_mod)

    # ray.serve stub
    ray_mod = types.ModuleType("ray")
    serve_mod = types.ModuleType("ray.serve")

    def deployment_decorator(**_kwargs):
        return lambda cls: cls

    serve_mod.deployment = deployment_decorator
    ray_mod.serve = serve_mod
    sys.modules.setdefault("ray", ray_mod)
    sys.modules.setdefault("ray.serve", serve_mod)


_install_vision_stubs()

# Now import the module under test
from src.deployments.vision import VisionDeployment, _ImageEmbeddingCache  # noqa: E402


# ---------------------------------------------------------------------------
# Minimal VisionDeployment instance (bypasses __init__)
# ---------------------------------------------------------------------------

def _make_vision() -> VisionDeployment:
    """Return a VisionDeployment instance without running __init__."""
    obj = object.__new__(VisionDeployment)
    obj.model_name = "stub"
    obj.device = "cpu"
    obj.model = MagicMock()
    obj.processor = MagicMock()
    obj.milvus_client = MagicMock()
    obj.collection_name = "stub_collection"
    obj.vector_field = "stub_field"
    obj.nprobe = 10
    obj._image_cache = _ImageEmbeddingCache(max_size=8, ttl_seconds=60.0)
    return obj


# ---------------------------------------------------------------------------
# 1. Interface: all public methods are coroutines
# ---------------------------------------------------------------------------

class TestVisionAsyncInterface:
    def test_call_is_coroutine_function(self):
        assert inspect.iscoroutinefunction(VisionDeployment.__call__), (
            "__call__ must be async def so Ray Serve runs it on the event loop"
        )

    def test_search_by_image_is_coroutine_function(self):
        assert inspect.iscoroutinefunction(VisionDeployment.search_by_image)

    def test_search_by_text_is_coroutine_function(self):
        assert inspect.iscoroutinefunction(VisionDeployment.search_by_text)

    def test_search_multimodal_is_coroutine_function(self):
        assert inspect.iscoroutinefunction(VisionDeployment.search_multimodal)

    def test_sync_bodies_are_not_coroutines(self):
        """The _*_sync variants must be plain functions (run inside to_thread)."""
        assert not inspect.iscoroutinefunction(VisionDeployment._search_by_image_sync)
        assert not inspect.iscoroutinefunction(VisionDeployment._search_by_text_sync)
        assert not inspect.iscoroutinefunction(VisionDeployment._search_multimodal_sync)


# ---------------------------------------------------------------------------
# 2. Delegation: async wrappers use asyncio.to_thread
# ---------------------------------------------------------------------------

class TestVisionToThreadDelegation:
    """
    Each async method must hand off the blocking work to a thread via
    asyncio.to_thread so the event loop isn't blocked.
    """

    def _run(self, coro):
        return asyncio.get_event_loop().run_until_complete(coro)

    def test_search_by_image_uses_to_thread(self):
        vision = _make_vision()
        sentinel = {"items": [], "status": "success", "mode": "image", "query_time_ms": 1.0}
        with patch("asyncio.to_thread", new=AsyncMock(return_value=sentinel)) as mock_to_thread:
            result = self._run(vision.search_by_image({"image_base64": "ZmFrZQ=="}))
        mock_to_thread.assert_awaited_once()
        first_arg = mock_to_thread.call_args[0][0]
        assert first_arg == vision._search_by_image_sync, (
            "search_by_image must delegate to _search_by_image_sync via to_thread"
        )
        assert result == sentinel

    def test_search_by_text_uses_to_thread(self):
        vision = _make_vision()
        sentinel = {"items": [], "status": "success", "mode": "text_to_image", "query_time_ms": 1.0}
        with patch("asyncio.to_thread", new=AsyncMock(return_value=sentinel)) as mock_to_thread:
            result = self._run(vision.search_by_text({"query": "red dress"}))
        mock_to_thread.assert_awaited_once()
        first_arg = mock_to_thread.call_args[0][0]
        assert first_arg == vision._search_by_text_sync, (
            "search_by_text must delegate to _search_by_text_sync via to_thread"
        )
        assert result == sentinel

    def test_search_multimodal_uses_to_thread(self):
        vision = _make_vision()
        sentinel = {"items": [], "status": "success", "mode": "multimodal", "query_time_ms": 1.0}
        with patch("asyncio.to_thread", new=AsyncMock(return_value=sentinel)) as mock_to_thread:
            result = self._run(vision.search_multimodal({"query": "dress"}))
        mock_to_thread.assert_awaited_once()
        first_arg = mock_to_thread.call_args[0][0]
        assert first_arg == vision._search_multimodal_sync, (
            "search_multimodal must delegate to _search_multimodal_sync via to_thread"
        )
        assert result == sentinel


# ---------------------------------------------------------------------------
# 3. Routing: __call__ dispatches to the correct async method
# ---------------------------------------------------------------------------

class TestVisionCallRouting:
    def _run(self, coro):
        return asyncio.get_event_loop().run_until_complete(coro)

    def _make_sentinel(self, mode: str) -> dict:
        return {"items": [], "status": "success", "mode": mode, "query_time_ms": 1.0}

    def test_call_image_mode_routes_to_search_by_image(self):
        vision = _make_vision()
        sentinel = self._make_sentinel("image")
        vision.search_by_image = AsyncMock(return_value=sentinel)
        result = self._run(vision({"mode": "image", "image_base64": "ZmFrZQ=="}))
        vision.search_by_image.assert_awaited_once()
        assert result == sentinel

    def test_call_image_to_image_alias_routes_to_search_by_image(self):
        vision = _make_vision()
        sentinel = self._make_sentinel("image")
        vision.search_by_image = AsyncMock(return_value=sentinel)
        result = self._run(vision({"mode": "image_to_image", "image_base64": "ZmFrZQ=="}))
        vision.search_by_image.assert_awaited_once()

    def test_call_text_to_image_routes_to_search_by_text(self):
        vision = _make_vision()
        sentinel = self._make_sentinel("text_to_image")
        vision.search_by_text = AsyncMock(return_value=sentinel)
        result = self._run(vision({"mode": "text_to_image", "query": "dress"}))
        vision.search_by_text.assert_awaited_once()
        assert result == sentinel

    def test_call_multimodal_routes_to_search_multimodal(self):
        vision = _make_vision()
        sentinel = self._make_sentinel("multimodal")
        vision.search_multimodal = AsyncMock(return_value=sentinel)
        result = self._run(vision({"mode": "multimodal", "query": "dress", "image_base64": "ZmFrZQ=="}))
        vision.search_multimodal.assert_awaited_once()
        assert result == sentinel

    def test_call_unknown_mode_returns_error(self):
        vision = _make_vision()
        result = self._run(vision({"mode": "unknown_xyz"}))
        assert result["status"] == "error"
        assert "unknown_xyz" in result["error"]

    def test_call_default_mode_is_image(self):
        """Omitting mode defaults to image search."""
        vision = _make_vision()
        vision.search_by_image = AsyncMock(return_value=self._make_sentinel("image"))
        self._run(vision({"image_base64": "ZmFrZQ=="}))
        vision.search_by_image.assert_awaited_once()


# ---------------------------------------------------------------------------
# 4. Concurrency: two awaited calls don't serialize on the event loop
# ---------------------------------------------------------------------------

class TestVisionConcurrency:
    """
    With asyncio.to_thread, two concurrent search_by_image calls should
    overlap rather than serialize.  This test verifies they can be gathered.
    """

    def test_two_image_searches_can_be_gathered(self):
        vision = _make_vision()
        sentinel = {"items": [], "status": "success", "mode": "image", "query_time_ms": 5.0}

        call_count = {"n": 0}

        async def fake_to_thread(fn, *args, **kwargs):
            call_count["n"] += 1
            return sentinel

        async def run():
            with patch("asyncio.to_thread", side_effect=fake_to_thread):
                results = await asyncio.gather(
                    vision.search_by_image({"image_base64": "ZmFrZQ=="}),
                    vision.search_by_image({"image_base64": "ZmFrZQ=="}),
                )
            return results

        results = asyncio.get_event_loop().run_until_complete(run())
        assert len(results) == 2
        assert call_count["n"] == 2
