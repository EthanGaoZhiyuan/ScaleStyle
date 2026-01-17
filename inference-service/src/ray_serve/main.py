import ray
from ray import serve
from deployments.embedding import EmbeddingDeployment
from deployments.retrieval import RetrievalDeployment
from deployments.ingress import IngressDeployment
from deployments.popularity import PopularityDeployment
from deployments.reranker import RerankerDeployment

ray.init(ignore_reinit_error=True)
serve.start(detached=True)

# 1. Deploy Core Services
embedding_handle = EmbeddingDeployment.bind()
retrieval_handle = RetrievalDeployment.bind()
popularity_handle = PopularityDeployment.bind()
reranker_handle = RerankerDeployment.bind()

# 2. Deploy Ingress Service with handles to Core Services
ingress = IngressDeployment.bind(embedding_handle, retrieval_handle, popularity_handle, reranker_handle)

# 3. Run the Ingress Deployment
serve.run(ingress, name="scale_style_app")

print("ScaleStyle Ray Serve Application is up and running.")
