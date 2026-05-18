"""
Vision Deployment for Multimodal Search

CLIP-based image search using openai/clip-vit-base-patch32.
(FashionCLIP — patrickjohncyh/fashion-clip — is a future domain-specific upgrade candidate.)

Features:
- Image embedding generation (from URL or base64)
- Milvus vector search (image collection)
- Text-to-image search (CLIP text encoder)
- Fallback to text search if image unavailable
"""

import asyncio
import base64
import collections
import hashlib
import io
import logging
import os
import threading
import time
from typing import Dict, Any, List, Optional

import numpy as np
from PIL import Image
from ray import serve

logger = logging.getLogger("ray.serve")

try:
    from transformers import CLIPModel, CLIPProcessor
    import torch

    VISION_AVAILABLE = True
except ImportError:
    logger.warning("transformers not installed, vision search will not work")
    VISION_AVAILABLE = False

try:
    from pymilvus import MilvusClient

    MILVUS_AVAILABLE = True
except ImportError:
    logger.warning("pymilvus not installed, vector search will not work")
    MILVUS_AVAILABLE = False


class _ImageEmbeddingCache:
    """
    Thread-safe in-process LRU + TTL cache for CLIP image embeddings.

    Key   = SHA-256(raw decoded bytes) + ':' + model_name
    Value = {"embedding": ndarray, "model_name": str, "dim": int, "created_at": float}

    Including model_name in the key means a model change automatically causes cache
    misses without any explicit invalidation.  TTL prevents unbounded memory growth
    for long-running processes.  LRU eviction bounds size when many distinct images
    are seen.
    """

    def __init__(self, max_size: int = 256, ttl_seconds: float = 600.0) -> None:
        self._store: collections.OrderedDict = collections.OrderedDict()
        self._lock = threading.Lock()
        self._max_size = max_size
        self._ttl = ttl_seconds
        self._hits = 0
        self._misses = 0

    def _key(self, image_bytes: bytes, model_name: str) -> str:
        return hashlib.sha256(image_bytes).hexdigest() + ":" + model_name

    def get(self, image_bytes: bytes, model_name: str) -> Optional[object]:
        key = self._key(image_bytes, model_name)
        with self._lock:
            entry = self._store.get(key)
            if entry is None:
                self._misses += 1
                return None
            if time.time() - entry["created_at"] > self._ttl:
                del self._store[key]
                self._misses += 1
                return None
            self._store.move_to_end(key)
            self._hits += 1
            return entry["embedding"]

    def put(self, image_bytes: bytes, model_name: str, embedding: object) -> None:
        key = self._key(image_bytes, model_name)
        dim = embedding.shape[0] if hasattr(embedding, "shape") else len(embedding)
        entry = {
            "embedding": embedding,
            "model_name": model_name,
            "dim": dim,
            "created_at": time.time(),
        }
        with self._lock:
            if key in self._store:
                self._store.move_to_end(key)
            self._store[key] = entry
            while len(self._store) > self._max_size:
                self._store.popitem(last=False)

    @property
    def hits(self) -> int:
        with self._lock:
            return self._hits

    @property
    def misses(self) -> int:
        with self._lock:
            return self._misses

    def __len__(self) -> int:
        with self._lock:
            return len(self._store)


@serve.deployment(
    name="vision",
    num_replicas=1,
    ray_actor_options={"num_cpus": 0.1, "num_gpus": 0},
    max_ongoing_requests=int(os.getenv("VISION_MAX_ONGOING_REQUESTS", "4")),
)
class VisionDeployment:
    """
    Vision deployment for multimodal search

    Supports:
    1. Image → Image search (upload image, find similar products)
    2. Text → Image search (text query, find matching product images)
    3. Multimodal fusion (combine text and image signals)

    All public entry points are async. CPU-intensive operations (CLIP inference,
    base64 decode, PIL image loading) and blocking I/O (Milvus search) run via
    asyncio.to_thread() so the Ray Serve event loop stays free for concurrent
    requests during heavy computation.
    """

    def __init__(self):
        # Limit torch threads to prevent CPU overload
        os.environ["OMP_NUM_THREADS"] = "1"
        os.environ["MKL_NUM_THREADS"] = "1"

        if not VISION_AVAILABLE:
            raise RuntimeError("transformers package required for vision deployment")

        if not MILVUS_AVAILABLE:
            raise RuntimeError("pymilvus package required for vision deployment")

        # Configuration
        self.model_name = os.getenv("VISION_MODEL", "openai/clip-vit-base-patch32")
        milvus_host = os.getenv("MILVUS_HOST", "localhost")
        milvus_port = os.getenv("MILVUS_PORT", "19530")
        self.collection_name = os.getenv(
            "MILVUS_IMAGE_COLLECTION", "scale_style_clip_image_v1"
        )
        self.vector_field = os.getenv("IMAGE_VECTOR_FIELD", "image_embedding")
        self.nprobe = int(os.getenv("IMAGE_SEARCH_NPROBE", "10"))

        # Load CLIP model
        logger.info(f"Loading vision model: {self.model_name}")
        self.device = "cuda" if torch.cuda.is_available() else "cpu"
        self.model = CLIPModel.from_pretrained(self.model_name).to(self.device)
        self.processor = CLIPProcessor.from_pretrained(self.model_name)
        self.model.eval()
        logger.info(f"Vision model loaded on {self.device}")

        # In-process image embedding cache (LRU + TTL, thread-safe)
        _cache_size = int(os.getenv("VISION_EMBEDDING_CACHE_SIZE", "256"))
        _cache_ttl = float(os.getenv("VISION_EMBEDDING_CACHE_TTL_SECONDS", "600"))
        self._image_cache = _ImageEmbeddingCache(
            max_size=_cache_size, ttl_seconds=_cache_ttl
        )
        logger.info(f"Image embedding cache: max_size={_cache_size}, ttl={_cache_ttl}s")

        # Connect to Milvus using MilvusClient (same pattern as RetrievalDeployment)
        milvus_uri = f"http://{milvus_host}:{milvus_port}"
        logger.info(f"Connecting to Milvus at {milvus_uri}")
        self.milvus_client = MilvusClient(uri=milvus_uri)
        logger.info(f"Connected to Milvus collection: {self.collection_name}")

    # ------------------------------------------------------------------
    # Private sync helpers — CPU/network-blocking; called via to_thread
    # ------------------------------------------------------------------

    def _load_image_from_url(self, image_url: str) -> Image.Image:
        """Load image from URL (DISABLED for security)

        SSRF Protection: URL loading disabled to prevent Server-Side Request Forgery attacks.
        Use base64 image upload instead for demo/production environments.
        """
        raise ValueError(
            "Image URL loading disabled for security (SSRF protection). "
            "Please use base64 image upload instead: encode your image and send via 'image_base64' field."
        )

    def _load_image_from_base64(self, image_base64: str) -> Image.Image:
        """Load image from base64 string"""
        image_data = base64.b64decode(image_base64)
        image = Image.open(io.BytesIO(image_data))
        return image.convert("RGB")

    def _encode_image(self, image: Image.Image) -> np.ndarray:
        """
        Generate CLIP image embedding (openai/clip-vit-base-patch32).

        Returns:
            512-d normalized numpy array
        """
        inputs = self.processor(images=image, return_tensors="pt").to(self.device)

        with torch.no_grad():
            image_features = self.model.get_image_features(**inputs)
            # L2 normalize
            embedding = image_features / image_features.norm(dim=-1, keepdim=True)

        return embedding.cpu().numpy()[0]

    def _encode_image_from_base64_cached(self, image_base64: str) -> np.ndarray:
        """
        Decode base64, check cache by SHA-256 content hash, encode on miss.

        Falls back to direct decode+encode if cache raises any exception so a
        broken cache never fails the request.
        """
        try:
            image_bytes = base64.b64decode(image_base64)
            cached = self._image_cache.get(image_bytes, self.model_name)
            if cached is not None:
                logger.debug("image_embedding_cache_hit model=%s", self.model_name)
                return cached
            logger.debug("image_embedding_cache_miss model=%s", self.model_name)
            image = Image.open(io.BytesIO(image_bytes)).convert("RGB")
            embedding = self._encode_image(image)
            self._image_cache.put(image_bytes, self.model_name, embedding)
            return embedding
        except Exception as exc:
            logger.warning("image_embedding_cache_error: %s — direct encode", exc)
            image = self._load_image_from_base64(image_base64)
            return self._encode_image(image)

    def _encode_text(self, text: str) -> np.ndarray:
        """
        Generate CLIP text embedding (openai/clip-vit-base-patch32).

        Returns:
            512-d normalized numpy array
        """
        inputs = self.processor(text=text, return_tensors="pt", padding=True).to(
            self.device
        )

        with torch.no_grad():
            text_features = self.model.get_text_features(**inputs)
            # L2 normalize
            embedding = text_features / text_features.norm(dim=-1, keepdim=True)

        return embedding.cpu().numpy()[0]

    def _search_milvus(
        self,
        embedding: np.ndarray,
        k: int = 10,
    ) -> List[Dict[str, Any]]:
        """
        Search Milvus for similar images using IVF_FLAT / IP index.

        Args:
            embedding: Query embedding (512-d normalized)
            k: Number of results

        Returns:
            List of dicts with article_id, score, image_path
        """
        search_params = {
            "metric_type": "IP",
            "params": {"nprobe": self.nprobe},
        }

        results = self.milvus_client.search(
            collection_name=self.collection_name,
            data=[embedding.tolist()],
            anns_field=self.vector_field,
            limit=k,
            output_fields=["article_id", "article_id_str", "image_path"],
            search_params=search_params,
        )

        items = []
        for hit in results[0]:
            entity = hit.get("entity", {}) or {}
            items.append(
                {
                    "article_id": entity.get("article_id"),
                    "article_id_str": entity.get("article_id_str"),
                    "score": float(hit.get("distance", 0.0)),
                    "image_path": entity.get("image_path"),
                }
            )

        return items

    # ------------------------------------------------------------------
    # Sync bodies — run inside asyncio.to_thread; no event-loop calls
    # ------------------------------------------------------------------

    def _search_by_image_sync(self, request: Dict[str, Any]) -> Dict[str, Any]:
        """Blocking image-search body. Called via asyncio.to_thread."""
        start_time = time.time()

        try:
            image_url = request.get("image_url")
            image_base64 = request.get("image_base64")
            k = request.get("k", 10)

            if not image_url and not image_base64:
                return {
                    "error": "Must provide either image_url or image_base64",
                    "status": "error",
                }

            if image_url:
                image = self._load_image_from_url(image_url)
                embedding = self._encode_image(image)
            else:
                embedding = self._encode_image_from_base64_cached(image_base64)
            items = self._search_milvus(embedding, k=k)

            return {
                "items": items,
                "query_time_ms": (time.time() - start_time) * 1000,
                "mode": "image",
                "status": "success",
            }

        except Exception as e:
            logger.error(f"Image search error: {e}", exc_info=True)
            return {
                "error": str(e),
                "status": "error",
                "query_time_ms": (time.time() - start_time) * 1000,
            }

    def _search_by_text_sync(self, request: Dict[str, Any]) -> Dict[str, Any]:
        """Blocking text-to-image search body. Called via asyncio.to_thread."""
        start_time = time.time()

        try:
            query = request.get("query", "").strip()
            k = request.get("k", 10)

            if not query:
                return {"error": "Query text is required", "status": "error"}

            embedding = self._encode_text(query)
            items = self._search_milvus(embedding, k=k)

            return {
                "items": items,
                "query_time_ms": (time.time() - start_time) * 1000,
                "mode": "text_to_image",
                "status": "success",
            }

        except Exception as e:
            logger.error(f"Text-to-image search error: {e}", exc_info=True)
            return {
                "error": str(e),
                "status": "error",
                "query_time_ms": (time.time() - start_time) * 1000,
            }

    def _search_multimodal_sync(self, request: Dict[str, Any]) -> Dict[str, Any]:
        """Blocking multimodal search body. Called via asyncio.to_thread."""
        start_time = time.time()

        try:
            query = request.get("query", "").strip()
            image_url = request.get("image_url")
            image_base64 = request.get("image_base64")
            k = request.get("k", 10)
            text_weight = request.get("text_weight", 0.5)
            image_weight = request.get("image_weight", 0.5)

            if not query and not (image_url or image_base64):
                return {
                    "error": "Must provide at least one of: query, image_url, image_base64",
                    "status": "error",
                }

            embeddings = []
            weights = []

            if query:
                text_emb = self._encode_text(query)
                embeddings.append(text_emb)
                weights.append(text_weight)

            if image_url or image_base64:
                if image_url:
                    image = self._load_image_from_url(image_url)
                    image_emb = self._encode_image(image)
                else:
                    image_emb = self._encode_image_from_base64_cached(image_base64)
                embeddings.append(image_emb)
                weights.append(image_weight)

            total_weight = sum(weights)
            weights = [w / total_weight for w in weights]

            fused_embedding = sum(w * emb for w, emb in zip(weights, embeddings))
            fused_embedding = fused_embedding / np.linalg.norm(fused_embedding)

            items = self._search_milvus(fused_embedding, k=k)

            return {
                "items": items,
                "query_time_ms": (time.time() - start_time) * 1000,
                "mode": "multimodal",
                "fusion_weights": {
                    "text": weights[0] if query else 0,
                    "image": weights[-1],
                },
                "status": "success",
            }

        except Exception as e:
            logger.error(f"Multimodal search error: {e}", exc_info=True)
            return {
                "error": str(e),
                "status": "error",
                "query_time_ms": (time.time() - start_time) * 1000,
            }

    # ------------------------------------------------------------------
    # Public async API — off-loads blocking work via asyncio.to_thread
    # ------------------------------------------------------------------

    async def search_by_image(self, request: Dict[str, Any]) -> Dict[str, Any]:
        """
        Image-to-image search (async).

        Request:
        {
            "image_url": "https://...",  # OR
            "image_base64": "iVBORw0KGgo...",
            "k": 10,
            "ef": 100
        }

        Response:
        {
            "items": [{"article_id": "...", "score": 0.95, ...}],
            "query_time_ms": 123,
            "mode": "image"
        }
        """
        return await asyncio.to_thread(self._search_by_image_sync, request)

    async def search_by_text(self, request: Dict[str, Any]) -> Dict[str, Any]:
        """
        Text-to-image search using CLIP text encoder (async).

        Request:
        {
            "query": "red summer dress",
            "k": 10,
            "ef": 100
        }

        Response:
        {
            "items": [{"article_id": "...", "score": 0.85, ...}],
            "query_time_ms": 45,
            "mode": "text_to_image"
        }
        """
        return await asyncio.to_thread(self._search_by_text_sync, request)

    async def search_multimodal(self, request: Dict[str, Any]) -> Dict[str, Any]:
        """
        Multimodal search — combine text + image embeddings (async).

        Request:
        {
            "query": "red dress",
            "image_url": "https://...",  # OR image_base64
            "k": 10,
            "text_weight": 0.5,
            "image_weight": 0.5
        }

        Response:
        {
            "items": [...],
            "query_time_ms": 234,
            "mode": "multimodal"
        }
        """
        return await asyncio.to_thread(self._search_multimodal_sync, request)

    async def __call__(self, request: Dict[str, Any]) -> Dict[str, Any]:
        """
        Main entry point, routes to appropriate search method (async).

        Request must include "mode" field:
        - "image" or "image_to_image": Image-to-image search
        - "text_to_image": Text-to-image search
        - "multimodal": Combined search
        """
        mode = request.get("mode", "image")

        if mode in ("image", "image_to_image"):
            return await self.search_by_image(request)
        elif mode == "text_to_image":
            return await self.search_by_text(request)
        elif mode == "multimodal":
            return await self.search_multimodal(request)
        else:
            return {
                "error": f"Unknown mode: {mode}. Valid modes: image, image_to_image, text_to_image, multimodal",
                "status": "error",
            }
