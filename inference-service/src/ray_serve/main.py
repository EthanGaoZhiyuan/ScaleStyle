import ray
from ray import serve
from deployments.router import RouterDeployment
from deployments.embedding import EmbeddingDeployment
from deployments.retrieval import RetrievalDeployment
from deployments.ingress import IngressDeployment
from deployments.popularity import PopularityDeployment
from deployments.reranker import RerankerDeployment
from deployments.generation import GenerationDeployment
from deployments.clip_search import CLIPSearchDeployment

ray.init(ignore_reinit_error=True)
serve.start(detached=True)

# 1. Deploy Core Services
router_handle = RouterDeployment.bind()
embedding_handle = EmbeddingDeployment.bind()
retrieval_handle = RetrievalDeployment.bind()
popularity_handle = PopularityDeployment.bind()
reranker_handle = RerankerDeployment.bind()
generation_handle = GenerationDeployment.bind()
clip_search_handle = CLIPSearchDeployment.bind()

# 2. Deploy Ingress Service with handles to Core Services
ingress = IngressDeployment.bind(
    router_handle,
    embedding_handle,
    retrieval_handle,
    popularity_handle,
    reranker_handle,
    generation_handle,
    clip_search_handle,
)

# 3. Run the Ingress Deployment
serve.run(ingress, name="scale_style_app")

print("ScaleStyle Ray Serve Application is up and running.")
