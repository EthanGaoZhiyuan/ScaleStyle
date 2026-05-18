"""
Tests for the image embedding cache in VisionDeployment.

Verifies:
1. Same image bytes encodes only once (cache hit skips _encode_image).
2. Different image bytes do not collide.
3. Model name change causes a cache miss (different key).
4. Cache failure does not fail the request (falls back to direct encode).
5. TTL expiry causes re-encode on next call.
6. LRU eviction drops oldest entries when capacity is reached.

Tests use the same stub pattern as test_vision_async.py to avoid importing
torch / transformers / pymilvus.
"""

import base64
import sys
import time
import types
from unittest.mock import MagicMock, patch

# ---------------------------------------------------------------------------
# Stub heavy dependencies (identical to test_vision_async.py pattern)
# ---------------------------------------------------------------------------


def _install_vision_stubs():
    torch_mod = types.ModuleType("torch")
    torch_mod.no_grad = MagicMock(
        return_value=MagicMock(
            __enter__=lambda s, *a: s,
            __exit__=lambda s, *a: None,
        )
    )
    torch_mod.cuda = MagicMock()
    torch_mod.cuda.is_available = lambda: False
    sys.modules.setdefault("torch", torch_mod)

    transformers_mod = types.ModuleType("transformers")
    transformers_mod.CLIPModel = MagicMock()
    transformers_mod.CLIPProcessor = MagicMock()
    sys.modules.setdefault("transformers", transformers_mod)

    pymilvus_mod = types.ModuleType("pymilvus")
    pymilvus_mod.MilvusClient = MagicMock()
    sys.modules.setdefault("pymilvus", pymilvus_mod)

    pil_mod = types.ModuleType("PIL")
    pil_image_mod = types.ModuleType("PIL.Image")
    pil_image_mod.Image = MagicMock()
    pil_image_mod.open = MagicMock(
        return_value=MagicMock(convert=lambda m: MagicMock())
    )
    pil_mod.Image = pil_image_mod
    sys.modules.setdefault("PIL", pil_mod)
    sys.modules.setdefault("PIL.Image", pil_image_mod)

    np_mod = types.ModuleType("numpy")
    np_mod.ndarray = object
    linalg = types.ModuleType("numpy.linalg")
    linalg.norm = lambda x: 1.0
    np_mod.linalg = linalg
    sys.modules.setdefault("numpy", np_mod)

    ray_mod = types.ModuleType("ray")
    serve_mod = types.ModuleType("ray.serve")
    serve_mod.deployment = lambda **_kw: (lambda cls: cls)
    ray_mod.serve = serve_mod
    sys.modules.setdefault("ray", ray_mod)
    sys.modules.setdefault("ray.serve", serve_mod)


_install_vision_stubs()

from src.deployments.vision import _ImageEmbeddingCache, VisionDeployment  # noqa: E402

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_vision(model_name="test-model") -> VisionDeployment:
    """Return a VisionDeployment bypassing __init__, with cache attached."""
    obj = object.__new__(VisionDeployment)
    obj.model_name = model_name
    obj.device = "cpu"
    obj.model = MagicMock()
    obj.processor = MagicMock()
    obj.milvus_client = MagicMock()
    obj.collection_name = "stub"
    obj.vector_field = "stub"
    obj.nprobe = 10
    obj._image_cache = _ImageEmbeddingCache(max_size=8, ttl_seconds=60.0)
    return obj


def _b64(data: bytes) -> str:
    return base64.b64encode(data).decode()


# ---------------------------------------------------------------------------
# _ImageEmbeddingCache unit tests
# ---------------------------------------------------------------------------


class TestImageEmbeddingCache:

    def test_get_on_empty_cache_returns_none(self):
        cache = _ImageEmbeddingCache()
        assert cache.get(b"abc", "model-a") is None

    def test_put_then_get_returns_same_embedding(self):
        cache = _ImageEmbeddingCache()
        emb = MagicMock()
        emb.shape = (512,)
        cache.put(b"imagedata", "model-a", emb)
        result = cache.get(b"imagedata", "model-a")
        assert result is emb

    def test_different_bytes_do_not_collide(self):
        cache = _ImageEmbeddingCache()
        emb_a = MagicMock(shape=(512,))
        emb_b = MagicMock(shape=(512,))
        cache.put(b"image-a", "model", emb_a)
        cache.put(b"image-b", "model", emb_b)
        assert cache.get(b"image-a", "model") is emb_a
        assert cache.get(b"image-b", "model") is emb_b

    def test_model_name_change_is_cache_miss(self):
        cache = _ImageEmbeddingCache()
        emb = MagicMock(shape=(512,))
        cache.put(b"img", "model-v1", emb)
        # Same bytes, different model → different key → miss
        assert cache.get(b"img", "model-v2") is None

    def test_ttl_expiry_returns_none(self):
        cache = _ImageEmbeddingCache(ttl_seconds=0.01)
        emb = MagicMock(shape=(512,))
        cache.put(b"img", "model", emb)
        time.sleep(0.05)
        assert cache.get(b"img", "model") is None

    def test_lru_eviction_removes_oldest(self):
        cache = _ImageEmbeddingCache(max_size=2)
        ea = MagicMock(shape=(512,))
        eb = MagicMock(shape=(512,))
        ec = MagicMock(shape=(512,))
        cache.put(b"a", "m", ea)
        cache.put(b"b", "m", eb)
        cache.put(b"c", "m", ec)  # evicts "a"
        assert cache.get(b"a", "m") is None
        assert cache.get(b"b", "m") is eb
        assert cache.get(b"c", "m") is ec

    def test_hit_miss_counters(self):
        cache = _ImageEmbeddingCache()
        emb = MagicMock(shape=(512,))
        cache.get(b"x", "m")  # miss
        cache.put(b"x", "m", emb)
        cache.get(b"x", "m")  # hit
        assert cache.hits == 1
        assert cache.misses == 1

    def test_len_reflects_stored_entries(self):
        cache = _ImageEmbeddingCache()
        assert len(cache) == 0
        cache.put(b"a", "m", MagicMock(shape=(512,)))
        cache.put(b"b", "m", MagicMock(shape=(512,)))
        assert len(cache) == 2

    def test_thread_safety_concurrent_puts(self):
        import threading

        cache = _ImageEmbeddingCache(max_size=1000)
        errors = []

        def worker(i):
            try:
                data = f"image-{i}".encode()
                emb = MagicMock(shape=(512,))
                cache.put(data, "model", emb)
                cache.get(data, "model")
            except Exception as exc:
                errors.append(exc)

        threads = [threading.Thread(target=worker, args=(i,)) for i in range(50)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        assert errors == []


# ---------------------------------------------------------------------------
# VisionDeployment cache integration tests
# ---------------------------------------------------------------------------


class TestVisionDeploymentCaching:

    def test_same_base64_encodes_only_once(self):
        """Two calls with the same image_base64 must call _encode_image exactly once."""
        vision = _make_vision()
        fake_embedding = MagicMock(shape=(512,))
        vision._encode_image = MagicMock(return_value=fake_embedding)

        img_bytes = b"fake-image-data"
        b64 = _b64(img_bytes)

        with patch(
            "PIL.Image.open", return_value=MagicMock(convert=lambda m: MagicMock())
        ):
            emb1 = vision._encode_image_from_base64_cached(b64)
            emb2 = vision._encode_image_from_base64_cached(b64)

        assert vision._encode_image.call_count == 1
        assert emb1 is fake_embedding
        assert emb2 is fake_embedding

    def test_different_base64_encodes_each_separately(self):
        """Two different images produce two separate encode calls; embeddings don't collide."""
        vision = _make_vision()
        emb_a = MagicMock(shape=(512,))
        emb_b = MagicMock(shape=(512,))
        vision._encode_image = MagicMock(side_effect=[emb_a, emb_b])

        b64_a = _b64(b"image-content-a")
        b64_b = _b64(b"image-content-b")

        with patch(
            "PIL.Image.open", return_value=MagicMock(convert=lambda m: MagicMock())
        ):
            result_a = vision._encode_image_from_base64_cached(b64_a)
            result_b = vision._encode_image_from_base64_cached(b64_b)

        assert vision._encode_image.call_count == 2
        assert result_a is emb_a
        assert result_b is emb_b

    def test_model_name_change_invalidates_cache(self):
        """Same image bytes under different model_name → cache miss → two encode calls."""
        vision = _make_vision(model_name="clip-v1")
        emb1 = MagicMock(shape=(512,))
        emb2 = MagicMock(shape=(512,))
        vision._encode_image = MagicMock(side_effect=[emb1, emb2])

        img_b64 = _b64(b"same-image")

        with patch(
            "PIL.Image.open", return_value=MagicMock(convert=lambda m: MagicMock())
        ):
            r1 = vision._encode_image_from_base64_cached(img_b64)

        # Switch model name — simulates model upgrade
        vision.model_name = "clip-v2"

        with patch(
            "PIL.Image.open", return_value=MagicMock(convert=lambda m: MagicMock())
        ):
            r2 = vision._encode_image_from_base64_cached(img_b64)

        assert vision._encode_image.call_count == 2
        assert r1 is emb1
        assert r2 is emb2

    def test_cache_failure_does_not_fail_request(self):
        """If the cache raises, _encode_image_from_base64_cached falls back to direct encode."""
        vision = _make_vision()
        fake_embedding = MagicMock(shape=(512,))
        vision._encode_image = MagicMock(return_value=fake_embedding)

        # Corrupt cache: get raises
        vision._image_cache.get = MagicMock(side_effect=RuntimeError("cache boom"))
        vision._load_image_from_base64 = MagicMock(return_value=MagicMock())

        result = vision._encode_image_from_base64_cached(_b64(b"img"))

        assert result is fake_embedding
        vision._encode_image.assert_called_once()

    def test_cache_miss_stores_result(self):
        """After a miss, the embedding is stored so the next call is a hit."""
        vision = _make_vision()
        emb = MagicMock(shape=(512,))
        vision._encode_image = MagicMock(return_value=emb)

        b64 = _b64(b"new-image")
        assert vision._image_cache.misses == 0
        assert vision._image_cache.hits == 0

        with patch(
            "PIL.Image.open", return_value=MagicMock(convert=lambda m: MagicMock())
        ):
            vision._encode_image_from_base64_cached(b64)  # miss
            vision._encode_image_from_base64_cached(b64)  # hit

        assert vision._image_cache.misses == 1
        assert vision._image_cache.hits == 1
        assert vision._encode_image.call_count == 1
