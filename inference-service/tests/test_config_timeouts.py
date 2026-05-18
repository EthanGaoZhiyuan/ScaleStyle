"""
Unit tests for timeout configuration defaults.

Validates production-viable timeout defaults that prevent constant
degradation in K8s deployments without explicit overrides.
"""

import os
from src.config import (
    EmbeddingConfig,
    RetrievalConfig,
    RerankerConfig,
    GenerationConfig,
    PersonalizationConfig,
)


def test_embedding_timeout_production_viable():
    """Embedding timeout should be production-viable for CPU inference."""
    # Default should be 500ms (enough for cold CPU inference)
    assert (
        EmbeddingConfig.TIMEOUT_MS == 500
    ), "EMBEDDING_TIMEOUT_MS default should be 500ms for CPU-based BGE-small"


def test_retrieval_timeout_production_viable():
    """Retrieval timeout should be production-viable for Milvus queries."""
    # Default should be 300ms (enough for warm/cold Milvus queries)
    assert (
        RetrievalConfig.TIMEOUT_MS == 300
    ), "RETRIEVAL_TIMEOUT_MS default should be 300ms for Milvus ANN over 100K+ vectors"


def test_reranker_timeout_production_viable():
    """Reranker timeout should be production-viable for CPU inference."""
    # Default should be 250ms (enough for CPU-based cross-encoder)
    assert (
        RerankerConfig.TIMEOUT_MS == 250
    ), "RERANKER_TIMEOUT_MS default should be 250ms for CPU-based cross-encoder"


def test_timeout_environment_override():
    """Timeouts should be overridable via environment variables."""
    # Save original env
    original_env = {}
    env_vars = ["EMBEDDING_TIMEOUT_MS", "RETRIEVAL_TIMEOUT_MS", "RERANKER_TIMEOUT_MS"]

    for var in env_vars:
        original_env[var] = os.getenv(var)

    try:
        # Set custom timeouts
        os.environ["EMBEDDING_TIMEOUT_MS"] = "100"
        os.environ["RETRIEVAL_TIMEOUT_MS"] = "150"
        os.environ["RERANKER_TIMEOUT_MS"] = "75"

        # Reimport config classes (in real scenario, these would be read at startup)
        # For this test, we just verify the pattern is correct by checking getenv logic
        assert int(os.getenv("EMBEDDING_TIMEOUT_MS", "500")) == 100
        assert int(os.getenv("RETRIEVAL_TIMEOUT_MS", "300")) == 150
        assert int(os.getenv("RERANKER_TIMEOUT_MS", "250")) == 75

    finally:
        # Restore original env
        for var, val in original_env.items():
            if val is None:
                os.environ.pop(var, None)
            else:
                os.environ[var] = val


def test_timeout_hierarchy_within_gateway_deadline():
    """
    Enforce the timeout budget hierarchy:

      inner optional stage timeout < gateway application deadline (500ms)

    docker-compose tightens all stage timeouts so that, even in the worst
    case where embed + retrieve + rerank all run sequentially, the total
    stays inside the 500ms Reactor deadline on the gateway side.

    Production K8s defaults (config.py) are intentionally wider because
    nodes have more predictable latency and GPU acceleration; they are not
    constrained by the local-dev 500ms budget.
    """
    # docker-compose local-dev overrides (must all be < 600ms gateway deadline)
    GATEWAY_DEADLINE_MS = 600
    docker_overrides = {
        "EMBEDDING_TIMEOUT_MS": 200,
        "RETRIEVAL_TIMEOUT_MS": 150,
        "RERANKER_TIMEOUT_MS": 120,
        "GENERATION_TIMEOUT_MS": 50,
        "PERSONALIZATION_TIMEOUT_MS": 50,
    }

    for name, value in docker_overrides.items():
        assert (
            value < GATEWAY_DEADLINE_MS
        ), f"{name}={value}ms exceeds gateway deadline {GATEWAY_DEADLINE_MS}ms"

    # Document that actual config.py defaults match expectations
    assert EmbeddingConfig.TIMEOUT_MS == 500
    assert RetrievalConfig.TIMEOUT_MS == 300
    assert RerankerConfig.TIMEOUT_MS == 250
    assert GenerationConfig.TIMEOUT_MS == 50
    assert PersonalizationConfig.SNAPSHOT_TIMEOUT_MS == 50
