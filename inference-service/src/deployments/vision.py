"""
Vision Deployment for Multimodal Search

Ray Serve deployment for FashionCLIP-based image search.

Features:
- Image embedding generation (from URL or base64)
- Milvus vector search (image collection)
- Text-to-image search (CLIP text encoder)
- Fallback to text search if image unavailable
"""

import base64
import io
import logging
import os
import time
from typing import Dict, Any, List

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
    from pymilvus import Collection, connections

    MILVUS_AVAILABLE = True
except ImportError:
    logger.warning("pymilvus not installed, vector search will not work")
    MILVUS_AVAILABLE = False


@serve.deployment(
    name="vision",
    num_replicas=1,
    ray_actor_options={"num_cpus": 0.1, "num_gpus": 0},
    max_ongoing_requests=4,  # Reduced from 10 to avoid CPU oversubscription on vision tasks
)
class VisionDeployment:
    """
    Vision deployment for multimodal search

    Supports:
    1. Image → Image search (upload image, find similar products)
    2. Text → Image search (text query, find matching product images)
    3. Multimodal fusion (combine text and image signals)
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
        self.model_name = os.getenv("VISION_MODEL", "patrickjohncyh/fashion-clip")
        self.milvus_host = os.getenv("MILVUS_HOST", "localhost")
        self.milvus_port = os.getenv("MILVUS_PORT", "19530")
        self.collection_name = os.getenv(
            "MILVUS_IMAGE_COLLECTION", "scalestyle_image_v1"
        )

        # Load CLIP model
        logger.info(f"Loading vision model: {self.model_name}")
        self.device = "cuda" if torch.cuda.is_available() else "cpu"
        self.model = CLIPModel.from_pretrained(self.model_name).to(self.device)
        self.processor = CLIPProcessor.from_pretrained(self.model_name)
        self.model.eval()
        logger.info(f"✅ Vision model loaded on {self.device}")

        # Lazy load Milvus
        self._milvus_collection = None
        self._milvus_connected = False

    @property
    def milvus_collection(self) -> Collection:
        """Lazy initialization of Milvus connection"""
        if self._milvus_collection is None:
            logger.info(
                f"Connecting to Milvus at {self.milvus_host}:{self.milvus_port}"
            )
            connections.connect(
                alias="vision",
                host=self.milvus_host,
                port=self.milvus_port,
            )
            self._milvus_collection = Collection(self.collection_name)
            self._milvus_collection.load()
            self._milvus_connected = True
            logger.info(f"✅ Connected to Milvus collection: {self.collection_name}")

        return self._milvus_collection

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
        Generate FashionCLIP embedding for image

        Returns:
            512-d normalized numpy array
        """
        inputs = self.processor(images=image, return_tensors="pt").to(self.device)

        with torch.no_grad():
            image_features = self.model.get_image_features(**inputs)
            # L2 normalize
            embedding = image_features / image_features.norm(dim=-1, keepdim=True)

        return embedding.cpu().numpy()[0]

    def _encode_text(self, text: str) -> np.ndarray:
        """
        Generate FashionCLIP embedding for text query

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
        ef: int = 100,
    ) -> List[Dict[str, Any]]:
        """
        Search Milvus for similar images

        Args:
            embedding: Query embedding (512-d normalized)
            k: Number of results
            ef: HNSW search parameter (higher = more accurate, slower)

        Returns:
            List of dicts with article_id, score, image_path
        """
        search_params = {
            "metric_type": "IP",  # Inner Product (cosine for normalized vectors)
            "params": {"ef": ef},
        }

        results = self.milvus_collection.search(
            data=[embedding.tolist()],
            anns_field="vector",
            param=search_params,
            limit=k,
            output_fields=["article_id", "image_path", "width", "height"],
        )

        items = []
        for hit in results[0]:
            items.append(
                {
                    "article_id": hit.entity.get("article_id"),
                    "score": float(hit.score),
                    "image_path": hit.entity.get("image_path"),
                    "width": hit.entity.get("width"),
                    "height": hit.entity.get("height"),
                }
            )

        return items

    def search_by_image(self, request: Dict[str, Any]) -> Dict[str, Any]:
        """
        Image-to-image search

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
        start_time = time.time()

        try:
            # Extract parameters
            image_url = request.get("image_url")
            image_base64 = request.get("image_base64")
            k = request.get("k", 10)
            ef = request.get("ef", 100)

            if not image_url and not image_base64:
                return {
                    "error": "Must provide either image_url or image_base64",
                    "status": "error",
                }

            # Load image
            if image_url:
                image = self._load_image_from_url(image_url)
            else:
                image = self._load_image_from_base64(image_base64)

            # Generate embedding
            embedding = self._encode_image(image)

            # Search Milvus
            items = self._search_milvus(embedding, k=k, ef=ef)

            query_time_ms = (time.time() - start_time) * 1000

            return {
                "items": items,
                "query_time_ms": query_time_ms,
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

    def search_by_text(self, request: Dict[str, Any]) -> Dict[str, Any]:
        """
        Text-to-image search (using CLIP text encoder)

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
        start_time = time.time()

        try:
            query = request.get("query", "").strip()
            k = request.get("k", 10)
            ef = request.get("ef", 100)

            if not query:
                return {"error": "Query text is required", "status": "error"}

            # Generate text embedding
            embedding = self._encode_text(query)

            # Search Milvus
            items = self._search_milvus(embedding, k=k, ef=ef)

            query_time_ms = (time.time() - start_time) * 1000

            return {
                "items": items,
                "query_time_ms": query_time_ms,
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

    def search_multimodal(self, request: Dict[str, Any]) -> Dict[str, Any]:
        """
        Multimodal search (combine text + image)

        Request:
        {
            "query": "red dress",
            "image_url": "https://...",  # OR image_base64
            "k": 10,
            "text_weight": 0.5,  # 0.0-1.0, weight for text embedding
            "image_weight": 0.5  # 0.0-1.0, weight for image embedding
        }

        Response:
        {
            "items": [...],
            "query_time_ms": 234,
            "mode": "multimodal"
        }
        """
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

            # Generate embeddings
            embeddings = []
            weights = []

            if query:
                text_emb = self._encode_text(query)
                embeddings.append(text_emb)
                weights.append(text_weight)

            if image_url or image_base64:
                if image_url:
                    image = self._load_image_from_url(image_url)
                else:
                    image = self._load_image_from_base64(image_base64)
                image_emb = self._encode_image(image)
                embeddings.append(image_emb)
                weights.append(image_weight)

            # Weighted fusion (normalize weights)
            total_weight = sum(weights)
            weights = [w / total_weight for w in weights]

            fused_embedding = sum(w * emb for w, emb in zip(weights, embeddings))
            # Renormalize fused embedding
            fused_embedding = fused_embedding / np.linalg.norm(fused_embedding)

            # Search Milvus
            items = self._search_milvus(fused_embedding, k=k)

            query_time_ms = (time.time() - start_time) * 1000

            return {
                "items": items,
                "query_time_ms": query_time_ms,
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

    def __call__(self, request: Dict[str, Any]) -> Dict[str, Any]:
        """
        Main entry point, routes to appropriate search method

        Request must include "mode" field:
        - "image" or "image_to_image": Image-to-image search
        - "text_to_image": Text-to-image search
        - "multimodal": Combined search
        """
        mode = request.get("mode", "image")

        if mode in ("image", "image_to_image"):
            return self.search_by_image(request)
        elif mode == "text_to_image":
            return self.search_by_text(request)
        elif mode == "multimodal":
            return self.search_multimodal(request)
        else:
            return {
                "error": f"Unknown mode: {mode}. Valid modes: image, image_to_image, text_to_image, multimodal",
                "status": "error",
            }
