import os
import time
import uuid
import logging
from typing import Optional

import redis
from fastapi import FastAPI, HTTPException
from pydantic import BaseModel, Field
from ray import serve
from ray.serve.handle import DeploymentHandle
import json


from src.ray_serve.deployments.router import RouterDeployment
from src.ray_serve.deployments.embedding import EmbeddingDeployment
from src.ray_serve.deployments.retrieval import RetrievalDeployment
from src.ray_serve.deployments.popularity import PopularityDeployment

logger = logging.getLogger("scalestyle.ingress")
app = FastAPI()

class SearchRequest(BaseModel):
    query: str = Field(..., min_length=1)
    k: int = Field(10, ge=1, le=50)
    debug: bool = False
    user_id: Optional[str] = None
    
def _redis_client() -> redis.Redis:
    host = os.getenv("REDIS_HOST", "localhost")
    port = int(os.getenv("REDIS_PORT", "6379"))
    return redis.Redis(host=host, port=port, decode_responses=True)

def _enrich_and_filter(r, candidates, filters, k):
    colour = filters.get("colour_group_name")
    price_lt = filters.get("price_lt")

    
    ids = []
    for c in candidates:
        aid = c.get("article_id")
        if aid is not None:
            ids.append(str(aid))

    pipe = r.pipeline()
    for aid in ids:
        pipe.hgetall(f"item:{aid}")
    metas = pipe.execute()

    results = []
    for c, meta in zip(candidates, metas):
        aid = c.get("article_id")
        if not meta:
            continue

        if colour and meta.get("colour_group_name") != colour:
            continue
        if price_lt is not None:
            try:
                if float(meta.get("price", "inf")) >= float(price_lt):
                    continue
            except Exception:
                pass

        results.append({"article_id": aid, "score": c.get("score"), "meta": meta})
        if len(results) >= k:
            break

    return results

@serve.deployment
@serve.ingress(app)
class IngressDeployment:
    def __init__(self, 
                 router_handle: DeploymentHandle, 
                 embedding_handle: DeploymentHandle, 
                 retrieval_handle: DeploymentHandle,
                 popularity_handle: DeploymentHandle):
        self.router_handle = router_handle
        self.embedding_handle = embedding_handle
        self.retrieval_handle = retrieval_handle
        self.popularity_handle = popularity_handle
        
        self.redis = _redis_client()
        
    @app.get("/healthz")
    async def healthz(self):
        return {"ok": True}

    @app.get("/readyz")
    async def readyz(self):
        """
        Check if redis is ready.
        """
        try:
            self.redis.ping()
        except Exception as e:
            raise HTTPException(status_code=503, detail=f"redis not ready: {e}")

        # embedding readiness
        try:
            ready = await self.embedding_handle.is_ready.remote()
            if not ready:
                raise HTTPException(status_code=503, detail="embedding not ready")
        except HTTPException:
            raise
        except Exception as e:
            raise HTTPException(status_code=503, detail=f"embedding not ready: {e}")

        # milvus readiness (retrieval deployment init already loads collection)
        retrieval_ready = False
        try:
            retrieval_ready = await self.retrieval_handle.is_ready.remote()
        except Exception:
            retrieval_ready = False

        return {
            "status": "ready",
            "deps": {
                "redis": True,
                "embedding": True,
                "retrieval": retrieval_ready,
            },
        }

    @app.post("/search")
    async def search(self, req: SearchRequest):
        request_id = str(uuid.uuid4())
        t0 = time.time()

        # 1) route
        try:
            route = await self.router_handle.route.remote(req.query)
        except Exception as e:
            logger.exception("request_id=%s route_failed err=%s", request_id, e)
            route = {"intent": "SEARCH", "filters": {}}

        intent = route.get("intent", "SEARCH")
        filters = route.get("filters") or {}

        # 2) browse path => popularity
        if intent == "BROWSE":
            results = await self.popularity_handle.topk.remote(req.k)
            total_ms = (time.time() - t0) * 1000
            resp = {"query": req.query, "route": route, "results": results, "request_id": request_id}
            if req.debug:
                resp["latency_ms"] = {"total": total_ms}
            logger.info(
                "request_id=%s intent=BROWSE k=%d total_ms=%.2f",
                request_id,
                req.k,
                total_ms,
            )
            return resp

        # 3) embed (with implicit timeout at caller; Week1: simple fallback)
        embed_ms = None
        try:
            t_embed0 = time.time()
            vector = await self.embedding_handle.embed.remote(req.query, is_query=True)
            embed_ms = (time.time() - t_embed0) * 1000
        except Exception as e:
            logger.exception("request_id=%s embed_failed err=%s -> fallback popularity", request_id, e)
            results = await self.popularity_handle.topk.remote(req.k)
            total_ms = (time.time() - t0) * 1000
            resp = {"query": req.query, "route": route, "results": results, "request_id": request_id}
            if req.debug:
                resp["latency_ms"] = {"embed": embed_ms, "total": total_ms}
            return resp

        # 4) recall from milvus
        ret_ms = None
        try:
            t_ret0 = time.time()
            candidate_k = 500 if filters else 200
            candidates = await self.retrieval_handle.search.remote(
                vector,
                top_k=req.k,
                candidate_k=candidate_k,
                filters=filters,
            )
            ret_ms = (time.time() - t_ret0) * 1000
        except Exception as e:
            logger.exception("request_id=%s retrieval_failed err=%s -> fallback popularity", request_id, e)
            results = await self.popularity_handle.topk.remote(req.k)
            total_ms = (time.time() - t0) * 1000
            resp = {"query": req.query, "route": route, "results": results, "request_id": request_id}
            if req.debug:
                resp["latency_ms"] = {"embed": embed_ms, "retrieve": ret_ms, "total": total_ms}
            return resp

        # 5) filter + enrich (redis)
        try:
            results = _enrich_and_filter(self.redis, candidates, filters, req.k)
            # if filter removes everything, fallback to popularity (Week1 pragmatic)
            if not results:
                results = await self.popularity_handle.topk.remote(req.k)
        except Exception as e:
            logger.exception("request_id=%s enrich_filter_failed err=%s -> fallback popularity", request_id, e)
            results = await self.popularity_handle.topk.remote(req.k)

        total_ms = (time.time() - t0) * 1000
        resp = {"query": req.query, "route": route, "results": results, "request_id": request_id}
        if req.debug:
            resp["latency_ms"] = {"embed": embed_ms, "retrieve": ret_ms, "total": total_ms}

        logger.info(
            "request_id=%s intent=SEARCH k=%d embed_ms=%.2f ret_ms=%.2f total_ms=%.2f filters=%s",
            request_id,
            req.k,
            embed_ms or -1,
            ret_ms or -1,
            total_ms,
            json.dumps(filters, ensure_ascii=False),
        )
        return resp
    
    
router_node = RouterDeployment.bind()
embedding_node = EmbeddingDeployment.bind()
retrieval_node = RetrievalDeployment.bind()
popularity_node = PopularityDeployment.bind()
ingress_app = IngressDeployment.bind(router_node, embedding_node, retrieval_node, popularity_node)