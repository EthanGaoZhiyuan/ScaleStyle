import sys
from pathlib import Path
from types import ModuleType

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

torch_stub = ModuleType("torch")
torch_stub.float16 = "float16"
torch_stub.float32 = "float32"
functional_stub = ModuleType("torch.nn.functional")
functional_stub.normalize = lambda *args, **kwargs: None
nn_stub = ModuleType("torch.nn")
nn_stub.functional = functional_stub
torch_stub.nn = nn_stub

transformers_stub = ModuleType("transformers")
transformers_stub.AutoModel = object
transformers_stub.AutoTokenizer = object

sys.modules.setdefault("torch", torch_stub)
sys.modules.setdefault("torch.nn", nn_stub)
sys.modules.setdefault("torch.nn.functional", functional_stub)
sys.modules.setdefault("transformers", transformers_stub)

from src.deployments.embedding import EmbeddingDeployment, EMBEDDING_MAX_ONGOING_REQUESTS


def test_embedding_deployment_has_bounded_serve_concurrency():
    options = EmbeddingDeployment._serve_deployment_options

    assert options["num_replicas"] == 1
    assert options["max_ongoing_requests"] == EMBEDDING_MAX_ONGOING_REQUESTS
    assert EMBEDDING_MAX_ONGOING_REQUESTS == 4