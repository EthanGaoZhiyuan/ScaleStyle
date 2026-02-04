"""
CLIP Image Search Deployment for Ray Serve
Generates embeddings from product images using OpenAI CLIP model
"""

import logging
from typing import Dict, Any
import numpy as np
from PIL import Image
import io
import base64
import requests

from ray import serve

logger = logging.getLogger("ray.serve")

try:
    from transformers import CLIPProcessor, CLIPModel
    import torch

    CLIP_AVAILABLE = True
except ImportError:
    logger.warning("transformers not installed, CLIP search will not work")
    CLIP_AVAILABLE = False


@serve.deployment(
    name="clip-search",
    num_replicas=1,
    ray_actor_options={"num_cpus": 1, "num_gpus": 0},
    max_concurrent_queries=10,
)
class CLIPSearchDeployment:
    """
    Ray Serve deployment for CLIP-based image search

    Features:
    - Load CLIP model (openai/clip-vit-base-patch32)
    - Process image from URL or base64
    - Generate 512-dim embeddings
    - Search Milvus for similar products
    """

    def __init__(self):
        if not CLIP_AVAILABLE:
            raise RuntimeError("transformers package required for CLIP")

        # Load CLIP model
        model_name = "openai/clip-vit-base-patch32"
        logger.info(f"Loading CLIP model: {model_name}")

        self.device = "cuda" if torch.cuda.is_available() else "cpu"
        self.model = CLIPModel.from_pretrained(model_name).to(self.device)
        self.processor = CLIPProcessor.from_pretrained(model_name)
        self.model.eval()

        logger.info(f"CLIP model loaded on device: {self.device}")

        # Lazy load Milvus client
        self._milvus_client = None
        self._collection_name = "scale_style_clip_v1"

    @property
    def milvus_client(self):
        """Lazy initialization of Milvus client"""
        if self._milvus_client is None:
            from pymilvus import connections, Collection

            # Connect to Milvus
            connections.connect(
                alias="default",
                host="milvus-standalone",  # K8s service name
                port="19530",
            )

            # Get collection
            self._milvus_client = Collection(self._collection_name)
            self._milvus_client.load()

            logger.info(f"Connected to Milvus collection: {self._collection_name}")

        return self._milvus_client

    def _load_image(self, image_source: str, is_base64: bool = False) -> Image.Image:
        """
        Load image from URL or base64 string

        Args:
            image_source: URL or base64 string
            is_base64: If True, treat as base64, else as URL

        Returns:
            PIL Image object
        """
        if is_base64:
            # Decode base64
            image_data = base64.b64decode(image_source)
            image = Image.open(io.BytesIO(image_data))
        else:
            # Download from URL
            response = requests.get(image_source, timeout=10)
            response.raise_for_status()
            image = Image.open(io.BytesIO(response.content))

        # Convert to RGB if needed
        if image.mode != "RGB":
            image = image.convert("RGB")

        return image

    def _generate_embedding(self, image: Image.Image) -> np.ndarray:
        """
        Generate CLIP embedding for image

        Args:
            image: PIL Image

        Returns:
            512-dim numpy array
        """
        # Preprocess image
        inputs = self.processor(images=image, return_tensors="pt")
        inputs = {k: v.to(self.device) for k, v in inputs.items()}

        # Generate embedding
        with torch.no_grad():
            outputs = self.model.get_image_features(**inputs)

        # Normalize embedding
        embedding = outputs.cpu().numpy()[0]
        embedding = embedding / np.linalg.norm(embedding)

        return embedding

    async def __call__(self, request: Dict[str, Any]) -> Dict[str, Any]:
        """
        Handle image search request

        Request format:
        {
            "image_url": "https://example.com/image.jpg",  # OR
            "image_base64": "iVBORw0KGgoAAAANS...",
            "k": 5  # Number of results
        }

        Response format:
        {
            "items": [
                {"item_id": "12345", "score": 0.95},
                ...
            ],
            "query_time_ms": 123
        }
        """
        import time

        start_time = time.time()

        try:
            # Extract parameters
            image_url = request.get("image_url")
            image_base64 = request.get("image_base64")
            k = request.get("k", 5)

            if not image_url and not image_base64:
                return {
                    "error": "Must provide either image_url or image_base64",
                    "status": "error",
                }

            # Load image
            image = self._load_image(
                image_source=image_url or image_base64, is_base64=bool(image_base64)
            )

            # Generate embedding
            embedding = self._generate_embedding(image)

            # Search Milvus
            search_params = {
                "metric_type": "IP",  # Inner product (cosine similarity)
                "params": {"nprobe": 10},
            }

            results = self.milvus_client.search(
                data=[embedding.tolist()],
                anns_field="vector",
                param=search_params,
                limit=k,
                output_fields=["item_id"],
            )

            # Format results
            items = []
            for hit in results[0]:
                items.append(
                    {"item_id": hit.entity.get("item_id"), "score": float(hit.score)}
                )

            query_time_ms = int((time.time() - start_time) * 1000)

            return {
                "items": items,
                "k": k,
                "query_time_ms": query_time_ms,
                "status": "success",
            }

        except Exception as e:
            logger.error(f"CLIP search error: {e}", exc_info=True)
            return {"error": str(e), "status": "error"}


# Deployment handle for router
clip_search_app = CLIPSearchDeployment.bind()
