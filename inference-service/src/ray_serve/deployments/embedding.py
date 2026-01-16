import os
import time
import logging

import torch
import torch.nn.functional as F
from transformers import AutoModel, AutoTokenizer
from ray import serve

logger = logging.getLogger("scalestyle.embedding")


def _env_int(name: str, default: int) -> int:
    v = os.getenv(name)
    if v is None:
        return default
    try:
        return int(v)
    except Exception:
        return default


@serve.deployment(
    ray_actor_options={
        "num_cpus": _env_int("EMBEDDING_NUM_CPUS", 2),
        "num_gpus": float(os.getenv("EMBEDDING_NUM_GPUS", "0")),
    },
    autoscaling_config=None,
)
class EmbeddingDeployment:
    def __init__(self):
        self.ready = False

        self.device = "cuda" if torch.cuda.is_available() else "cpu"
        self.model_name = os.getenv("EMBEDDING_MODEL", "BAAI/bge-large-en-v1.5")
        self.max_length = int(os.getenv("EMBEDDING_MAX_LEN", "512"))
        self.query_prefix = os.getenv(
            "EMBEDDING_QUERY_PREFIX",
            "Represent this sentence for searching relevant passages:",
        ).strip()

        # dtype: cpu float32; cuda float16
        self.dtype = torch.float16 if self.device == "cuda" else torch.float32

        t0 = time.time()
        self.tokenizer = AutoTokenizer.from_pretrained(self.model_name)
        t_tok = (time.time() - t0) * 1000

        t1 = time.time()
        self.model = AutoModel.from_pretrained(self.model_name, torch_dtype=self.dtype)
        if self.device == "cuda":
            self.model = self.model.to(self.device)
        else:
            self.model = self.model.to(self.device)
        self.model.eval()
        t_model = (time.time() - t1) * 1000

        self.ready = True
        logger.info(
            "Embedding ready model=%s device=%s dtype=%s tok_ms=%.2f model_ms=%.2f",
            self.model_name,
            self.device,
            str(self.dtype),
            t_tok,
            t_model,
        )

    def is_ready(self) -> bool:
        return bool(self.ready)

    def _prep_text(self, text: str, is_query: bool) -> str:
        if is_query and self.query_prefix:
            return f"{self.query_prefix} {text}"
        return text

    def embed(self, text: str, is_query: bool = True):
        """
        Serves a single query string and returns a list of floats (vector embedding).
        """
        if isinstance(text, str):
            texts = [self._prep_text(text, is_query)]
            single = True
        else:
            texts = [self._prep_text(t, is_query) for t in text]
            single = False

        inputs = self.tokenizer(
            texts,
            max_length=self.max_length,
            padding=True,
            truncation=True,
            return_tensors="pt",
        )
        inputs = {k: v.to(self.device) for k, v in inputs.items()}

        with torch.no_grad():
            outputs = self.model(**inputs)
            # CLS pooling
            embeddings = outputs.last_hidden_state[:, 0]
            embeddings = F.normalize(embeddings, p=2, dim=1)
            vecs = embeddings.float().cpu().numpy().tolist()

        return vecs[0] if single else vecs
