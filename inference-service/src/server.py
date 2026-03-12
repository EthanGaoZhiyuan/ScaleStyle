import os
import logging
from src.utils.metrics import bootstrap_metrics_storage

bootstrap_metrics_storage(clear_existing=True)

import ray  # noqa: E402
from ray import serve  # noqa: E402
from src.utils.redis_client import validate_startup_connection  # noqa: E402
from src.deployments.router import RouterDeployment  # noqa: E402
from src.deployments.embedding import EmbeddingDeployment  # noqa: E402
from src.deployments.retrieval import RetrievalDeployment  # noqa: E402
from src.deployments.ingress import IngressDeployment  # noqa: E402
from src.deployments.popularity import PopularityDeployment  # noqa: E402
from src.deployments.reranker import RerankerDeployment  # noqa: E402
from src.deployments.generation import GenerationDeployment  # noqa: E402

# Configure logger
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("scalestyle.server")

# Feature flag: Enable vision deployment (requires transformers + pymilvus)
VISION_ENABLED = os.getenv("VISION_ENABLED", "1").lower() in ("1", "true", "yes")

if VISION_ENABLED:
    try:
        from src.deployments.vision import VisionDeployment

        logger.info("Vision deployment enabled")
    except ImportError as e:
        logger.warning(f"Vision dependencies not available: {e}")
        logger.warning("Set VISION_ENABLED=0 to disable vision deployment")
        VISION_ENABLED = False

# Ray memory configuration: environment-driven to support different deployment profiles
# - Local dev + generation enabled: 6-8 GB total
# - Production w/o generation: RAY(5)+ObjStore(1)+BGE(1.3)+reranker(0.09)+overhead(0.5) ≈ 7.9 GB → 8Gi limit
# - Production w/ generation (Qwen2-1.5B): ~11 GB total (requires 12Gi container)
RAY_MEMORY_GB = float(
    os.getenv("RAY_MEMORY_GB", "3")
)  # Total heap memory for Ray workers
RAY_OBJECT_STORE_GB = float(
    os.getenv("RAY_OBJECT_STORE_GB", "0.5")
)  # Plasma shared memory

logger.info(
    f"Initializing Ray: memory={RAY_MEMORY_GB}GB, object_store={RAY_OBJECT_STORE_GB}GB"
)

validate_startup_connection()

ray.init(
    ignore_reinit_error=True,
    resources={
        "memory": int(RAY_MEMORY_GB * 1024 * 1024 * 1024)
    },  # Heap memory (Ray 2.x+ API)
    object_store_memory=int(
        RAY_OBJECT_STORE_GB * 1024 * 1024 * 1024
    ),  # Plasma store (allocated at startup)
)
serve.start(detached=True)

# Deploy core services
router_handle = RouterDeployment.bind()
embedding_handle = EmbeddingDeployment.bind()
retrieval_handle = RetrievalDeployment.bind()
popularity_handle = PopularityDeployment.bind()
reranker_handle = RerankerDeployment.bind()
generation_handle = GenerationDeployment.bind()

# Conditionally deploy vision service
vision_handle = VisionDeployment.bind() if VISION_ENABLED else None

# Deploy ingress service with handles to core services
ingress = IngressDeployment.bind(
    router_handle,
    embedding_handle,
    retrieval_handle,
    popularity_handle,
    reranker_handle,
    generation_handle,
    vision_handle,
)

# Run the ingress deployment
serve.run(ingress, name="scale_style_app")

logger.info("ScaleStyle Ray Serve Application is up and running")
if VISION_ENABLED:
    logger.info("Vision deployment enabled for multimodal search")
else:
    logger.info("Vision deployment disabled (set VISION_ENABLED=1 to enable)")
