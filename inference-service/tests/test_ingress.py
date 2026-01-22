import pytest
from tests.utils import FakeHandle


@pytest.mark.asyncio
async def test_empty_candidates_fallback_popularity(monkeypatch):
    """
    Test fallback to popularity when retrieval returns empty results
    Boundary condition: Retrieval returns empty results
    """

    # Mock Redis
    class DummyRedis:
        def ping(self):
            return True

        def pipeline(self):
            class P:
                def hgetall(self, k):
                    pass

                def execute(self, **kwargs):
                    # Return meta data for popular items
                    return [
                        {
                            "article_id": "p1",
                            "detail_desc": "popular item 1",
                            "colour_group_name": "Red",
                            "price": "0.1",
                        },
                        {
                            "article_id": "p2",
                            "detail_desc": "popular item 2",
                            "colour_group_name": "Blue",
                            "price": "0.2",
                        },
                    ]

            return P()

    monkeypatch.setattr(
        "src.ray_serve.deployments.ingress._redis_client", lambda: DummyRedis()
    )

    from src.ray_serve.deployments.ingress import IngressDeployment, SearchRequest

    # Set up fake handles - router must accept (query, user_id) to match ingress call
    router = FakeHandle(
        route=lambda q, user_id=None: {
            "intent": "SEARCH",
            "filters": {},
            "flow": "smart",
        }
    )
    embed = FakeHandle(embed=lambda q, is_query=True: [0.1, 0.2, 0.3])
    retrieval = FakeHandle(search=lambda vector, **kw: [])  # Empty results!
    popularity = FakeHandle(topk=lambda k: [{"article_id": "p1"}, {"article_id": "p2"}])
    reranker = FakeHandle(
        score=lambda q, docs: {
            "scores": [0.1] * len(docs),
            "rerank_ms": 1,
            "mode": "stub",
        }
    )

    ing = IngressDeployment(router, embed, retrieval, popularity, reranker)
    resp = await ing.search(
        SearchRequest(query="red dress", k=2, debug=True, user_id="u2")
    )

    # Verify that even with empty retrieval, results exist (from popularity)
    assert "results" in resp
    assert len(resp["results"]) >= 1

    # Verify results are contract-normalized
    for item in resp["results"]:
        assert "article_id" in item

    print(
        f" Empty retrieval fallback successful, returned {len(resp['results'])} results"
    )


@pytest.mark.asyncio
async def test_redis_timeout_graceful_degradation(monkeypatch):
    """
    Test graceful degradation when Redis times out
    Boundary condition: Redis pipeline timeout/exception
    """

    class BadRedis:
        def ping(self):
            return True

        def pipeline(self):
            raise TimeoutError("Redis connection timeout")

    monkeypatch.setattr(
        "src.ray_serve.deployments.ingress._redis_client", lambda: BadRedis()
    )

    from src.ray_serve.deployments.ingress import IngressDeployment, SearchRequest

    router = FakeHandle(
        route=lambda q, user_id=None: {
            "intent": "SEARCH",
            "filters": {},
            "flow": "smart",
        }
    )
    embed = FakeHandle(embed=lambda q, is_query=True: [0.1, 0.2, 0.3])
    retrieval = FakeHandle(
        search=lambda vector, **kw: [{"article_id": "1", "score": 0.9}]
    )
    popularity = FakeHandle(topk=lambda k: [{"article_id": "p1"}])
    reranker = FakeHandle(
        score=lambda q, docs: {
            "scores": [0.1] * len(docs),
            "rerank_ms": 1,
            "mode": "stub",
        }
    )

    ing = IngressDeployment(router, embed, retrieval, popularity, reranker)

    # Even with Redis timeout, request should not crash
    resp = await ing.search(SearchRequest(query="test", k=1, debug=True, user_id="u2"))

    # Should have results (degraded to no meta enrichment or fallback)
    assert "results" in resp
    assert "request_id" in resp

    print(" Redis timeout graceful degradation successful")


@pytest.mark.asyncio
async def test_ab_flow_base_no_rerank(monkeypatch):
    """
    Test that base flow does not call reranker
    Boundary condition: A/B testing - base group
    """

    class DummyRedis:
        def ping(self):
            return True

        def pipeline(self):
            class P:
                def hgetall(self, k):
                    pass

                def execute(self):
                    return [
                        {
                            "article_id": "1",
                            "detail_desc": "test item",
                            "colour_group_name": "Red",
                            "price": "0.1",
                        }
                    ]

            return P()

    monkeypatch.setattr(
        "src.ray_serve.deployments.ingress._redis_client", lambda: DummyRedis()
    )

    from src.ray_serve.deployments.ingress import IngressDeployment, SearchRequest

    # Track reranker call count
    calls = {"rerank": 0}

    def rerank_score(q, docs):
        calls["rerank"] += 1
        return {"scores": [0.2] * len(docs), "rerank_ms": 10, "mode": "cross-encoder"}

    router = FakeHandle(
        route=lambda q, user_id=None: {
            "intent": "SEARCH",
            "filters": {},
            "flow": "base",
        }
    )
    embed = FakeHandle(embed=lambda q, is_query=True: [0.1, 0.2, 0.3])
    retrieval = FakeHandle(
        search=lambda vector, **kw: [{"article_id": "1", "score": 0.9}]
    )
    popularity = FakeHandle(topk=lambda k: [{"article_id": "p1"}])
    reranker = FakeHandle(score=rerank_score)

    ing = IngressDeployment(router, embed, retrieval, popularity, reranker)
    resp = await ing.search(SearchRequest(query="test", k=1, debug=True, user_id="u1"))

    # Base flow should NOT call reranker
    assert calls["rerank"] == 0
    assert "results" in resp  # Verify response structure
    print(f" Base flow correctly skipped reranker (call count: {calls['rerank']})")


@pytest.mark.asyncio
async def test_ab_flow_smart_calls_rerank(monkeypatch):
    """
    Test that smart flow calls reranker
    Boundary condition: A/B testing - smart group
    """

    class DummyRedis:
        def ping(self):
            return True

        def pipeline(self):
            class P:
                def hgetall(self, k):
                    pass

                def execute(self, **kwargs):  # Accept keyword arguments
                    return [
                        {
                            "article_id": "1",
                            "detail_desc": "test item",
                            "colour_group_name": "Red",
                            "price": "0.1",
                        }
                    ]

            return P()

    monkeypatch.setattr(
        "src.ray_serve.deployments.ingress._redis_client", lambda: DummyRedis()
    )

    from src.ray_serve.deployments.ingress import IngressDeployment, SearchRequest

    # Track reranker call count
    calls = {"rerank": 0}

    def rerank_score(q, docs):
        calls["rerank"] += 1
        return {"scores": [0.9] * len(docs), "rerank_ms": 10, "mode": "cross-encoder"}

    # Fix: route lambda needs to accept query and user_id parameters
    router = FakeHandle(
        route=lambda q, user_id=None: {
            "intent": "SEARCH",
            "filters": {},
            "flow": "smart",
        }
    )
    embed = FakeHandle(embed=lambda q, is_query=True: [0.1, 0.2, 0.3])
    retrieval = FakeHandle(
        search=lambda vector, **kw: [{"article_id": "1", "score": 0.9}]
    )
    popularity = FakeHandle(topk=lambda k: [{"article_id": "p1"}])
    reranker = FakeHandle(score=rerank_score)

    ing = IngressDeployment(router, embed, retrieval, popularity, reranker)
    resp = await ing.search(SearchRequest(query="test", k=1, debug=True, user_id="u2"))

    # Smart flow should call reranker
    assert calls["rerank"] == 1
    assert "results" in resp  # Verify response structure
    print(f" Smart flow correctly called reranker (call count: {calls['rerank']})")
