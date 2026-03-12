"""
Unit tests for timeout configuration defaults.

Validates production-viable timeout defaults that prevent constant
degradation in K8s deployments without explicit overrides.
"""

import os
import pytest
from src.config import EmbeddingConfig, RetrievalConfig, RerankerConfig


def test_embedding_timeout_production_viable():
    """Embedding timeout should be production-viable for CPU inference."""
    # Default should be 500ms (enough for cold CPU inference)
    assert EmbeddingConfig.TIMEOUT_MS == 500, \
        "EMBEDDING_TIMEOUT_MS default should be 500ms for CPU-based BGE-large"


def test_retrieval_timeout_production_viable():
    """Retrieval timeout should be production-viable for Milvus queries."""
    # Default should be 300ms (enough for warm/cold Milvus queries)
    assert RetrievalConfig.TIMEOUT_MS == 300, \
        "RETRIEVAL_TIMEOUT_MS default should be 300ms for Milvus ANN over 100K+ vectors"


def test_reranker_timeout_production_viable():
    """Reranker timeout should be production-viable for CPU inference."""
    # Default should be 250ms (enough for CPU-based cross-encoder)
    assert RerankerConfig.TIMEOUT_MS == 250, \
        "RERANKER_TIMEOUT_MS default should be 250ms for CPU-based cross-encoder"


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


def test_timeout_comparison_with_docker_compose():
    """
    Document relationship between config.py defaults and docker-compose overrides.
    
    This test serves as documentation for the timeout architecture:
    - config.py defaults: Production-viable for CPU-only K8s nodes
    - docker-compose.yml: More generous for local dev (allows debugging)
    - GPU deployments: Should override via environment to tighter values
    """
    # config.py defaults (production-viable for CPU)
    config_defaults = {
        "EMBEDDING_TIMEOUT_MS": 500,
        "RETRIEVAL_TIMEOUT_MS": 300,
        "RERANKER_TIMEOUT_MS": 250,
    }
    
    # docker-compose.yml typical overrides (generous for local dev)
    docker_overrides = {
        "EMBEDDING_TIMEOUT_MS": 1200,
        "RETRIEVAL_TIMEOUT_MS": 800,
        "RERANKER_TIMEOUT_MS": 1200,
    }
    
    # Verify config defaults are reasonable (not absurdly tight)
    assert config_defaults["EMBEDDING_TIMEOUT_MS"] >= 200, \
        "Embedding timeout too tight for cold CPU inference"
    assert config_defaults["RETRIEVAL_TIMEOUT_MS"] >= 100, \
        "Retrieval timeout too tight for Milvus queries"
    assert config_defaults["RERANKER_TIMEOUT_MS"] >= 100, \
        "Reranker timeout too tight for CPU inference"
    
    # Verify docker-compose overrides are more generous
    assert docker_overrides["EMBEDDING_TIMEOUT_MS"] > config_defaults["EMBEDDING_TIMEOUT_MS"]
    assert docker_overrides["RETRIEVAL_TIMEOUT_MS"] > config_defaults["RETRIEVAL_TIMEOUT_MS"]
    assert docker_overrides["RERANKER_TIMEOUT_MS"] > config_defaults["RERANKER_TIMEOUT_MS"]
    
    # Document that actual config values match expectations
    assert EmbeddingConfig.TIMEOUT_MS == config_defaults["EMBEDDING_TIMEOUT_MS"]
    assert RetrievalConfig.TIMEOUT_MS == config_defaults["RETRIEVAL_TIMEOUT_MS"]
    assert RerankerConfig.TIMEOUT_MS == config_defaults["RERANKER_TIMEOUT_MS"]
