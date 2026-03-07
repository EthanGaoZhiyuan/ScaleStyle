import os
import ray
from ray import serve
from deployments.router import RouterDeployment
from deployments.embedding import EmbeddingDeployment
from deployments.retrieval import RetrievalDeployment
from deployments.ingress import IngressDeployment
from deployments.popularity import PopularityDeployment
from deployments.reranker import RerankerDeployment
from deployments.generation import GenerationDeployment

# Feature flag: Enable vision deployment (requires transformers + pymilvus)
VISION_ENABLED = os.getenv("VISION_ENABLED", "1").lower() in ("1", "true", "yes")

if VISION_ENABLED:
    try:
        from deployments.vision import VisionDeployment
        print("✅ Vision deployment enabled")
    except ImportError as e:
        print(f"⚠️  Vision dependencies not available: {e}")
        print("   Set VISION_ENABLED=0 to disable vision deployment")
        VISION_ENABLED = False

# Initialize Ray with appropriate memory configuration
# K8s pod has 8GB limit, allocate 7GB to Ray to avoid OOM
ray.init(
    ignore_reinit_error=True,
    _memory=7 * 1024 * 1024 * 1024,  # 7GB in bytes
    object_store_memory=1 * 1024 * 1024 * 1024,  # 1GB for object store
)
serve.start(detached=True)

# 1. Deploy Core Services
router_handle = RouterDeployment.bind()
embedding_handle = EmbeddingDeployment.bind()
retrieval_handle = RetrievalDeployment.bind()
popularity_handle = PopularityDeployment.bind()
reranker_handle = RerankerDeployment.bind()
generation_handle = GenerationDeployment.bind()

# Conditionally deploy vision service
vision_handle = VisionDeployment.bind() if VISION_ENABLED else None

# 2. Deploy Ingress Service with handles to Core Services
ingress = IngressDeployment.bind(
    router_handle,
    embedding_handle,
    retrieval_handle,
    popularity_handle,
    reranker_handle,
    generation_handle,
    vision_handle,
)

# 3. Run the Ingress Deployment
serve.run(ingress, name="scale_style_app")

print("ScaleStyle Ray Serve Application is up and running.")
if VISION_ENABLED:
    print("✅ Vision deployment enabled for multimodal search")
else:
    print("ℹ️  Vision deployment disabled (set VISION_ENABLED=1 to enable)")
