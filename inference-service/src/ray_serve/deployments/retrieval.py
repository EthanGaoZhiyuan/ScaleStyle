import logging
from ray import serve
from pymilvus import MilvusClient
import os
import time
from typing import List, Dict, Any, Optional

logger = logging.getLogger("scalestyle.retrieval")


@serve.deployment
class RetrievalDeployment:
    def __init__(self):
        self.ready = False

        self.milvus_host = os.getenv("MILVUS_HOST", "localhost")
        self.milvus_port = os.getenv("MILVUS_PORT", "19530")
        self.collection_name = os.getenv("MILVUS_COLLECTION", "scale_style_bge_v1")

        uri = f"http://{self.milvus_host}:{self.milvus_port}"
        self.client = MilvusClient(uri=uri)

        if not self.client.has_collection(self.collection_name):
            logger.warning(
                "Milvus collection '%s' not found. Retrieval will be DISABLED until init is done. "
                "Run: python data-pipeline/src/scripts/milvus_init.py",
                self.collection_name,
            )
            self.ready = False
            return

        try:
            self.client.load_collection(self.collection_name)
            self.ready = True
            logger.info("Milvus ready uri=%s collection=%s", uri, self.collection_name)
        except Exception as e:
            logger.exception("Milvus load_collection failed: %s", e)
            self.ready = False

    def is_ready(self) -> bool:
        return bool(self.ready)

    def _build_filter_expr(self, filters: Dict[str, Any]) -> Optional[str]:
        exprs = []
        colour = filters.get("colour_group_name")
        price_lt = filters.get("price_lt")
        if colour:
            exprs.append(f'colour_group_name == "{colour}"')
        if price_lt is not None:
            exprs.append(f"price < {float(price_lt)}")
        return " and ".join(exprs) if exprs else None

    def search(
        self,
        vector: List[float],
        top_k: int = 10,
        candidate_k: int = 200,
        filters: Optional[Dict[str, Any]] = None,
    ) -> List[Dict[str, Any]]:
        if not self.ready:
            raise RuntimeError(
                "Retrieval not ready (Milvus collection missing or not loaded)"
            )

        t0 = time.time()
        search_params = {"metric_type": "COSINE", "params": {"nprobe": 10}}

        expr = self._build_filter_expr(filters or {})

        kwargs = {}
        if expr:
            kwargs["filter"] = expr

        try:
            results = self.client.search(
                collection_name=self.collection_name,
                data=[vector],
                limit=candidate_k,
                output_fields=["article_id"],
                search_params=search_params,
                **kwargs,
            )
        except TypeError:
            # fallback: older signature
            kwargs = {}
            if expr:
                kwargs["expr"] = expr
            results = self.client.search(
                collection_name=self.collection_name,
                data=[vector],
                limit=candidate_k,
                output_fields=["article_id"],
                search_params=search_params,
                **kwargs,
            )

        hits = results[0]
        out = []
        for h in hits:
            ent = h.get("entity", {}) or {}
            article_id = ent.get("article_id", h.get("id"))
            out.append({"article_id": article_id, "score": h.get("distance")})

        logger.info(
            "search candidate_k=%d expr=%s took=%.2fms",
            candidate_k,
            expr,
            (time.time() - t0) * 1000,
        )
        return out[:candidate_k]
